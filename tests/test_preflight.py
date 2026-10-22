import hashlib
import importlib
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from agent_repo.cli import main as cli
from agent_repo.github import GitHub
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml

FIXTURES = Path(__file__).parent / "fixtures" / "preflight"


def implementation():
    return importlib.import_module("agent_repo.preflight")


CASES = [
    ("agent_apply_pull_request", "agent-apply"),
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
    ("secret_inherit", "agent-apply,secrets,reusable_workflow,runner"),
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
    assert downstream == (["workflow_run"] if fixture != "workflow_run_unreachable" else [])


def test_malformed_workflow_fails_closed():
    module = implementation()
    with pytest.raises(ValueError):
        module.analyze(
            {"bad.yml": load_yaml(FIXTURES / "malformed.yml")}, module.PreflightContext("main")
        )


def api_transport(
    workflows, *, error=False, sha="a" * 40, entries=None, ref_path="/git/ref/heads/main"
):
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
        if "/git/ref/heads/" in parsed.path:
            assert parsed.path.endswith(ref_path)
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
    assert artifact["schema_version"] == "1"
    assert artifact["accepted_template_exposures"] == []
    assert validate(artifact, load_schema("preflight")) == []
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
    ("agent_apply_case", [("ci.yml", "check", "agent-apply")]),
    ("agent_apply_case_object", [("ci.yml", "check", "agent-apply")]),
    ("agent_apply_expression", [("ci.yml", "check", "agent-apply")]),
    ("agent_apply_expression_object", [("ci.yml", "check", "agent-apply")]),
    ("agent_apply_expression_unknown", [("ci.yml", "check", "agent-apply")]),
    ("agent_apply_local_push", [("callee.yml", "check", "agent-apply")]),
    ("agent_apply_local_pr", [("callee.yml", "check", "agent-apply")]),
    ("agent_apply_local_unreachable_push", [("callee.yml", "check", "agent-apply")]),
    ("agent_apply_local_unreachable_pr", [("callee.yml", "check", "agent-apply")]),
    ("agent_apply_local_missing", [("caller.yml", "call", "agent-apply")]),
    ("agent_apply_local_not_callable", [("caller.yml", "call", "agent-apply")]),
    ("agent_apply_local_cycle", [("callee.yml", "call", "agent-apply")]),
    ("agent_apply_local_nested", [("leaf.yml", "check", "agent-apply")]),
    ("agent_apply_staging", []),
    ("agent_apply_local_staging", []),
    ("agent_apply_local_disabled_call", []),
    ("agent_apply_local_disabled_job", []),
    ("agent_apply_uninvoked", []),
    ("reusable_nested_inherit_ungranted", []),
    ("target_github_head_ref_env", [("a.yml", "check", "pull_request_target")]),
    ("target_github_base_ref_env", []),
    ("downstream_python_urllib", [("consumer.yml", "check", "workflow_run")]),
    ("target_delete_secret", [("ci.yml", "check", "pull_request_target")]),
    ("target_create_privileged", [("ci.yml", "check", "pull_request_target")]),
    ("target_unknown_secret", [("ci.yml", "check", "pull_request_target")]),
    ("safe_schedule_privileged", []),
    ("safe_workflow_dispatch_privileged", []),
    ("target_unknown_labeler", []),
    ("target_unknown_runner", [("ci.yml", "check", "runner")]),
    ("downstream_script_archive_url", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_node_fetch", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_plain_run", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_opaque_action", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_allowlisted_action", []),
    ("downstream_allowlisted_run", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_unpinned_labeler", [("consumer.yml", "check", "workflow_run")]),
    ("downstream_empty_steps", []),
    ("downstream_unreachable_source", []),
    ("target_api_resolved_head", [("ci.yml", "check", "pull_request_target")]),
    ("target_pulls_api_lookup", [("ci.yml", "check", "pull_request_target")]),
    ("target_json_head_sha", [("ci.yml", "check", "pull_request_target")]),
    ("target_json_head_ref", [("ci.yml", "check", "pull_request_target")]),
    ("target_repository_dispatch_payload", [("ci.yml", "check", "pull_request_target")]),
    ("target_label_payload", [("ci.yml", "check", "pull_request_target")]),
    ("target_milestone_payload", [("ci.yml", "check", "pull_request_target")]),
    ("target_release_payload", [("ci.yml", "check", "pull_request_target")]),
    ("target_repository_dispatch_labeler", []),
    ("expression_quoted_env", [("ci.yml", "check", "secrets")]),
    ("expression_format_run", [("ci.yml", "check", "secrets")]),
    ("expression_escaped_quote_with", [("ci.yml", "check", "secrets")]),
    ("expression_multiline", [("ci.yml", "check", "secrets")]),
    ("expression_unterminated", [("ci.yml", "check", "secrets")]),
    ("expression_multiple", [("ci.yml", "check", "secrets")]),
    ("expression_automatic_token", []),
    ("expression_closed_boundary", []),
    (
        "expression_reusable_grant",
        [
            ("caller.yml", "call", "reusable_workflow"),
            ("caller.yml", "call", "secrets"),
            ("callee.yml", "check", "secrets"),
        ],
    ),
    ("expression_reusable_automatic_token", []),
    ("expression_target_head", [("ci.yml", "check", "pull_request_target")]),
    ("target_pull_head_refspec", [("ci.yml", "check", "pull_request_target")]),
    ("target_pull_merge_refspec", [("ci.yml", "check", "pull_request_target")]),
    ("target_head_strategy", [("ci.yml", "check", "pull_request_target")]),
    (
        "target_head_all_job_outputs",
        [("ci.yml", "check", "pull_request_target"), ("ci.yml", "meta", "pull_request_target")],
    ),
    ("target_comment_pr_checkout", [("ci.yml", "check", "pull_request_target")]),
    ("target_pr_url_comment", []),
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
    (
        "reusable_not_callable",
        [("caller.yml", "call", "agent-apply"), ("caller.yml", "call", "runner")],
    ),
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
    ("reusable_cycle", [("callee.yml", "back", "agent-apply"), ("callee.yml", "back", "runner")]),
    ("reusable_diamond", []),
    ("reusable_depth_10", []),
    (
        "reusable_depth_11",
        [("level10.yml", "call", "agent-apply"), ("level10.yml", "call", "runner")],
    ),
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
    ("downstream_script_ordinary_script", [("down.yml", "check", "workflow_run")]),
    ("downstream_pull_requests", [("down.yml", "check", "workflow_run")]),
    ("downstream_run_id", [("down.yml", "check", "workflow_run")]),
    ("downstream_script_display", [("down.yml", "check", "workflow_run")]),
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
    ("event_create", [("ci.yml", "check", "pull_request_target")]),
    ("event_merge_group", [("ci.yml", "check", "secrets")]),
    ("event_future_event", [("ci.yml", "check", "pull_request_target")]),
    ("event_schedule", []),
    ("event_workflow_dispatch", []),
    ("event_issues", [("ci.yml", "check", "pull_request_target")]),
    ("event_release", [("ci.yml", "check", "pull_request_target")]),
    ("event_delete", [("ci.yml", "check", "pull_request_target")]),
    ("event_issue_comment", [("ci.yml", "check", "pull_request_target")]),
    ("unsafe_environment", [("ci.yml", "check", "secrets")]),
    (
        "expression_environment",
        [("ci.yml", "check", "agent-apply"), ("ci.yml", "check", "secrets")],
    ),
    ("downstream_head_sha", [("down.yml", "check", "workflow_run")]),
    ("downstream_head_branch", [("down.yml", "check", "workflow_run")]),
    ("downstream_gh_download", [("down.yml", "check", "workflow_run")]),
    ("downstream_artifact_api", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_download", [("down.yml", "check", "workflow_run")]),
    ("downstream_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_write", [("down.yml", "check", "workflow_run")]),
    ("chain", [("a-down.yml", "check", "workflow_run"), ("b-mid.yml", "check", "workflow_run")]),
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
    ("artifact_plain", [("down.yml", "check", "workflow_run")]),
    ("artifact_display_name", [("down.yml", "check", "workflow_run")]),
    ("artifact_echo_downloads", [("down.yml", "check", "workflow_run")]),
    ("artifact_display_head", [("down.yml", "check", "workflow_run")]),
    ("artifact_main", []),
    ("artifact_ignore_agent", []),
    ("artifact_agent", [("down.yml", "check", "workflow_run")]),
    ("artifact_ignore_main", [("down.yml", "check", "workflow_run")]),
    ("independent_repository_dispatch", [("ci.yml", "check", "pull_request_target")]),
    ("independent_label", [("ci.yml", "check", "pull_request_target")]),
    ("independent_milestone", [("ci.yml", "check", "pull_request_target")]),
    ("independent_discussion", [("ci.yml", "check", "pull_request_target")]),
    ("independent_discussion_comment", [("ci.yml", "check", "pull_request_target")]),
    ("independent_gollum", [("ci.yml", "check", "pull_request_target")]),
    ("independent_page_build", [("ci.yml", "check", "pull_request_target")]),
    ("independent_watch", [("ci.yml", "check", "pull_request_target")]),
    ("independent_fork", [("ci.yml", "check", "pull_request_target")]),
    ("independent_public", [("ci.yml", "check", "pull_request_target")]),
    ("independent_registry_package", [("ci.yml", "check", "pull_request_target")]),
    ("independent_project", [("ci.yml", "check", "pull_request_target")]),
    ("independent_project_card", [("ci.yml", "check", "pull_request_target")]),
    ("independent_project_column", [("ci.yml", "check", "pull_request_target")]),
    ("independent_branch_protection_rule", [("ci.yml", "check", "pull_request_target")]),
    ("completion_merge_group", [("ci.yml", "check", "secrets")]),
    ("completion_check_suite", [("ci.yml", "check", "pull_request_target")]),
    ("completion_check_run", [("ci.yml", "check", "pull_request_target")]),
    ("completion_status", [("ci.yml", "check", "pull_request_target")]),
    ("completion_deployment_status", [("ci.yml", "check", "pull_request_target")]),
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
        [
            ("consumer.yml", "consume", "workflow_run"),
            ("source.yml", "produce", "pull_request_target"),
        ],
    ),
    (
        "target_issue_comment_downstream_privileged",
        [
            ("consumer.yml", "consume", "workflow_run"),
            ("source.yml", "produce", "pull_request_target"),
        ],
    ),
    (
        "target_issue_comment_downstream_readonly",
        [
            ("consumer.yml", "consume", "workflow_run"),
            ("source.yml", "produce", "pull_request_target"),
        ],
    ),
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
    (
        "target_issue_comment_self_hosted",
        [("ci.yml", "check", "pull_request_target"), ("ci.yml", "check", "runner")],
    ),
    (
        "target_issue_comment_runner_group",
        [("ci.yml", "check", "pull_request_target"), ("ci.yml", "check", "runner")],
    ),
    ("pull_request_review_ignored_filter", [("ci.yml", "check", "permissions")]),
    ("pull_request_review_comment_ignored_filter", [("ci.yml", "check", "permissions")]),
    ("downstream_unknown_job_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_step_secret", [("down.yml", "check", "workflow_run")]),
    ("downstream_unknown_automatic_token", [("down.yml", "check", "workflow_run")]),
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
    ("protected_environment_check_suite", [("ci.yml", "check", "pull_request_target")]),
    ("protected_environment_check_run", [("ci.yml", "check", "pull_request_target")]),
    ("protected_environment_status", [("ci.yml", "check", "pull_request_target")]),
    ("protected_environment_deployment_status", [("ci.yml", "check", "pull_request_target")]),
    ("protected_environment_pull_request_comment", [("ci.yml", "check", "pull_request_target")]),
    ("protected_environment_mixed_unknown", [("ci.yml", "check", "pull_request_target")]),
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
    (
        "caller_unprivileged_missing",
        [("caller.yml", "call", "agent-apply"), ("caller.yml", "call", "runner")],
    ),
    ("caller_readonly_automatic_token", [("callee.yml", "check", "runner")]),
    (
        "caller_downstream_write",
        [
            ("callee.yml", "check", "workflow_run"),
            ("caller.yml", "call", "reusable_workflow"),
            ("caller.yml", "call", "workflow_run"),
        ],
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
        workflows,
        module.PreflightContext(
            "main",
            token_readonly=fixture == "downstream_python_urllib",
            safe_environments=frozenset({"release"}),
        ),
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
        "listed_symlink",
        "branch_slash",
        "missing_token",
    ],
)
def test_command_binding_and_output(case, tmp_path, monkeypatch, capsys):
    command = importlib.import_module("agent_repo.commands.preflight")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(FIXTURES / f"command_{case}.yml", workflows / "ci.yml")
    entries = None
    if case in {"listing_999", "listing_1000", "duplicate_entry", "slash_entry", "listed_symlink"}:
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
        elif case == "listed_symlink":
            entries[0]["type"] = "symlink"
        else:
            entries[0]["name"] = "nested/ci.yml"
    transport = api_transport(
        workflows,
        entries=entries,
        ref_path="/git/ref/heads/release%2Fmain"
        if case == "branch_slash"
        else "/git/ref/heads/main",
    )
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
    if case == "missing_token":
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
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
            "release/main" if case == "branch_slash" else "main",
            "--workflows-dir",
            str(workflows),
            "--output",
            str(output),
        ]
    )
    success = case in {"output_rewrite", "listing_999", "branch_slash"}
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
    original_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = {
        "ACTION_PATH": str(action_path.parent),
        "ACTION_REF": "d" * 40,
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
        "--template-ref",
        "d" * 40,
        "--token-readonly",
        "--safe-environment",
        "release",
        "--safe-environment",
        "staging",
    ]
    assert step["env"]["PREFLIGHT_ACTION_REF"] == "${{ github.action_ref }}"
    monkeypatch.setenv("PATH", original_path)
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    workflow = (FIXTURES / "template_fix_propose.yml").read_text().replace(TEMPLATE_REF, "d" * 40)
    (workflows / "agent-fix.yml").write_text(workflow)
    monkeypatch.setattr(GitHub, "_urlopen", staticmethod(api_transport(workflows)))
    monkeypatch.setenv("GH_TOKEN", "credential-canary")
    monkeypatch.setenv("GITHUB_REPOSITORY", "your-org/project")
    arguments = result.stdout.splitlines()[2:]
    arguments[arguments.index("--workflows-dir") + 1] = str(workflows)
    arguments[arguments.index("--output") + 1] = str(tmp_path / "preflight.json")
    assert cli(["preflight", *arguments]) == 0
    artifact = json.loads((tmp_path / "preflight.json").read_text())
    assert artifact["pass"] is True
    assert artifact["accepted_template_exposures"] == [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]


