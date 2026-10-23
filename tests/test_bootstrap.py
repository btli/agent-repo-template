import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/bootstrap"
_spec = importlib.util.spec_from_file_location("bootstrap_state", FIXTURES / "state.py")
state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state)

REPO, TARGET, UPSTREAM = state.REPO, state.TARGET, state.UPSTREAM
PIN, VERSION = state.PIN, state.VERSION
GATEWAY = ("--gateway-url", state.GATEWAY_URL, "--gateway-kind", "databricks")
SYSTEM_ONE = ("--system-one-url", "https://systemone.example.com")
APP = ("--app-slug", "example-agent", "--app-client-id", "Iv23liExample")
INSTALL = ("--repo", REPO, "--ref", VERSION, *GATEWAY, *SYSTEM_ONE, *APP)
APP_IDENTITY_UNKNOWN = (
    "GitHub App identity unknown: pass --app-slug and --app-client-id from the App's settings page."
)
BRANCH = f"bootstrap/agent-repo-template-{VERSION}"
APP_LOOKUP_FAILED = (
    "GET /apps/{app_slug} failed with an error other than a private App's 404; "
    "bootstrap cannot cross-check the App."
)


def _tree_files(fake) -> dict[str, str]:
    [tree] = [call for call in fake.writes() if call["endpoint"] == f"{TARGET}/git/trees"]
    assert tree["method"] == "POST"
    assert tree["body"]["base_tree"] == state.BASE_TREE
    return {entry["path"]: entry["content"] for entry in tree["body"]["tree"]}


def _endpoints(calls) -> list[str]:
    return [f"{call['method']} {call['endpoint']}" for call in calls]


def test_plan_previews_every_mutation_with_read_calls_only(tmp_path):
    fake = state.FakeGitHub(tmp_path, state.empty_target())

    result = fake.run("--plan", *INSTALL)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fake.calls()
    assert all(call["method"] == "GET" and "--input" not in call["argv"] for call in fake.calls())
    output = result.stdout
    for label in state.labels():
        assert f"create label {label}" in output
    for environment in state.ENVIRONMENTS:
        assert f"create environment {environment}" in output
        assert f"allow deployments to {environment} from main" in output
    assert 'set variable AGENT_BOT_LOGIN = "example-agent[bot]"' in output
    assert 'set variable AGENT_APP_CLIENT_ID = "Iv23liExample"' in output
    assert 'set variable AGENT_FIX_MODEL = "claude-sonnet-4-5"' in output
    assert 'set variable AGENT_CI_DEFAULT_TOKEN_READONLY = "true"' in output
    assert f"set variable AGENT_FIX_ALLOWED_ENDPOINTS = {json.dumps(state.ENDPOINTS)}" in output
    assert "create ruleset agent-repo-template branches" in output
    assert "create ruleset agent-repo-template tags" in output
    assert f"create branch {BRANCH} with {len(state.consumer_files())} file(s)" in output
    assert "open draft pull request" in output
    assert f"gh secret set AGENT_TRIAGE_KEY --repo {REPO} --env agent-propose-triage" in output
    assert f"gh secret set AGENT_SYSTEM_ONE_KEY --repo {REPO} --env agent-propose-triage" in output
    assert not (fake.home / ".agent-repo-template").exists()
    endpoints = _endpoints(fake.calls())
    assert {"GET users/example-agent%5Bbot%5D", "GET apps/example-agent"} <= set(endpoints)
    assert not [call for call in fake.calls() if "installations" in call["endpoint"]]


