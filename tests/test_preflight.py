import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from agent_repo.cli import main as cli
from agent_repo.github import GitHub
from agent_repo.yamljson import load_yaml

FIXTURES = Path(__file__).parent / "fixtures" / "preflight"


def implementation():
    return importlib.import_module("agent_repo.preflight")


CASES = [
    ("uppercase_secret_context", "secrets"),
    ("push_partial_negation", "secrets"),
    ("push_unfiltered", "secrets"),
    ("push_all", "secrets"),
    ("push_agent", "secrets"),
    ("push_main", None),
    ("push_ignore", None),
    ("pr_unfiltered", "secrets"),
    ("pr_main", "secrets"),
    ("pr_release", None),
    ("on_string", "secrets"),
    ("on_list", "secrets"),
    ("on_map", "secrets"),
    ("push_negative", None),
    ("push_reinclude", "secrets"),
    ("push_partial_ignore", "secrets"),
    ("pr_review", "secrets"),
    ("pr_review_comment", "secrets"),
    ("dispatch", None),
    ("write_all", "permissions,id-token"),
    ("job_write", "permissions"),
    ("empty_permissions", None),
    ("missing_permissions", "permissions"),
    ("job_read_override", None),
    ("secret_env", "secrets"),
    ("secret_with", "secrets"),
    ("secret_run", "secrets"),
    ("github_token", None),
    ("secret_inherit", "secrets,reusable_workflow,runner"),
    ("safe_environment", None),
    ("safe_environment_object", None),
    ("safe_env_write", "permissions"),
    ("oidc", "id-token"),
    ("self_hosted", "runner"),
    ("self_hosted_list", "runner"),
    ("runner_group", "runner"),
    ("runner_expression", "runner"),
    ("ubuntu", None),
    ("job_false", None),
    ("workflow_env", "secrets"),
    ("bracket_secret", "secrets"),
    ("bracket_github_token", None),
    ("dynamic_secret", "secrets"),
    ("unreachable_secret", None),
    ("agent_apply", "agent-apply"),
    ("pr_plus_filter", "secrets"),
    ("pr_optional_filter", "secrets"),
    ("pr_slash_ignore", "secrets"),
]


@pytest.mark.parametrize(("fixture", "rule"), CASES, ids=[case[0] for case in CASES])
def test_exposure_cases(fixture, rule):
    module = implementation()
    workflow = load_yaml(FIXTURES / f"{fixture}.yml")
    context = module.PreflightContext(
        "release/main" if fixture == "pr_slash_ignore" else "main",
        safe_environments=frozenset({"release"}),
    )
    exposures = module.analyze({f"{fixture}.yml": workflow}, context)
    assert {item.rule for item in exposures} == (set(rule.split(",")) if rule else set())
    assert all(item.workflow == f"{fixture}.yml" and item.job == "check" for item in exposures)
    assert all(item.detail for item in exposures)


def test_confirmed_readonly_default():
    module = implementation()
    assert (
        module.analyze(
            {"readonly.yml": load_yaml(FIXTURES / "missing_permissions_readonly.yml")},
            module.PreflightContext("main", token_readonly=True),
        )
        == []
    )


@pytest.mark.parametrize(
    "fixture", ["workflow_run", "workflow_run_unreachable", "workflow_run_no_download"]
)
def test_workflow_run_chain(fixture):
    module = implementation()
    source = load_yaml(
        FIXTURES / ("push_main.yml" if fixture.endswith("unreachable") else "push_all.yml")
    )
    source["name"] = "Build"
    result = module.analyze(
        {"source.yml": source, "downstream.yml": load_yaml(FIXTURES / f"{fixture}.yml")},
        module.PreflightContext("main"),
    )
    downstream = [item.rule for item in result if item.workflow == "downstream.yml"]
    assert downstream == (["workflow_run"] if fixture == "workflow_run" else [])


def test_malformed_workflow_fails_closed():
    module = implementation()
    with pytest.raises(ValueError):
        module.analyze(
            {"bad.yml": load_yaml(FIXTURES / "malformed.yml")}, module.PreflightContext("main")
        )