SOURCE_CASES = [
    ("branch_custom_slash_ignore", [("ci.yml", "check", "permissions")], "bots/fix/*"),
    ("branch_recursive_agent_ignore_segment", [("ci.yml", "check", "permissions")], "agent/**"),
    ("branch_wildcard_intersection", [("ci.yml", "check", "permissions")], "agent/**"),
    ("branch_custom_agent", [("a-final.yml", "check", "workflow_run")], "bots/fix-*"),
    ("branch_custom_excluded", [], "bots/fix-*"),
    ("branch_ignore_exact_agent", [], "agent/fix-*"),
    ("branch_agent_included", [("a-final.yml", "check", "workflow_run")], "agent/fix-*"),
    (
        "branch_second_hop_main",
        [("a-final.yml", "check", "workflow_run"), ("b-mid.yml", "check", "workflow_run")],
        "agent/fix-*",
    ),
    ("branch_second_hop_agent", [("b-mid.yml", "check", "workflow_run")], "agent/fix-*"),
    ("branch_second_hop_ignore_main", [("b-mid.yml", "check", "workflow_run")], "agent/fix-*"),
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
    ("target_timeout_wrapper_readonly", [("ci.yml", "check", "pull_request_target")], False),
    ("target_nice_wrapper_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_nice_wrapper_readonly", [], False),
    ("target_xargs_wrapper_secret", [("ci.yml", "check", "pull_request_target")], False),
    ("target_xargs_wrapper_readonly", [("ci.yml", "check", "pull_request_target")], False),
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


TEMPLATE_REF = "43c2d3b58ce3ebccd38d9ee2a05464966b6a94da"


def fix_workflow():
    return load_yaml(FIXTURES / "template_fix_propose.yml")


def target_workflow(action, inputs):
    return {
        "on": "issue_comment",
        "permissions": {"contents": "write"},
        "jobs": {
            "check": {
                "runs-on": "ubuntu-24.04",
                "steps": [{"uses": f"{action}@{TEMPLATE_REF}", "with": inputs}],
            }
        },
    }


def scan_template(workflow, template_ref=TEMPLATE_REF):
    module = implementation()
    return module.analyze(
        {"agent-fix.yml": workflow},
        module.PreflightContext("main", template_ref=template_ref),
    )


@pytest.mark.parametrize(
    ("action", "inputs"),
    [
        ("btli/agent-repo-template/actions/gate", {}),
        ("btli/agent-repo-template/actions/setup", {}),
        ("BTLI/agent-repo-template/actions/preflight", {}),
        ("btli/agent-repo-template/actions/decide", {}),
        (
            "btli/agent-repo-template/actions/decide",
            {"default-branch": "${{ github.event.repository.default_branch }}"},
        ),
        ("btli/agent-repo-template/actions/triage-agent", {}),
        ("btli/agent-repo-template/actions/review-agent", {}),
        ("btli/agent-repo-template/actions/apply", {}),
        ("actions/checkout", {"persist-credentials": False}),
        ("actions/checkout", {"ref": "${{ github.sha }}", "persist-credentials": False}),
        (
            "actions/checkout",
            {"ref": "${{ github.event.repository.default_branch }}", "persist-credentials": False},
        ),
        ("actions/download-artifact", {}),
        ("actions/upload-artifact", {}),
        ("actions/create-github-app-token", {}),
    ],
)
def test_target_template_allowlist(action, inputs):
    module = implementation()
    workflow = target_workflow(action, inputs)
    assert module.analyze({"ci.yml": workflow}, module.PreflightContext("main")) == []
    workflow["jobs"]["check"]["steps"].append({"run": "echo ready"})
    assert [
        item.rule for item in module.analyze({"ci.yml": workflow}, module.PreflightContext("main"))
    ] == ["pull_request_target"]


@pytest.mark.parametrize(
    ("action", "inputs"),
    [
        ("btli/agent-repo-template/actions/decide", {"default-branch": "main"}),
        ("btli/agent-repo-template/actions/gate", {"ref": "${{ github.sha }}"}),
        ("btli/agent-repo-template/actions/review-agent", {"repository": "your-org/project"}),
        ("btli/agent-repo-template/actions/apply", {"token": "${{ secrets.OTHER_TOKEN }}"}),
        ("btli/agent-repo-template/actions/gate", {"github-token": "${{ github.token }}"}),
        ("btli/agent-repo-template/actions/fix-agent", {}),
        ("btli/other/actions/gate", {}),
        ("btli/AGENT-REPO-TEMPLATE/actions/gate", {}),
        ("actions/checkout", {"ref": "${{ github.head_ref }}", "persist-credentials": False}),
        ("actions/checkout", {"repository": "your-org/project", "persist-credentials": False}),
        ("actions/checkout", {}),
        ("actions/checkout", {"persist-credentials": "false"}),
        ("actions/checkout", {"ref": None, "persist-credentials": False}),
        ("actions/download-artifact", {"run-id": "123"}),
        ("actions/download-artifact", {"github-token": "${{ github.token }}"}),
    ],
)
def test_target_allowlist_rejects_overrides(action, inputs):
    assert [item.rule for item in scan_template(fix_workflow())] == ["template-fix-propose"]
    workflow = target_workflow(action, inputs)
    assert [item.rule for item in scan_template(workflow)] == ["pull_request_target"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extra-run",), {"run": "echo ready"}),
        (("steps", 3, "env"), {"AGENT_FIX_KEY": "${{ secrets.AGENT_FIX_KEY }}"}),
        (("steps", 0, "with", "ref"), "${{ github.head_ref }}"),
        (("steps", 1, "with", "ref"), "${{ needs.gate.outputs.head_ref }}"),
        (("steps", 0, "with", "repository"), "your-org/project"),
        (("steps", 4, "uses"), "btli/agent-repo-template/actions/fix-agent@v0.0.0"),
        (("steps", 2, "with", "run-id"), "123"),
        (("steps", 2, "with", "github-token"), "${{ github.token }}"),
        (("steps", 4, "env", "EXTRA"), "${{ secrets.EXTRA }}"),
        (("permissions", "contents"), "write"),
        (("permissions", "id-token"), "write"),
        (("steps", 4, "with", "token"), "${{ github.token }}"),
        (("steps", 4, "with", "agent-timeout"), 1800),
        (("steps", 1, "with", "fetch-depth"), 1),
        (("steps", 0, "with", "persist-credentials"), True),
        (("steps", 4, "with", "base-sha"), "${{ github.head_ref }}"),
        (("steps", 4, "with", "issue-file"), "${{ github.head_ref }}"),
        (("steps", 5, "with", "token"), "${{ secrets.GITHUB_TOKEN }}"),
        (("steps", 5, "with", "token"), "${{ github.token }}"),
        (("steps", 4, "with", "allowed-endpoints"), "example.com:443"),
        (("steps", 4, "if"), "false"),
        (("environment",), "other"),
        (("timeout-minutes",), 25),
        (("needs",), "other"),
        (("if",), "always()"),
        (("concurrency", "cancel-in-progress"), False),
        (("env",), {"AGENT_FIX_KEY": "${{ secrets.AGENT_FIX_KEY }}"}),
        (("container",), "alpine"),
    ],
)
def test_fix_rule_requires_canonical_job(path, value):
    workflow = fix_workflow()
    assert [item.rule for item in scan_template(workflow)] == ["template-fix-propose"]
    target = workflow["jobs"]["propose"]
    for key in path[:-1]:
        target = target[key]
    if path == ("extra-run",):
        target["steps"].append(value)
    else:
        target[path[-1]] = value
    result = scan_template(workflow)
    assert "template-fix-propose" not in {item.rule for item in result}
    assert "pull_request_target" in {item.rule for item in result}