def test_app_identity_needs_both_flags_and_a_bot_account_or_refuses_every_write(tmp_path):
    responses = state.empty_target()
    responses["GET users/missing-agent%5Bbot%5D"] = {"status": 404}
    responses["GET users/human-agent%5Bbot%5D"] = {
        "json": {"login": "human-agent[bot]", "type": "User"}
    }
    fake = state.FakeGitHub(tmp_path, responses)
    without = INSTALL[: -len(APP)]
    client = ("--app-client-id", "Iv23liExample")
    bot_missing = "GitHub App bot account not found: GET /users/{app_slug}[bot] must return a Bot."

    for arguments, message in (
        (("--plan", *without), APP_IDENTITY_UNKNOWN),
        (without, APP_IDENTITY_UNKNOWN),
        (
            (*without, "--app-slug", "example-agent"),
            "GitHub App identity incomplete: pass --app-client-id.",
        ),
        (("--plan", *without, *client), "GitHub App identity incomplete: pass --app-slug."),
        ((*without, "--app-slug", "missing-agent", *client), bot_missing),
        (("--plan", *without, "--app-slug", "human-agent", *client), bot_missing),
    ):
        result = fake.run(*arguments)

        assert result.returncode == 1
        assert f"ERROR: {message}" in result.stdout.splitlines()
        assert fake.writes() == []
        assert not [call for call in fake.calls() if "installations" in call["endpoint"]]

    # A public App is cross-checked: its client id must match the flag.
    responses["GET apps/example-agent"] = {
        "json": {"slug": "example-agent", "client_id": "Iv23liOther"}
    }
    mismatch = fake.run("--plan", *INSTALL)
    assert mismatch.returncode == 1
    assert (
        "ERROR: The public GET /apps/{app_slug} does not match --app-slug and --app-client-id."
        in mismatch.stdout.splitlines()
    )
    responses["GET apps/example-agent"]["json"]["client_id"] = "Iv23liExample"
    assert fake.run("--plan", *INSTALL).returncode == 0
    assert fake.writes() == []

    # Only a private App's 404 is excused; any other apps lookup failure stops before any write.
    for status, arguments in ((403, ("--plan", *INSTALL)), (502, INSTALL)):
        responses["GET apps/example-agent"] = {"status": status}
        failed = fake.run(*arguments)
        assert failed.returncode == 1
        assert f"ERROR: {APP_LOOKUP_FAILED}" in failed.stdout.splitlines()
        assert fake.writes() == []


def test_install_lands_files_on_a_draft_pr_branch_and_journals_every_write(tmp_path):
    fake = state.FakeGitHub(tmp_path, state.empty_target())

    result = fake.run(*INSTALL)

    assert result.returncode == 0, result.stdout + result.stderr
    writes = fake.writes()
    assert not [call for call in writes if "heads/main" in call["endpoint"]]
    assert not [call for call in writes if call["method"] not in {"POST", "PUT", "PATCH"}]
    files = _tree_files(fake)
    triage = files[".github/workflows/agent-triage.yml"]
    assert f"actions/gate@{PIN} # {VERSION}" in triage
    assert "__TEMPLATE" not in "".join(files.values())
    config = files[".agents/config.yml"]
    assert config.count("dry-run") == 3 and "live" not in config
    assert 'setup: "uv sync --frozen"' in config
    assert files["CONTRIBUTING.md"].startswith("## Repository agents")
    [commit] = [call for call in writes if call["endpoint"] == f"{TARGET}/git/commits"]
    assert commit["body"]["parents"] == [state.BASE]
    [ref] = [call for call in writes if call["endpoint"] == f"{TARGET}/git/refs"]
    assert ref["body"] == {"ref": f"refs/heads/{BRANCH}", "sha": "c" * 40}
    [pull] = [call for call in writes if call["endpoint"] == f"{TARGET}/pulls"]
    assert pull["body"]["draft"] is True
    assert (pull["body"]["head"], pull["body"]["base"]) == (BRANCH, "main")
    rulesets = [call["body"] for call in writes if call["endpoint"] == f"{TARGET}/rulesets"]
    expected = [{k: v for k, v in body.items() if k != "id"} for body in state.RULESETS.values()]
    assert rulesets == expected
    environment = f"{TARGET}/environments/agent-apply"
    assert {
        "method": "PUT",
        "body": {
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            }
        },
    }.items() <= next(call for call in writes if call["endpoint"] == environment).items()
    policy = next(
        call for call in writes if call["endpoint"] == f"{environment}/deployment-branch-policies"
    )
    assert policy["body"] == {"name": "main", "type": "branch"}
    journal = fake.journal()
    assert journal[-1]["event"] == "complete"
    done = [entry for entry in journal if entry["event"] == "done"]
    # One journal entry per mutation; creating the branch writes a tree, a commit and the ref.
    assert len(done) == len(writes) - 2
    assert all(entry["description"] for entry in done)
    assert f"gh api -X DELETE {TARGET}/git/refs/heads/{BRANCH}" in [
        entry["rollback"] for entry in done
    ]


