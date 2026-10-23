import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
_spec = importlib.util.spec_from_file_location("bootstrap_state", FIXTURES / "bootstrap/state.py")
state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state)

REPO, TARGET, UPSTREAM, PIN = state.REPO, state.TARGET, state.UPSTREAM, state.PIN
CONFIRMED = (
    "UNSAFE ON SHARED APP: consumer CI can expose agent credentials and the GitHub App is "
    "installed on every repository of this organization (repository_selection=all)."
)
ASSUMED = (
    "UNSAFE ON SHARED APP: consumer CI can expose agent credentials and the GitHub App's reach "
    "cannot be read with maintainer credentials, so it is assumed to reach other repositories; "
    "fix apply re-checks the installation with the App token before any push."
)
APP_IDENTITY_UNKNOWN = (
    "ERROR: GitHub App identity unknown: pass --app-slug and --app-client-id "
    "from the App's settings page."
)
ORG_INSTALLATIONS = "GET orgs/your-org/installations"
EXPOSED_CI = b"""name: CI
on: pull_request
permissions:
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-24.04
    steps:
      - run: echo deploy
        env:
          TOKEN: ${{ secrets.DEPLOY_TOKEN }}
"""


def _files(**changes: bytes | None) -> dict[str, bytes]:
    files = {"README.md": b"# Example\n", **state.consumer_files()}
    for path, data in changes.items():
        if data is None:
            files.pop(path)
        else:
            files[path] = data
    return files


def _with_files(responses: dict, files: dict[str, bytes]) -> dict:
    state.set_files(responses, files)
    return responses


def _fix_workflow(pin: str = PIN) -> bytes:
    text = (FIXTURES / "preflight/template_fix_propose.yml").read_text()
    return text.replace(state.T10B, pin).replace("v0.0.0", state.VERSION).encode()


def _check(tmp_path, responses: dict):
    fake = state.FakeGitHub(tmp_path, responses)
    result = fake.run("--check", "--repo", REPO)
    return fake, result


def _org_listing(selection: str, slug: str = "example-agent") -> dict:
    installation = {
        "app_slug": slug,
        "client_id": "Iv23liExample",
        "repository_selection": selection,
    }
    return {"json": {"total_count": 1, "installations": [installation]}}


def _installation_calls(fake) -> list[str]:
    return [call["endpoint"] for call in fake.calls() if "installations" in call["endpoint"]]


def test_a_bootstrapped_repository_passes_check_with_read_calls_only(tmp_path):
    fake, result = _check(tmp_path, state.healthy_target())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: Mode: System One" in result.stdout
    assert "Doctor found no errors." in result.stdout
    assert "UNSAFE" not in result.stdout
    assert all(call["method"] == "GET" and "--input" not in call["argv"] for call in fake.calls())
    endpoints = {call["endpoint"].split("?")[0] for call in fake.calls()}
    assert f"{TARGET}/environments/agent-apply/secrets" in endpoints
    assert f"{UPSTREAM}/git/ref/tags/{state.VERSION}" in endpoints

    identity = (
        "OK: GitHub App example-agent has client id Iv23liExample "
        "and bot account example-agent[bot]."
    )
    assert identity in result.stdout.splitlines()
    assert "GET users/example-agent%5Bbot%5D" in {
        f"GET {call['endpoint']}" for call in fake.calls()
    }
    assert _installation_calls(fake) == []

    doctor = [sys.executable, str(ROOT / "lib/agent_repo_cli.py"), "doctor", "--repo", REPO]
    flags = ("--app-slug", "example-agent", "--app-client-id", "Iv23liExample")
    direct = fake.run(command=[*doctor, *flags])
    assert (direct.returncode, direct.stdout) == (0, result.stdout)
    mismatched = fake.run(
        command=[*doctor, "--app-slug", "other-agent", "--app-client-id", "Iv23liOther"]
    )
    assert mismatched.returncode == 1
    lines = mismatched.stdout.splitlines()
    assert "ERROR: --app-slug does not match AGENT_BOT_LOGIN." in lines
    assert "ERROR: --app-client-id does not match AGENT_APP_CLIENT_ID." in lines
    assert all(call["method"] == "GET" for call in fake.calls())