@pytest.mark.parametrize("template_ref", [None, "", "v0.0.0", "a" * 40])
def test_fix_rule_requires_self_pin(template_ref):
    assert [item.rule for item in scan_template(fix_workflow())] == ["template-fix-propose"]
    assert [item.rule for item in scan_template(fix_workflow(), template_ref)] == [
        "pull_request_target"
    ]


def test_uniform_old_template_pin_is_ordinary_exposure():
    assert [item.rule for item in scan_template(fix_workflow())] == ["template-fix-propose"]
    workflow = load_yaml(FIXTURES / "template_fix_propose_old.yml")
    result = scan_template(workflow)
    assert [item.rule for item in result] == ["pull_request_target"]
    assert result[0].secrets == ("AGENT_FIX_KEY",)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("bracket_secret", ("NPM_TOKEN",)),
        ("bracket_numeric_secret", ("TOKEN_2",)),
        ("dynamic_secret", ("*",)),
        ("github_token", ()),
    ],
)
def test_exposure_secret_names(fixture, expected):
    module = implementation()
    workflow = load_yaml(FIXTURES / f"{fixture}.yml")
    workflow["permissions"] = {"contents": "write"}
    result = module.analyze({"ci.yml": workflow}, module.PreflightContext("main"))
    assert result
    assert all(item.secrets == expected for item in result)