def test_no_change_rerun_writes_nothing(tmp_path):
    fake = state.FakeGitHub(tmp_path, state.healthy_target())

    result = fake.run(*INSTALL)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fake.writes() == []
    assert "No changes." in result.stdout
    assert not (fake.home / ".agent-repo-template").exists()


def test_existing_files_are_kept_without_force_and_replaced_with_force(tmp_path):
    responses = state.healthy_target()
    files = {"README.md": b"# Example\n", **state.consumer_files()}
    files[".agents/config.yml"] = files[".agents/config.yml"].replace(
        b"fix_max_turns: 40", b"fix_max_turns: 20"
    )
    state.set_files(responses, files)
    fake = state.FakeGitHub(tmp_path, responses)

    kept = fake.run(*INSTALL)

    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert fake.writes() == []
    assert "Kept existing .agents/config.yml; pass --force to replace it." in kept.stdout

    forced = fake.run(*INSTALL, "--force")

    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert list(_tree_files(fake)) == [".agents/config.yml"]
    assert "fix_max_turns: 40" in _tree_files(fake)[".agents/config.yml"]


def test_partial_failure_prints_rollback_and_a_rerun_resumes(tmp_path):
    responses = state.empty_target()
    protected = {"protected_branches": True, "custom_branch_policies": False}
    responses[f"GET {TARGET}/environments"]["json"]["environments"] = [
        {"name": "agent-apply", "deployment_branch_policy": protected}
    ]
    responses[f"POST {TARGET}/rulesets"] = {"status": 422}
    fake = state.FakeGitHub(tmp_path, responses)

    failed = fake.run(*INSTALL)

    assert failed.returncode == 1
    output = failed.stdout
    assert "Bootstrap stopped: create ruleset agent-repo-template branches failed." in output
    rollback = output.split("Rollback commands, newest first:", 1)[1]
    assert f"gh api -X DELETE {TARGET}/labels/type%3Abug" in rollback
    apply = f"{TARGET}/environments/agent-apply"
    restore = json.dumps({"deployment_branch_policy": protected})
    assert f"echo '{restore}' | gh api -X PUT {apply} --input -" in rollback
    assert rollback.index(f"DELETE {apply}/deployment-branch-policies/") < rollback.index(restore)
    assert f"gh api -X DELETE {apply}\n" not in rollback
    assert f"gh api -X DELETE {TARGET}/environments/agent-propose-fix" in rollback
    assert f"gh api -X DELETE {TARGET}/actions/variables/AGENT_FIX_MODEL" in rollback
    assert rollback.index("AGENT_FIX_MODEL") < rollback.index("type%3Abug")
    assert not [call for call in fake.writes() if call["endpoint"] == f"{TARGET}/pulls"]

    resumed = state.healthy_target()
    resumed[f"GET {TARGET}/rulesets"] = {"json": []}
    state.set_files(resumed, {"README.md": b"# Example\n", "uv.lock": b"version = 1\n"})
    fake.responses = resumed

    rerun = fake.run(*INSTALL)

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert re.search(r"Resuming: \d+ mutation\(s\) completed by an earlier run\.", rerun.stdout)
    written = {call["endpoint"] for call in fake.writes()}
    assert written == {
        f"{TARGET}/rulesets",
        f"{TARGET}/git/trees",
        f"{TARGET}/git/commits",
        f"{TARGET}/git/refs",
        f"{TARGET}/pulls",
    }
    assert fake.journal()[-1]["event"] == "complete"