def api_transport(workflows, *, error=False, sha="a" * 40, entries=None):
    listed = []
    for path in sorted(workflows.iterdir()):
        data = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        listed.append(
            {
                "name": path.name,
                "path": f".github/workflows/{path.name}",
                "sha": blob,
                "type": "file",
            }
        )

    if entries is None:
        entries = listed

    def transport(request, timeout):
        parsed = urlparse(request.full_url)
        if error:
            raise RuntimeError("credential-canary")
        if parsed.path.endswith("/git/ref/heads/main"):
            value = {"object": {"type": "commit", "sha": "b" * 40}}
        else:
            assert parsed.path.endswith("/contents/.github/workflows")
            assert parse_qs(parsed.query) == {"ref": ["b" * 40]}
            assert request.get_header("Accept") == "application/vnd.github.object+json"
            value = {"type": "dir", "path": ".github/workflows", "sha": sha, "entries": entries}
        return 200, {}, json.dumps(value).encode()

    return transport


@pytest.mark.parametrize("case", ["safe", "exposed", "api_error", "stale", "bad_sha"])
def test_command_artifact_and_output(case, tmp_path, monkeypatch, capsys):
    implementation()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    shutil.copy(FIXTURES / f"command_{case}.yml", workflows / "ci.yaml")
    transport = api_transport(
        workflows, error=case == "api_error", sha="invalid" if case == "bad_sha" else "a" * 40
    )
    monkeypatch.setattr(GitHub, "_urlopen", staticmethod(transport))
    if case == "stale":
        (workflows / "ci.yaml").write_text("on: push\npermissions: {}\njobs: {}\n")
    monkeypatch.setenv("GH_TOKEN", "credential-canary")
    monkeypatch.setenv("GITHUB_REPOSITORY", "your-org/project")
    output = tmp_path / "preflight.json"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    result = cli(
        [
            "preflight",
            "--workflows-dir",
            str(workflows),
            "--default-branch",
            "main",
            "--output",
            str(output),
            "--run-id",
            "123",
            "--base-sha",
            "c" * 40,
        ]
    )
    success = case in {"safe", "exposed"}
    assert result == (0 if success else 1)
    artifact = json.loads(output.read_text())
    assert artifact["pass"] is (case == "safe")
    assert artifact["run_id"] == "123"
    assert artifact["base_sha"] == "c" * 40
    if success:
        assert artifact["workflow_tree_sha"] == "a" * 40
        assert bool(artifact["exposures"]) is (case == "exposed")
    captured = capsys.readouterr()
    assert f"pass={str(case == 'safe').lower()}" in captured.out
    assert github_output.read_text() == f"pass={str(case == 'safe').lower()}\n"
    assert "credential-canary" not in captured.out + captured.err + output.read_text()
    assert "your-org/project" not in output.read_text()