def _drift(responses: dict, change: str) -> None:
    variables = {}
    if change == "model":
        variables["AGENT_FIX_MODEL"] = "claude-opus-4-1"
    elif change == "label":
        labels = [{"name": name} for name in state.labels() if name != "agent:fix"]
        responses[f"GET {TARGET}/labels"] = {"json": labels}
    elif change == "policy":
        responses[f"GET {TARGET}/environments/agent-apply/deployment-branch-policies"] = {
            "json": {"branch_policies": [{"id": 5, "name": "*", "type": "branch"}]}
        }
    elif change == "safe-list":
        environments = responses[f"GET {TARGET}/environments"]["json"]["environments"]
        environments.append({"name": "staging", "deployment_branch_policy": None})
        variables["AGENT_SAFE_ENVIRONMENTS"] = "\n".join([*sorted(state.ENVIRONMENTS), "staging"])
    elif change == "token":
        responses[f"GET {TARGET}/actions/permissions/workflow"] = {
            "json": {"default_workflow_permissions": "write"}
        }
    elif change == "ruleset-missing":
        responses[f"GET {TARGET}/rulesets"] = {
            "json": [{"id": 11, "name": "agent-repo-template branches"}]
        }
    elif change == "ruleset-app-bypass":
        branches = dict(state.RULESETS[11])
        bypass = {"actor_id": 77, "actor_type": "Integration", "bypass_mode": "always"}
        branches["bypass_actors"] = [*branches["bypass_actors"], bypass]
        responses[f"GET {TARGET}/rulesets/11"] = {"json": branches}
    elif change == "rulesets-unavailable":
        responses[f"GET {TARGET}/rulesets"] = {"status": 403}
    elif change == "secret":
        responses[f"GET {TARGET}/environments/agent-propose-review/secrets"] = {
            "json": {"secrets": []}
        }
    elif change == "system-one-secret":
        responses[f"GET {TARGET}/environments/agent-propose-triage/secrets"] = {
            "json": {"secrets": [{"name": "AGENT_TRIAGE_KEY"}]}
        }
    elif change == "endpoints":
        variables["AGENT_FIX_ALLOWED_ENDPOINTS"] = None
    elif change == "bot-login":
        variables["AGENT_BOT_LOGIN"] = "example-agent"
    elif change == "identity-missing":
        variables.update({"AGENT_BOT_LOGIN": None, "AGENT_APP_CLIENT_ID": None})
    elif change == "bot-missing":
        responses["GET users/example-agent%5Bbot%5D"] = {"status": 404}
    elif change == "tampered-tag":
        responses[f"GET {UPSTREAM}/git/tags/{state.TAG_OBJECT}"] = {
            "json": {"object": {"type": "commit", "sha": "e" * 40}}
        }
    elif change == "branch-pin":
        files = _files()
        path = ".github/workflows/agent-review.yml"
        files[path] = files[path].replace(f"gate@{PIN}".encode(), b"gate@main")
        _with_files(responses, files)
    elif change == "old-fix-agent":
        _with_files(responses, _files(**{".github/workflows/agent-fix.yml": _fix_workflow()}))
        responses[f"GET {UPSTREAM}/compare/{state.T10B}...{PIN}"] = {
            "json": {"status": "behind", "behind_by": 2}
        }
    elif change == "agent-branch":
        head = "d" * 40
        responses[f"GET {TARGET}/git/matching-refs/heads/agent/"] = {
            "json": [{"ref": "refs/heads/agent/fix-3", "object": {"sha": head}}]
        }
        responses[f"GET {TARGET}/commits/{head}"] = {
            "json": {
                "sha": head,
                "author": {"login": "example-agent[bot]"},
                "commit": {"message": "Fix parser\n\nAgent-Base: abc\nAgent-Run: 555\n"},
            }
        }
        responses[f"GET {TARGET}/actions/runs/555"] = {
            "json": {"id": 555, "path": ".github/workflows/agent-triage.yml"}
        }
    elif change == "runner":
        responses[f"GET {TARGET}/actions/runners"] = {
            "json": {
                "runners": [
                    {
                        "name": "build-1",
                        "labels": [{"name": "self-hosted"}, {"name": "ubuntu-24.04"}],
                    }
                ]
            }
        }
    elif change == "charter":
        charter = state.consumer_files()[".agents/charter.md"].replace(b"## Mission", b"## Goal")
        _with_files(responses, _files(**{".agents/charter.md": charter}))
    state.set_variables(responses, variables)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "model",
            "ERROR: Variable AGENT_FIX_MODEL differs from models.fix; run ./bootstrap.sh "
            "--sync-models --repo your-org/example.",
        ),
        ("label", "ERROR: Missing label agent:fix."),
        ("policy", "ERROR: Environment agent-apply must allow deployments from main only."),
        (
            "safe-list",
            "ERROR: AGENT_SAFE_ENVIRONMENTS lists staging, whose deployment policy does not "
            "exclude agent branches.",
        ),
        (
            "token",
            "ERROR: AGENT_CI_DEFAULT_TOKEN_READONLY is true but the default workflow token can "
            "write.",
        ),
        (
            "ruleset-missing",
            "ERROR: Ruleset agent-repo-template tags is missing or differs from bootstrap; rerun "
            "bootstrap.",
        ),
        (
            "ruleset-app-bypass",
            "ERROR: Ruleset agent-repo-template branches is missing or differs from bootstrap; "
            "rerun bootstrap.",
        ),
        (
            "rulesets-unavailable",
            "UNSAFE: rulesets are unavailable, so nothing stops the GitHub App from writing "
            "non-agent refs.",
        ),
        ("secret", "ERROR: Missing secret AGENT_REVIEW_KEY for environment agent-propose-review."),
        (
            "system-one-secret",
            "ERROR: Missing secret AGENT_SYSTEM_ONE_KEY for environment agent-propose-triage.",
        ),
        ("endpoints", "ERROR: Missing variable AGENT_FIX_ALLOWED_ENDPOINTS."),
        (
            "bot-login",
            "ERROR: AGENT_BOT_LOGIN must be a bot login ending in [bot].",
        ),
        ("identity-missing", APP_IDENTITY_UNKNOWN),
        (
            "bot-missing",
            "ERROR: GitHub App bot account not found: "
            "GET /users/{app_slug}[bot] must return a Bot.",
        ),
        (
            "tampered-tag",
            "ERROR: .github/workflows/agent-triage.yml: gate pin does not match upstream v0.1.0.",
        ),
        (
            "branch-pin",
            "ERROR: .github/workflows/agent-review.yml: gate pin is not a 40-hex commit.",
        ),
        (
            "old-fix-agent",
            "ERROR: .github/workflows/agent-fix.yml: fix-agent pin predates the required "
            "fix-agent hardening.",
        ),
        (
            "agent-branch",
            "ERROR: refs/heads/agent/fix-3 head is App-authored but its Agent-Run is not an "
            "agent-fix run.",
        ),
        ("runner", "ERROR: Self-hosted runner build-1 uses the GitHub-hosted label ubuntu-24.04."),
        ("charter", "ERROR: Charter: missing required heading: Mission"),
    ],
)
def test_doctor_reports_each_drift_as_an_error(tmp_path, change, message):
    responses = state.healthy_target()
    _drift(responses, change)

    _, result = _check(tmp_path, responses)

    assert result.returncode == 1
    assert message in result.stdout.splitlines()
    assert "Doctor found no errors." not in result.stdout


