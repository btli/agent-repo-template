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
    ("secret_inherit", "secrets"),
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
    ("pr_target_head", "pull_request_target"),
    ("pr_target_ref", "pull_request_target"),
    ("pr_target_base", None),
    ("pr_target_bracket", "pull_request_target"),
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


def api_transport(workflows, *, error=False, sha="a" * 40):
    entries = []
    for path in sorted(workflows.iterdir()):
        data = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        entries.append(
            {
                "name": path.name,
                "path": f".github/workflows/{path.name}",
                "sha": blob,
                "type": "file",
            }
        )

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