def test_action_runs_command(tmp_path, monkeypatch):
    implementation()
    action_path = Path("actions/preflight/action.yml")
    assert action_path.exists(), "preflight action missing"
    action = load_yaml(action_path)
    step = action["runs"]["steps"][0]
    output = tmp_path / "github-output"
    artifact = tmp_path / "preflight.json"
    monkeypatch.setenv("PREFLIGHT_ACTION_PATH", str(action_path.resolve().parent))
    monkeypatch.setenv("PREFLIGHT_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("PREFLIGHT_WORKFLOWS_DIR", str(tmp_path / "absent"))
    monkeypatch.setenv("PREFLIGHT_OUTPUT", str(artifact))
    monkeypatch.setenv("PREFLIGHT_TOKEN_READONLY", "false")
    monkeypatch.setenv("PREFLIGHT_SAFE_ENVIRONMENTS", "")
    monkeypatch.setenv("PREFLIGHT_RUN_ID", "123")
    monkeypatch.setenv("PREFLIGHT_BASE_SHA", "c" * 40)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(artifact.read_text())["pass"] is False
    assert output.read_text() == "pass=false\n"


SCENARIO_CASES = [
    ("target_head_workflow_env_alias", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_job_env_alias", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_job_env_run", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_caller_input", [("caller.yml", "call", "pull_request_target")]),
    ("target_pull_merge_ref", [("ci.yml", "check", "pull_request_target")]),
    ("target_merge_commit_sha", [("ci.yml", "check", "pull_request_target")]),
    ("target_gh_pr_checkout", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_workflow_defaults", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_job_defaults", [("ci.yml", "check", "pull_request_target")]),
    ("target_pr_number_only", []),
    ("target_event_number_only", []),
    ("reusable_mixed_token_input", [("callee.yml", "check", "secrets")]),
    ("target_head_condition", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_privileged_allowlisted", [("ci.yml", "check", "pull_request_target")]),
    ("reusable_unreachable", []),
    ("reusable_nested_ungranted", []),
    ("reusable_context_token", []),
    ("reusable_not_callable", [("caller.yml", "call", "runner")]),
    ("reusable_yaml_extension", [("callee.yaml", "check", "runner")]),
    ("reusable_self_hosted", [("callee.yml", "check", "runner")]),
    ("reusable_runner_group", [("callee.yml", "check", "runner")]),
    ("reusable_hosted_readonly", []),
    ("reusable_disabled", []),
    ("reusable_permission_ceiling", []),
    (
        "reusable_inherited_write",
        [
            ("caller.yml", "call", "permissions"),
            ("caller.yml", "call", "reusable_workflow"),
            ("callee.yml", "check", "permissions"),
        ],
    ),
    (
        "reusable_inherited_oidc",
        [
            ("caller.yml", "call", "id-token"),
            ("caller.yml", "call", "reusable_workflow"),
            ("callee.yml", "check", "id-token"),
        ],
    ),
    (
        "reusable_secrets_explicit",
        [
            ("caller.yml", "call", "reusable_workflow"),
            ("caller.yml", "call", "secrets"),
            ("callee.yml", "check", "secrets"),
        ],
    ),
    (
        "reusable_secrets_inherit",
        [
            ("caller.yml", "call", "reusable_workflow"),
            ("caller.yml", "call", "secrets"),
            ("callee.yml", "check", "secrets"),
        ],
    ),
    ("reusable_secrets_absent", []),
    ("reusable_secrets_automatic", []),
    (
        "reusable_secrets_unrelated",
        [("caller.yml", "call", "reusable_workflow"), ("caller.yml", "call", "secrets")],
    ),
    ("reusable_environment_secret", [("callee.yml", "check", "secrets")]),
    ("reusable_protected_environment", []),
    ("reusable_nested_runner", [("leaf.yml", "check", "runner")]),
    ("reusable_cycle", [("callee.yml", "back", "runner")]),
    ("reusable_diamond", []),
    ("reusable_depth_10", []),
    ("reusable_depth_11", [("level10.yml", "call", "runner")]),
    ("reusable_target_pull_request_target", [("callee.yml", "check", "pull_request_target")]),
    ("reusable_target_issue_comment", [("callee.yml", "check", "pull_request_target")]),
    ("reusable_downstream_artifact", [("callee.yml", "check", "workflow_run")]),
    (
        "reusable_nested_secrets",
        [
            ("caller.yml", "call", "secrets"),
            ("caller.yml", "call", "reusable_workflow"),
            ("callee.yml", "call", "secrets"),
            ("callee.yml", "call", "reusable_workflow"),
            ("leaf.yml", "check", "secrets"),
        ],
    ),
    ("target_head_readonly", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_privileged", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_brackets", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_ref", [("ci.yml", "check", "pull_request_target")]),
    ("target_base_readonly", []),
    ("target_head_display", []),
    ("reusable_target_head", [("callee.yml", "check", "pull_request_target")]),
    ("downstream_script_download_artifact", [("down.yml", "check", "workflow_run")]),
    ("downstream_script_list_artifacts", [("down.yml", "check", "workflow_run")]),
    ("downstream_script_artifacts_path", [("down.yml", "check", "workflow_run")]),
    ("downstream_script_ordinary_script", []),
    ("downstream_pull_requests", [("down.yml", "check", "workflow_run")]),
    ("downstream_run_id", []),
    ("downstream_script_display", []),
    ("push_tags_only", []),
    ("workflow_name_path", [("down.yml", "check", "workflow_run")]),
    ("custom_runner", [("ci.yml", "check", "runner")]),
    ("custom_list", [("ci.yml", "check", "runner")]),
    ("mixed_runner", [("ci.yml", "check", "runner")]),
    ("missing_runner", [("ci.yml", "check", "runner")]),
    ("numeric_runner", [("ci.yml", "check", "runner")]),
    ("empty_runner", [("ci.yml", "check", "runner")]),
    ("hosted_windows", []),
    ("hosted_macos", []),
    ("hosted_list", []),
    ("event_create", [("ci.yml", "check", "secrets")]),
    ("event_merge_group", [("ci.yml", "check", "secrets")]),
    ("event_future_event", [("ci.yml", "check", "secrets")]),
    ("event_schedule", []),
    ("event_workflow_dispatch", []),
    ("event_issues", [("ci.yml", "check", "pull_request_target")]),
    ("event_release", []),
    ("event_delete", []),
    ("event_issue_comment", [("ci.yml", "check", "pull_request_target")]),
    ("unsafe_environment", [("ci.yml", "check", "secrets")]),
    ("expression_environment", [("ci.yml", "check", "secrets")]),
    ("downstream_head_sha", [("down.yml", "check", "workflow_run")]),
    ("downstream_head_branch", [("down.yml", "check", "workflow_run")]),
    ("downstream_gh_download", [("down.yml", "check", "workflow_run")]),
    ("downstream_artifact_api", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_download", [("down.yml", "check", "workflow_run")]),
    ("downstream_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_write", [("down.yml", "check", "workflow_run")]),
    ("chain", [("a-down.yml", "check", "workflow_run")]),
    ("unreachable_apply", [("ci.yml", "check", "agent-apply")]),
    (
        "all_jobs",
        [
            ("ci.yml", "private", "runner"),
            ("ci.yml", "secret", "secrets"),
            ("ci.yml", "write", "permissions"),
        ],
    ),
    ("private_ubuntu_gpu", [("ci.yml", "check", "runner")]),
    ("private_ubuntu_gpu_list", [("ci.yml", "check", "runner")]),
    ("private_ubuntu_internal", [("ci.yml", "check", "runner")]),
    ("private_ubuntu_internal_list", [("ci.yml", "check", "runner")]),
    ("private_windows_builder", [("ci.yml", "check", "runner")]),
    ("private_windows_builder_list", [("ci.yml", "check", "runner")]),
    ("private_macos_internal", [("ci.yml", "check", "runner")]),
    ("private_macos_internal_list", [("ci.yml", "check", "runner")]),
    ("published_ubuntu_latest", []),
    ("published_ubuntu_22_04", []),
    ("published_ubuntu_24_04", []),
    ("published_ubuntu_26_04", []),
    ("published_ubuntu_22_04_arm", []),
    ("published_ubuntu_24_04_arm", []),
    ("published_ubuntu_26_04_arm", []),
    ("published_ubuntu_slim", []),
    ("published_windows_latest", []),
    ("published_windows_2022", []),
    ("published_windows_2025", []),
    ("published_windows_2025_vs2026", []),
    ("published_windows_11_arm", []),
    ("published_windows_11_vs2026_arm", []),
    ("published_macos_latest", []),
    ("published_macos_14", []),
    ("published_macos_15", []),
    ("published_macos_26", []),
    ("published_macos_15_intel", []),
    ("published_macos_26_intel", []),
    ("published_macos_latest_large", []),
    ("published_macos_14_large", []),
    ("published_macos_15_large", []),
    ("published_macos_26_large", []),
    ("published_macos_latest_xlarge", []),
    ("published_macos_14_xlarge", []),
    ("published_macos_15_xlarge", []),
    ("published_macos_26_xlarge", []),
    ("published_xcode_27", []),
    ("published_xcode_27_xlarge", []),
    ("artifact_plain", []),
    ("artifact_display_name", []),
    ("artifact_echo_downloads", []),
    ("artifact_display_head", []),
    ("artifact_main", []),
    ("artifact_ignore_agent", []),
    ("artifact_agent", [("down.yml", "check", "workflow_run")]),
    ("artifact_ignore_main", [("down.yml", "check", "workflow_run")]),
    ("independent_repository_dispatch", []),
    ("independent_label", []),
    ("independent_milestone", []),
    ("independent_discussion", [("ci.yml", "check", "pull_request_target")]),
    ("independent_discussion_comment", [("ci.yml", "check", "pull_request_target")]),
    ("independent_gollum", []),
    ("independent_page_build", []),
    ("independent_watch", []),
    ("independent_fork", []),
    ("independent_public", []),
    ("independent_registry_package", []),
    ("independent_project", []),
    ("independent_project_card", []),
    ("independent_project_column", []),
    ("independent_branch_protection_rule", []),
    ("completion_merge_group", [("ci.yml", "check", "secrets")]),
    ("completion_check_suite", [("ci.yml", "check", "secrets")]),
    ("completion_check_run", [("ci.yml", "check", "secrets")]),
    ("completion_status", [("ci.yml", "check", "secrets")]),
    ("completion_deployment_status", [("ci.yml", "check", "secrets")]),
    ("disabled_apply", []),
    ("comment_base_only", [("ci.yml", "check", "pull_request_target")]),
    (
        "target_and_push",
        [("ci.yml", "check", "permissions"), ("ci.yml", "check", "pull_request_target")],
    ),
    (
        "target_pull_request_target_downstream_privileged",
        [
            ("consumer.yml", "consume", "workflow_run"),
            ("source.yml", "produce", "pull_request_target"),
        ],
    ),
    (
        "target_pull_request_target_downstream_readonly",
        [("source.yml", "produce", "pull_request_target")],
    ),
    ("target_issue_comment_downstream_privileged", [("consumer.yml", "consume", "workflow_run")]),
    ("target_issue_comment_downstream_readonly", []),
    ("workflow_name_brackets", [("down.yml", "check", "workflow_run")]),
    ("workflow_name_casefold", [("down.yml", "check", "workflow_run")]),
    ("workflow_name_glob", [("down.yml", "check", "workflow_run")]),
    (
        "target_pull_request_target_self_hosted",
        [("ci.yml", "check", "pull_request_target"), ("ci.yml", "check", "runner")],
    ),
    (
        "target_pull_request_target_runner_group",
        [("ci.yml", "check", "pull_request_target"), ("ci.yml", "check", "runner")],
    ),
    ("target_issue_comment_self_hosted", [("ci.yml", "check", "runner")]),
    ("target_issue_comment_runner_group", [("ci.yml", "check", "runner")]),
    ("pull_request_review_ignored_filter", [("ci.yml", "check", "permissions")]),
    ("pull_request_review_comment_ignored_filter", [("ci.yml", "check", "permissions")]),
    ("downstream_unknown_job_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_step_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_automatic_token", []),
    ("branch_target_pull_request_target_agent_excluded", []),
    ("branch_target_pull_request_target_main", [("down.yml", "check", "workflow_run")]),
    ("branch_target_pull_request_target_unfiltered", [("down.yml", "check", "workflow_run")]),
    ("branch_target_issue_comment_agent_excluded", []),
    ("branch_target_issue_comment_main", [("down.yml", "check", "workflow_run")]),
    ("branch_target_issue_comment_unfiltered", [("down.yml", "check", "workflow_run")]),
    ("branch_target_issues_agent_excluded", []),
    ("branch_target_discussion_agent_excluded", []),
    ("branch_target_discussion_comment_agent_excluded", []),
    ("workflow_name_glob_casefold", [("down.yml", "check", "workflow_run")]),
    ("scalar_branches", [("ci.yml", "check", "secrets")]),
    ("scalar_branches_ignore", [("ci.yml", "check", "secrets")]),
    ("scalar_workflow_names", [("down.yml", "check", "workflow_run")]),
    ("pr_release_artifact_source", [("deploy.yml", "check", "workflow_run")]),
    ("pr_develop_artifact_source", [("deploy.yml", "check", "workflow_run")]),
    ("pr_ignore_main_artifact_source", [("deploy.yml", "check", "workflow_run")]),
    ("protected_environment_check_suite", [("ci.yml", "check", "secrets")]),
    ("protected_environment_check_run", [("ci.yml", "check", "secrets")]),
    ("protected_environment_status", [("ci.yml", "check", "secrets")]),
    ("protected_environment_deployment_status", [("ci.yml", "check", "secrets")]),
    ("protected_environment_pull_request_comment", [("ci.yml", "check", "secrets")]),
    ("protected_environment_mixed_unknown", [("ci.yml", "check", "secrets")]),
    ("protected_environment_push_schedule", []),
    (
        "caller_privileged_write",
        [("caller.yml", "call", "permissions"), ("caller.yml", "call", "reusable_workflow")],
    ),
    (
        "caller_privileged_inherit",
        [("caller.yml", "call", "reusable_workflow"), ("caller.yml", "call", "secrets")],
    ),
    (
        "caller_privileged_secret",
        [("caller.yml", "call", "reusable_workflow"), ("caller.yml", "call", "secrets")],
    ),
    ("caller_unprivileged_local", [("callee.yml", "check", "runner")]),
    ("caller_unprivileged_remote", [("caller.yml", "call", "runner")]),
    ("caller_unprivileged_missing", [("caller.yml", "call", "runner")]),
    ("caller_readonly_automatic_token", [("callee.yml", "check", "runner")]),
    (
        "caller_downstream_write",
        [("caller.yml", "call", "reusable_workflow"), ("caller.yml", "call", "workflow_run")],
    ),
    (
        "target_reusable_caller_secret",
        [
            ("caller.yml", "call", "pull_request_target"),
            ("caller.yml", "call", "reusable_workflow"),
        ],
    ),
    (
        "target_reusable_caller_write",
        [
            ("caller.yml", "call", "pull_request_target"),
            ("caller.yml", "call", "reusable_workflow"),
        ],
    ),
]


@pytest.mark.parametrize(
    ("fixture", "expected"), SCENARIO_CASES, ids=[case[0] for case in SCENARIO_CASES]
)
def test_workflow_exposures(fixture, expected):
    module = implementation()
    workflows = load_yaml(FIXTURES / f"{fixture}.yml")
    result = module.analyze(
        workflows, module.PreflightContext("main", safe_environments=frozenset({"release"}))
    )
    assert sorted((item.workflow, item.job, item.rule) for item in result) == sorted(expected)


@pytest.mark.parametrize(
    "fixture",
    [
        "bad_permission_case",
        "bad_permission_type",
        "bad_permission_list",
        "empty_branches",
        "empty_ignore",
        "invalid_secondary_branches",
        "bad_name",
    ],
)
def test_invalid_workflow_values(fixture):
    module = implementation()
    with pytest.raises(ValueError):
        module.analyze(load_yaml(FIXTURES / f"{fixture}.yml"), module.PreflightContext("main"))


@pytest.mark.parametrize(
    "case",
    [
        "missing_local",
        "extra_local",
        "unexpected_error",
        "output_symlink",
        "output_rewrite",
        "listing_999",
        "listing_1000",
        "duplicate_entry",
        "slash_entry",
        "workflow_symlink",
    ],
)
def test_command_binding_and_output(case, tmp_path, monkeypatch, capsys):
    command = importlib.import_module("agent_repo.commands.preflight")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(FIXTURES / f"command_{case}.yml", workflows / "ci.yml")
    entries = None
    if case in {"listing_999", "listing_1000", "duplicate_entry", "slash_entry"}:
        data = (workflows / "ci.yml").read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        entries = [
            {"name": "ci.yml", "path": ".github/workflows/ci.yml", "sha": blob, "type": "file"}
        ]
        if case.startswith("listing_"):
            entries += [
                {
                    "name": f"note-{index}.txt",
                    "path": f".github/workflows/note-{index}.txt",
                    "sha": "d" * 40,
                    "type": "file",
                }
                for index in range(int(case.split("_")[1]) - 1)
            ]
        elif case == "duplicate_entry":
            entries.append(dict(entries[0]))
        else:
            entries[0]["name"] = "nested/ci.yml"
    transport = api_transport(workflows, entries=entries)
    if case == "workflow_symlink":
        target_workflow = tmp_path / "original.yml"
        (workflows / "ci.yml").rename(target_workflow)
        (workflows / "ci.yml").symlink_to(target_workflow)
    monkeypatch.setattr(GitHub, "_urlopen", staticmethod(transport))
    if case == "missing_local":
        (workflows / "ci.yml").unlink()
    if case == "extra_local":
        shutil.copy(FIXTURES / f"command_{case}.yml", workflows / "extra.yml")
    if case == "unexpected_error":

        def fail_yaml(path):
            raise RecursionError("credential-canary")

        monkeypatch.setattr(command, "load_yaml", fail_yaml)
    monkeypatch.setenv("GH_TOKEN", "credential-canary")
    monkeypatch.setenv("GITHUB_REPOSITORY", "your-org/project")
    output = tmp_path / "preflight.json"
    target = tmp_path / "target"
    if case == "output_symlink":
        target.write_text("preserve")
        output.symlink_to(target)
    if case == "output_rewrite":
        output.write_text("old result")
        original_read = Path.read_text

        def refuse_output_read(path, *args, **kwargs):
            if path == output:
                raise PermissionError("credential-canary")
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", refuse_output_read)
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    result = cli(
        [
            "preflight",
            "--default-branch",
            "main",
            "--workflows-dir",
            str(workflows),
            "--output",
            str(output),
        ]
    )
    success = case in {"output_rewrite", "listing_999"}
    assert result == (0 if success else 1)
    captured = capsys.readouterr()
    assert "credential-canary" not in captured.out + captured.err
    if case == "output_symlink":
        assert target.read_text() == "preserve"
        assert github_output.read_text() == "pass=false\n"
    else:
        with output.open() as handle:
            artifact = json.load(handle)
        assert artifact["pass"] is success
        assert github_output.read_text() == f"pass={str(success).lower()}\n"
        assert "credential-canary" not in json.dumps(artifact)


def test_action_forwards_verified_settings(tmp_path, monkeypatch):
    action_path = Path("actions/preflight/action.yml").resolve()
    step = load_yaml(action_path)["runs"]["steps"][0]
    binary = tmp_path / "python3"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = {
        "ACTION_PATH": str(action_path.parent),
        "DEFAULT_BRANCH": "main",
        "WORKFLOWS_DIR": "consumer workflows",
        "OUTPUT": "preflight.json",
        "TOKEN_READONLY": "true",
        "SAFE_ENVIRONMENTS": "release\nstaging",
        "RUN_ID": "123",
        "BASE_SHA": "c" * 40,
    }
    for key, value in settings.items():
        monkeypatch.setenv(f"PREFLIGHT_{key}", value)
    result = subprocess.run(["/bin/bash", "-e", "-c", step["run"]], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        str(action_path.parent) + "/../../lib/agent_repo_cli.py",
        "preflight",
        "--default-branch",
        "main",
        "--workflows-dir",
        "consumer workflows",
        "--output",
        "preflight.json",
        "--run-id",
        "123",
        "--base-sha",
        "c" * 40,
        "--token-readonly",
        "--safe-environment",
        "release",
        "--safe-environment",
        "staging",
    ]


SOURCE_CASES = [
    ("branch_recursive_agent_ignore_segment", [("ci.yml", "check", "permissions")], "agent/**"),
    ("branch_wildcard_intersection", [("ci.yml", "check", "permissions")], "agent/**"),
    ("branch_custom_agent", [("a-final.yml", "check", "workflow_run")], "bots/fix-*"),
    ("branch_custom_excluded", [], "bots/fix-*"),
    ("branch_ignore_exact_agent", [], "agent/fix-*"),
    ("branch_agent_included", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_second_hop_main", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_second_hop_agent", [], "agent/fix-*"),
    ("branch_second_hop_ignore_main", [], "agent/fix-*"),
    ("branch_queue", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_queue_excluded", [], "agent/fix-*"),
    ("branch_unknown_event", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_mixed_sources", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_fork_pull_request_main", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    (
        "branch_fork_pull_request_ignore_agent",
        [("a-final.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
    (
        "branch_fork_pull_request_review_main",
        [("a-final.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
    (
        "branch_fork_pull_request_review_ignore_agent",
        [("a-final.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
    (
        "branch_fork_pull_request_review_comment_main",
        [("a-final.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
    (
        "branch_fork_pull_request_review_comment_ignore_agent",
        [("a-final.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
]


@pytest.mark.parametrize(
    ("fixture", "expected", "agent_ref_glob"), SOURCE_CASES, ids=[case[0] for case in SOURCE_CASES]
)
def test_source_provenance(fixture, expected, agent_ref_glob):
    module = implementation()
    workflows = load_yaml(FIXTURES / f"{fixture}.yml")
    result = module.analyze(workflows, module.PreflightContext("main", agent_ref_glob))
    assert [(item.workflow, item.job, item.rule) for item in result] == [
        tuple(item) for item in expected
    ]


TARGET_CASES = [
    ("target_opaque_action_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_opaque_action_readonly", [], False),
    ("target_local_action_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_local_action_readonly", [], False),
    ("target_timeout_wrapper_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_timeout_wrapper_readonly", [], False),
    ("target_nice_wrapper_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_nice_wrapper_readonly", [], False),
    ("target_xargs_wrapper_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_xargs_wrapper_readonly", [], False),
    ("target_python_shell_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_python_shell_readonly", [], False),
    ("target_powershell_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_powershell_readonly", [], False),
    ("target_inline_interpreter_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_inline_interpreter_readonly", [], False),
    ("target_container_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_container_readonly", [], False),
    ("target_contents_api_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_contents_api_readonly", [], False),
    ("target_tree_api_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_tree_api_readonly", [], False),
    ("target_reaction_api_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_reaction_api_readonly", [], False),
    ("target_git_authorship_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_git_authorship_readonly", [], False),
    ("target_github_script_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_github_script_readonly", [], False),
    ("target_labeler_pull_request_target", [], False),
    ("target_labeler_issue_comment", [], False),
    ("target_first_interaction_pull_request_target", [], False),
    ("target_first_interaction_issue_comment", [], False),
    ("target_stale_pull_request_target", [], False),
    ("target_stale_issue_comment", [], False),
    ("target_tagged_action", [("ci.yml", "check", "pull_request_target")], False),
    ("target_short_pin", [("ci.yml", "check", "pull_request_target")], False),
    ("target_long_pin", [("ci.yml", "check", "pull_request_target")], False),
    ("target_nonhex_pin", [("ci.yml", "check", "pull_request_target")], False),
    ("target_action_subpath", [("ci.yml", "check", "pull_request_target")], False),
    ("target_different_owner", [("ci.yml", "check", "pull_request_target")], False),
    ("target_name_suffix", [("ci.yml", "check", "pull_request_target")], False),
    ("target_mixed_actions", [("ci.yml", "check", "pull_request_target")], False),
    ("target_mixed_run", [("ci.yml", "check", "pull_request_target")], False),
    ("target_run_and_uses", [("ci.yml", "check", "pull_request_target")], False),
    ("target_empty_steps", [("ci.yml", "check", "pull_request_target")], False),
    ("target_unknown_step", [("ci.yml", "check", "pull_request_target")], False),
    ("target_all_allowlisted", [], False),
    ("target_case_insensitive_pin", [], False),
    ("target_write_run", [("ci.yml", "check", "pull_request_target")], False),
    ("target_oidc_run", [("ci.yml", "check", "pull_request_target")], False),
    ("target_workflow_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_token_secret", [], False),
    ("target_bracket_token_secret", [], False),
    ("target_dynamic_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_with_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_run_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_staging_environment", [], False),
    ("target_expression_environment", [], False),
    ("target_protected_environment", [], False),
    ("target_allowlisted_environment", [], False),
    ("target_read_all_run", [], False),
    ("target_default_unknown", [("ci.yml", "check", "pull_request_target")], False),
    ("target_default_readonly", [("ci.yml", "check", "pull_request_target")], True),
    ("target_display_metadata", [("ci.yml", "check", "pull_request_target")], False),
    (
        "target_pull_request_target_container_env",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    (
        "target_pull_request_target_container_credentials",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_pull_request_target_service_env", [("ci.yml", "check", "pull_request_target")], False),
    (
        "target_pull_request_target_service_credentials",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_pull_request_target_container_public", [], False),
    (
        "target_pull_request_target_secret_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    (
        "target_pull_request_target_bracket_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_pull_request_target_context_token_readonly", [], False),
    (
        "target_pull_request_target_context_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issue_comment_container_env", [("ci.yml", "check", "pull_request_target")], False),
    (
        "target_issue_comment_container_credentials",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issue_comment_service_env", [("ci.yml", "check", "pull_request_target")], False),
    (
        "target_issue_comment_service_credentials",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issue_comment_container_public", [], False),
    ("target_issue_comment_secret_token_readonly", [], False),
    (
        "target_issue_comment_secret_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issue_comment_bracket_token_readonly", [], False),
    (
        "target_issue_comment_bracket_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issue_comment_context_token_readonly", [], False),
    (
        "target_issue_comment_context_token_write",
        [("ci.yml", "check", "pull_request_target")],
        False,
    ),
    ("target_issues_privileged", [("ci.yml", "check", "pull_request_target")], False),
    ("target_issues_labeler", [], False),
    ("target_discussion_privileged", [("ci.yml", "check", "pull_request_target")], False),
    ("target_discussion_labeler", [], False),
    ("target_discussion_comment_privileged", [("ci.yml", "check", "pull_request_target")], False),
    ("target_discussion_comment_labeler", [], False),
    ("target_environment_secret_reference", [("ci.yml", "check", "pull_request_target")], False),
    ("target_unknown_job_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_unknown_step_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_unknown_automatic_token", [], False),
    ("target_labeler_service_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_labeler_container_credentials", [("ci.yml", "check", "pull_request_target")], False),
    ("target_labeler_service_write", [("ci.yml", "check", "pull_request_target")], False),
    ("target_labeler_container_write", [("ci.yml", "check", "pull_request_target")], False),
    ("comment_default_readonly", [], True),
    ("target_implicit_context_token", [("ci.yml", "check", "pull_request_target")], True),
    ("target_explicit_workflow_readonly", [], True),
    ("target_explicit_job_readonly", [], True),
    ("target_explicit_read_all", [], True),
]


@pytest.mark.parametrize(
    ("fixture", "expected", "token_readonly"), TARGET_CASES, ids=[case[0] for case in TARGET_CASES]
)
def test_target_event_privileges(fixture, expected, token_readonly):
    module = implementation()
    result = module.analyze(
        load_yaml(FIXTURES / f"{fixture}.yml"),
        module.PreflightContext(
            "main", token_readonly=token_readonly, safe_environments=frozenset({"release"})
        ),
    )
    assert [(item.workflow, item.job, item.rule) for item in result] == expected
    for item in result:
        assert item.job in item.detail
        assert "fix.accept_ci_exposure" in item.detail