def test_ref_resolves_through_upstream_and_ignores_a_tampered_local_tag(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **os.environ}
    for command in (
        ["git", "init", "-q"],
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "tampered",
        ],
        ["git", "tag", VERSION],
    ):
        subprocess.run(command, cwd=checkout, env=git, check=True)
    local = subprocess.run(
        ["git", "rev-parse", VERSION],
        cwd=checkout,
        env=git,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake = state.FakeGitHub(tmp_path, state.empty_target())

    result = fake.run(*INSTALL, cwd=checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert local != PIN
    assert not fake.git_log.exists()
    assert f"Template {VERSION} resolves upstream to {PIN}." in result.stdout
    endpoints = _endpoints(fake.calls())
    assert f"GET {UPSTREAM}/git/ref/tags/{VERSION}" in endpoints
    assert f"GET {UPSTREAM}/git/tags/{state.TAG_OBJECT}" in endpoints
    assert f"GET {UPSTREAM}/compare/{PIN}...main" in endpoints
    workflow = _tree_files(fake)[".github/workflows/agent-review.yml"]
    assert local not in workflow and f"@{PIN} # {VERSION}" in workflow


@pytest.mark.parametrize("pin", [("--ref", VERSION), ("--candidate", PIN, "--version", VERSION)])
def test_a_commit_off_upstream_main_is_refused(tmp_path, pin):
    responses = state.empty_target()
    responses[f"GET {UPSTREAM}/compare/{PIN}...main"] = {
        "json": {"status": "diverged", "behind_by": 3}
    }
    fake = state.FakeGitHub(tmp_path, responses)

    result = fake.run("--repo", REPO, *pin, *GATEWAY, *SYSTEM_ONE, *APP)

    assert result.returncode == 1
    assert "Template commit is not on upstream main." in result.stdout
    assert fake.writes() == []


def test_candidate_installs_a_commit_on_upstream_main_with_its_version_comment(tmp_path):
    responses = state.empty_target()
    responses[f"GET {TARGET}/git/matching-refs/heads/bootstrap/agent-repo-template-v0.0.0-dev"] = {
        "json": []
    }
    fake = state.FakeGitHub(tmp_path, responses)

    result = fake.run(
        "--repo", REPO, "--candidate", PIN, "--version", "v0.0.0-dev", *GATEWAY, *SYSTEM_ONE, *APP
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"GET {UPSTREAM}/commits/{PIN}" in _endpoints(fake.calls())
    assert (
        f"actions/apply@{PIN} # v0.0.0-dev"
        in _tree_files(fake)[".github/workflows/agent-triage.yml"]
    )
    assert fake.run("--repo", REPO, "--candidate", PIN).returncode == 2
    assert fake.run("--repo", REPO, "--candidate", "abc123", "--version", VERSION).returncode == 2


def test_no_secret_value_is_printed_or_journaled(tmp_path):
    responses = state.empty_target()
    stale_secret = "ghp_" + "S3cr3tL00kingValue" * 2
    responses[f"GET {TARGET}/actions/variables"] = {
        "json": {"variables": [{"name": "AGENT_BOT_LOGIN", "value": stale_secret}]}
    }
    responses[f"POST {TARGET}/rulesets"] = {"status": 500}
    fake = state.FakeGitHub(tmp_path, responses)
    synthetic = {"GH_TOKEN": "gho_" + "Synthetic" * 4, "AGENT_FIX_KEY": "sk-" + "synthetic" * 4}

    result = fake.run(*INSTALL, env=synthetic)

    assert result.returncode == 1
    journal = json.dumps(fake.journal())
    for value in (stale_secret, *synthetic.values()):
        assert value not in result.stdout + result.stderr + journal
    assert "previous value of AGENT_BOT_LOGIN withheld" in result.stdout
    assert "update variable AGENT_BOT_LOGIN" in result.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "rulesets-unavailable",
            "Rulesets are unavailable for this repository; "
            "bootstrap cannot protect non-agent refs.",
        ),
        ("not-admin", "Bootstrap needs repository admin access."),
        (
            "extra-policy",
            "Environment agent-apply has deployment policies other than main; "
            "remove them and rerun.",
        ),
        (
            "branch-content",
            f"Branch {BRANCH} already exists with other content; merge or delete it, then rerun.",
        ),
        (
            "empty-config",
            ".agents/config.yml is not a valid agent configuration; fix it or pass --force.",
        ),
        ("empty-labels", ".agents/labels.yml must hold a labels list."),
    ],
)
def test_bootstrap_stops_before_any_write(tmp_path, change, message):
    responses = state.empty_target()
    if change == "rulesets-unavailable":
        responses[f"GET {TARGET}/rulesets"] = {"status": 403}
    elif change == "not-admin":
        responses[f"GET {TARGET}"]["json"]["permissions"]["admin"] = False
    elif change == "extra-policy":
        state.add_environments(responses, {"agent-apply": "main"})
        policies = f"GET {TARGET}/environments/agent-apply/deployment-branch-policies"
        extra = {"id": 2, "name": "release/*", "type": "branch"}
        responses[policies]["json"]["branch_policies"].append(extra)
    elif change == "branch-content":
        head = "d" * 40
        responses[f"GET {TARGET}/git/matching-refs/heads/{BRANCH}"] = {
            "json": [{"ref": f"refs/heads/{BRANCH}", "object": {"sha": head}}]
        }
        responses[f"GET {TARGET}/git/trees/{head}"] = {
            "json": {"sha": "e" * 40, "tree": [], "truncated": False}
        }
    else:
        # A zero-byte retained file is validated as itself, never replaced by the default.
        path = ".agents/config.yml" if change == "empty-config" else ".agents/labels.yml"
        state.set_files(responses, {"README.md": b"# Example\n", path: b""})
    fake = state.FakeGitHub(tmp_path, responses)

    for arguments in (("--plan", *INSTALL), INSTALL):
        result = fake.run(*arguments)

        assert result.returncode == 1
        assert f"ERROR: {message}" in result.stdout.splitlines()
        assert "Bootstrap made no changes." in result.stdout
        assert fake.writes() == []