def test_llm_only_mode_needs_no_system_one_key_and_warns_about_close(tmp_path):
    config = state.consumer_files()[".agents/config.yml"]
    config = config.replace(
        b"system_one:\n  provider: databricks-openjev\n  model: system.ai.openjev-qwen35-4b\n", b""
    ).replace(b"close:\n  enabled: false", b"close:\n  enabled: true")
    responses = _with_files(state.healthy_target(), _files(**{".agents/config.yml": config}))
    state.set_variables(responses, {"AGENT_SYSTEM_ONE_URL": None})
    responses[f"GET {TARGET}/environments/agent-propose-triage/secrets"] = {
        "json": {"secrets": [{"name": "AGENT_TRIAGE_KEY"}]}
    }

    _, result = _check(tmp_path, responses)

    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    assert "OK: Mode: LLM-only (no System One)" in lines
    assert "WARN: close.enabled is true in LLM-only mode; auto-close stays off." in lines
    assert "AGENT_SYSTEM_ONE" not in result.stdout


@pytest.mark.parametrize(
    ("owner", "listing", "exposed", "accept", "expected"),
    [
        ("User", None, True, False, ASSUMED),
        ("Organization", _org_listing("all"), True, False, CONFIRMED),
        ("Organization", _org_listing("selected"), True, False, ASSUMED),
        ("Organization", {"status": 403}, True, False, ASSUMED),
        ("Organization", _org_listing("all", "other-app"), True, False, ASSUMED),
        ("Organization", _org_listing("all"), False, False, None),
        ("User", None, True, True, ASSUMED),
    ],
)
def test_unknown_app_reach_is_shared_whenever_consumer_ci_exposes_credentials(
    tmp_path, owner, listing, exposed, accept, expected
):
    config = state.consumer_files()[".agents/config.yml"]
    if accept:
        config = config.replace(b"accept_ci_exposure: false", b"accept_ci_exposure: true")
    changes = {".agents/config.yml": config}
    if exposed:
        changes[".github/workflows/ci.yml"] = EXPOSED_CI
    responses = _with_files(state.healthy_target(), _files(**changes))
    responses[f"GET {TARGET}"]["json"]["owner"]["type"] = owner
    if listing is not None:
        responses[ORG_INSTALLATIONS] = listing

    fake, result = _check(tmp_path, responses)

    lines = result.stdout.splitlines()
    assert [line for line in lines if line.startswith("UNSAFE")] == ([expected] if expected else [])
    assert all(call["method"] == "GET" for call in fake.calls())
    if owner == "User" or not exposed:
        assert _installation_calls(fake) == []
    else:
        assert _installation_calls(fake) == ["orgs/your-org/installations?per_page=100"]
    if exposed:
        assert result.returncode == 1
        assert "ERROR: CI exposure: ci.yml job deploy rule secrets reaches DEPLOY_TOKEN." in lines
    else:
        assert result.returncode == 0, result.stdout
    if accept:
        assert (
            "WARN: fix.accept_ci_exposure is true; it never clears UNSAFE ON SHARED APP." in lines
        )


def test_the_accepted_template_fix_exposure_is_not_an_ordinary_exposure(tmp_path):
    files = _files(**{".github/workflows/agent-fix.yml": _fix_workflow()})
    responses = _with_files(state.healthy_target(), files)
    responses[f"GET {TARGET}"]["json"]["owner"]["type"] = "Organization"
    responses[ORG_INSTALLATIONS] = _org_listing("all")

    _, result = _check(tmp_path, responses)

    assert result.returncode == 0, result.stdout
    lines = result.stdout.splitlines()
    assert (
        "Accepted template exposure: template-fix-propose job=propose secret=AGENT_FIX_KEY" in lines
    )
    assert not any(line.startswith(("UNSAFE", "ERROR")) for line in lines)