@pytest.mark.parametrize(
    "schema_fault", [None, "rule", "accepted_rule", "extra", "version", "secret"]
)
def test_fix_command_validates_and_logs_accepted_exposure(
    schema_fault, tmp_path, monkeypatch, capsys
):
    command = importlib.import_module("agent_repo.commands.preflight")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(FIXTURES / "template_fix_propose.yml", workflows / "agent-fix.yml")
    monkeypatch.setattr(GitHub, "_urlopen", staticmethod(api_transport(workflows)))
    monkeypatch.setenv("GH_TOKEN", "credential-canary")
    monkeypatch.setenv("GITHUB_REPOSITORY", "your-org/project")
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    output = tmp_path / "preflight.json"
    args = [
        "preflight",
        "--default-branch",
        "main",
        "--workflows-dir",
        str(workflows),
        "--template-ref",
        TEMPLATE_REF,
        "--output",
        str(output),
        "--run-id",
        "123",
        "--base-sha",
        "c" * 40,
    ]
    assert cli(args) == 0
    artifact = json.loads(output.read_text())
    assert artifact["pass"] is True
    assert artifact["exposures"] == []
    assert artifact["accepted_template_exposures"] == [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    assert validate(artifact, load_schema("preflight")) == []
    captured = capsys.readouterr()
    assert "template-fix-propose" in captured.out + summary.read_text()
    assert "AGENT_FIX_KEY" in captured.out and "AGENT_FIX_KEY" in summary.read_text()
    assert (
        "credential-canary"
        not in captured.out + captured.err + output.read_text() + summary.read_text()
    )
    if schema_fault is not None:
        invalid = deepcopy(artifact)
        if schema_fault == "rule":
            invalid["exposures"] = [
                {
                    "workflow": "ci.yml",
                    "job": "propose",
                    "rule": "unknown",
                    "detail": "Refused.",
                    "secrets": ["AGENT_FIX_KEY"],
                }
            ]
        elif schema_fault == "accepted_rule":
            invalid["accepted_template_exposures"][0]["rule"] = "unknown"
        elif schema_fault == "extra":
            invalid["accepted_template_exposures"][0]["extra"] = "unexpected"
        elif schema_fault == "version":
            invalid["schema_version"] = "2"
        else:
            del invalid["accepted_template_exposures"][0]["secret"]
        assert validate(invalid, load_schema("preflight"))
        original_validate = command.validate
        monkeypatch.setattr(
            command, "validate", lambda instance, schema: original_validate(invalid, schema)
        )
        output.unlink()
        assert cli(args) == 1
        assert not output.exists()
        captured = capsys.readouterr()
        assert "pass=false" in captured.out
        assert "credential-canary" not in captured.out + captured.err