def test_a_writable_default_token_is_reported_and_never_changed(tmp_path):
    responses = state.empty_target()
    responses[f"GET {TARGET}/actions/permissions/workflow"] = {
        "json": {"default_workflow_permissions": "write"}
    }
    fake = state.FakeGitHub(tmp_path, responses)

    result = fake.run(*INSTALL)

    assert result.returncode == 0, result.stdout + result.stderr
    command = (
        f"gh api -X PUT {TARGET}/actions/permissions/workflow -f default_workflow_permissions=read"
    )
    assert command in result.stdout
    assert 'set variable AGENT_CI_DEFAULT_TOKEN_READONLY = "false"' in result.stdout
    assert not [call for call in fake.writes() if "permissions/workflow" in call["endpoint"]]


def test_safe_environments_need_a_policy_that_excludes_agent_and_pull_refs(tmp_path):
    responses = state.empty_target()
    state.add_environments(responses, {**state.SAFE_PATTERNS, **state.UNSAFE_PATTERNS})
    protected = {"protected_branches": True, "custom_branch_policies": False}
    responses[f"GET {TARGET}/environments"]["json"]["environments"].append(
        {"name": "open", "deployment_branch_policy": protected}
    )
    fake = state.FakeGitHub(tmp_path, responses)

    result = fake.run("--plan", *INSTALL)

    assert result.returncode == 0, result.stdout + result.stderr
    safe = sorted([*state.ENVIRONMENTS, *state.SAFE_PATTERNS])
    assert (
        f"set variable AGENT_SAFE_ENVIRONMENTS = {json.dumps(chr(10).join(safe))}" in result.stdout
    )


def test_sync_models_sets_only_the_model_variables(tmp_path):
    responses = state.healthy_target()
    files = {"README.md": b"# Example\n", **state.consumer_files()}
    files[".agents/config.yml"] = files[".agents/config.yml"].replace(
        b"fix: claude-sonnet-4-5", b"fix: claude-opus-4-1"
    )
    state.set_files(responses, files)
    fake = state.FakeGitHub(tmp_path, responses)

    planned = fake.run("--sync-models", "--plan", "--repo", REPO)

    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert fake.writes() == []
    assert 'update variable AGENT_FIX_MODEL = "claude-opus-4-1"' in planned.stdout

    synced = fake.run("--sync-models", "--repo", REPO)

    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert [(call["method"], call["endpoint"], call["body"]) for call in fake.writes()] == [
        (
            "PATCH",
            f"{TARGET}/actions/variables/AGENT_FIX_MODEL",
            {"name": "AGENT_FIX_MODEL", "value": "claude-opus-4-1"},
        )
    ]

    files[".agents/config.yml"] = files[".agents/config.yml"].replace(b"claude-opus-4-1", b"gpt-4o")
    state.set_files(responses, files)
    refused = fake.run("--sync-models", "--repo", REPO)

    assert refused.returncode == 1
    assert "models.fix is not an Anthropic-family gateway model id." in refused.stdout
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("owner_type", "path"),
    [("Organization", "/organizations/your-org/settings/apps/new"), ("User", "/settings/apps/new")],
)
def test_app_link_prefills_the_union_of_role_permissions_without_a_webhook(
    tmp_path, owner_type, path
):
    responses = state.empty_target()
    responses["GET users/your-org"] = {"json": {"login": "your-org", "type": owner_type}}
    fake = state.FakeGitHub(tmp_path, responses)

    result = fake.run("app-link", "--owner", "your-org")

    assert result.returncode == 0, result.stdout + result.stderr
    [url] = [line for line in result.stdout.split() if line.startswith("https://")]
    parsed = urlsplit(url)
    assert (parsed.netloc, parsed.path) == ("github.com", path)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    assert query["webhook_active"] == "false" and query["public"] == "false"
    assert {key: query[key] for key in ("contents", "issues", "pull_requests", "metadata")} == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    levels = {"read": 1, "write": 2}
    for workflow in (ROOT / "consumer/.github/workflows").glob("*.yml"):
        for name, level in re.findall(r"permission-([a-z-]+):\s*(\w+)", workflow.read_text()):
            assert levels[query[name.replace("-", "_")]] >= levels[level], (workflow.name, name)
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("yq", "bootstrap needs yq v4"),
        ("yq3", "bootstrap needs yq v4"),
        ("gh", "bootstrap needs the GitHub CLI (gh)"),
    ],
)
def test_bootstrap_sh_checks_prerequisites_first(tmp_path, missing, message):
    fake = state.FakeGitHub(
        tmp_path, state.empty_target(), yq=missing not in {"yq", "yq3"}, gh=missing != "gh"
    )
    if missing == "yq3":
        fake._script("yq", 'echo "yq version 3.4.1"')

    result = fake.run("--plan", *INSTALL)

    assert result.returncode == 1
    assert message in result.stderr
    assert fake.calls() == []


def test_seeded_models_follow_the_setup_action_model_rule(tmp_path):
    from agent_repo.bootstrap import valid_model

    environment = {
        "HOME": str(tmp_path),
        "GATEWAY_URL": "https://gateway.example.com",
        "GATEWAY_KIND": "databricks",
        "API_KEY_ENV": "LLM_API_KEY",
    }
    for model in (
        "claude-sonnet-4-5",
        "us.anthropic.claude-3-7",
        "gpt-4o",
        "claude",
        "x/claude-a",
        "claude-a b",
    ):
        accepted = (
            subprocess.run(
                [sys.executable, "-I", str(ROOT / "actions/setup/configure.py")],
                env={**environment, "MODEL": model},
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        assert valid_model(model) is accepted, model
