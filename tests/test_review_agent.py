import importlib.util
import json
import os
import shlex
from pathlib import Path

from agent_repo.yamljson import load_yaml


def test_review_agent_has_no_tools_and_deny_all_policy():
    agent_dir = Path("agents/review")
    config = load_yaml(agent_dir / "config.yaml")
    policy = config["guardrails"]["policies"]["deny_all_tools"]["function"]

    assert config["executor"]["config"]["harness"] == "claude-sdk"
    assert not (agent_dir / "tools").exists()
    assert policy["path"] == "agent_repo.policies.allow_only"
    assert policy["arguments"]["allowed"] == []
    assert "matching the supplied review schema" in (agent_dir / "prompt.md").read_text()


def test_consumer_review_workflow_has_separate_read_and_write_jobs():
    workflow = load_yaml(Path("consumer/.github/workflows/agent-review.yml"))

    assert set(workflow["on"]) == {"pull_request_target", "issue_comment"}
    assert set(workflow["jobs"]) == {
        "agent-review-gate",
        "agent-propose-review",
        "agent-apply-review",
    }
    gate_job = workflow["jobs"]["agent-review-gate"]
    assert "concurrency" not in gate_job
    assert workflow["jobs"]["agent-propose-review"]["needs"] == "agent-review-gate"
    assert (
        workflow["jobs"]["agent-propose-review"]["if"]
        == "${{ needs.agent-review-gate.outputs.run == 'true' }}"
    )
    propose_job = workflow["jobs"]["agent-propose-review"]
    concurrency_context = "${{ github.event.pull_request.number || github.event.issue.number }}"
    assert propose_job["concurrency"] == {
        "group": f"agent-review-{concurrency_context}",
        "cancel-in-progress": True,
    }
    assert propose_job["permissions"]["pull-requests"] == "read"
    assert workflow["jobs"]["agent-propose-review"]["environment"] == "agent-propose-review"
    apply_job = workflow["jobs"]["agent-apply-review"]
    assert apply_job["permissions"] == {"actions": "read", "contents": "read"}
    assert apply_job["environment"] == "agent-apply"
    assert "concurrency" not in apply_job
    assert "issues" not in apply_job["permissions"]
    propose_action = next(
        step
        for step in workflow["jobs"]["agent-propose-review"]["steps"]
        if step.get("name") == "Propose review"
    )
    assert propose_action["with"]["expected-head-sha"] == (
        "${{ needs.agent-review-gate.outputs.head_sha }}"
    )
    assert propose_action["with"]["canary"] == "${{ needs.agent-review-gate.outputs.canary }}"
    token_step = next(step for step in apply_job["steps"] if step.get("id") == "app-token")
    assert token_step["uses"].startswith("actions/create-github-app-token@")
    permission_inputs = {
        key.removeprefix("permission-"): value
        for key, value in token_step["with"].items()
        if key.startswith("permission-")
    }
    assert permission_inputs == {"contents": "read", "pull-requests": "write"}
    assert token_step["with"]["client-id"] == "${{ vars.AGENT_APP_CLIENT_ID }}"
    assert token_step["with"]["private-key"] == "${{ secrets.AGENT_APP_PRIVATE_KEY }}"
    assert token_step["uses"] == (
        "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
    )
    assert (
        "uses: actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0"
        in Path("consumer/.github/workflows/agent-review.yml").read_text()
    )
    assert (
        "uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2"
        in Path("consumer/.github/workflows/agent-review.yml").read_text()
    )
    apply_step = next(step for step in apply_job["steps"] if step.get("name") == "Apply review")
    assert apply_step["with"]["token"] == "${{ steps.app-token.outputs.token }}"
    assert (
        apply_step["with"]["expected-head-sha"]
        == "${{ needs.agent-propose-review.outputs.head_sha }}"
    )
    assert "files-file" in propose_action["with"]
    artifact_step = next(
        step
        for step in workflow["jobs"]["agent-propose-review"]["steps"]
        if step.get("name") == "Share review inputs with the apply job"
    )
    assert "files.json" not in artifact_step["with"]["path"]
    assert "review.json" in artifact_step["with"]["path"]
    assert "files-file" not in apply_step["with"]
    review_action = load_yaml(Path("actions/review-agent/action.yml"))
    assert review_action["inputs"]["files-file"]["required"] is False
    assert review_action["inputs"]["expected-head-sha"]["required"] is True
    assert any(
        step.get("env", {}).get("EXPECTED_HEAD_SHA") == "${{ inputs.expected-head-sha }}"
        for step in review_action["runs"]["steps"]
    )
    propose_run = next(
        step for step in review_action["runs"]["steps"] if step.get("name") == "Run review agent"
    )
    assert propose_run["env"]["AGENT_CANARY"] == "${{ inputs.canary }}"
    assert "issues: write" not in Path("consumer/.github/workflows/agent-review.yml").read_text()


def test_propose_policy_checkout_is_pinned_to_event_sha():
    workflow = load_yaml(Path("consumer/.github/workflows/agent-review.yml"))
    steps = workflow["jobs"]["agent-propose-review"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout review policy")
    propose = next(step for step in steps if step.get("name") == "Propose review")

    assert checkout["uses"] == ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["path"] == "agent-review-policy"
    assert checkout["with"]["sparse-checkout"] == ".agents"
    assert checkout["with"]["persist-credentials"] is False
    assert propose["with"]["config-dir"] == ("${{ github.workspace }}/agent-review-policy/.agents")


def _review_run_module():
    module_spec = importlib.util.spec_from_file_location(
        "review_agent_run", "actions/review-agent/run.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _prepare_review_run(tmp_path, monkeypatch, fake_body):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / "omnigent"
    executable.write_text("#!/bin/sh\n" + fake_body + "\n")
    executable.chmod(0o755)
    timeout = binary_dir / "timeout"
    timeout.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    timeout.chmod(0o755)
    files = tmp_path / "files.json"
    files.write_text(
        json.dumps(
            [
                {
                    "filename": "file.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                    "patch": "@@ -0,0 +1 @@\n+added",
                }
            ]
        )
    )
    output = tmp_path / "review.json"
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    for key, value in {
        "OUTPUT_FILE": str(output),
        "RUN_ID": "run-1",
        "BASE_SHA": "a" * 40,
        "HEAD_SHA": "b" * 40,
        "AGENT_CANARY": "gate-canary-0123456789abcdef",
        "SUMMARY_ONLY": "false",
        "FILES_FILE": str(files),
        "REVIEW_AGENT": str(Path("agents/review").resolve()),
        "LLM_API_KEY": "test-secret-key",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENT_SCAN_KEYS", raising=False)
    monkeypatch.delenv("SUMMARY_REASON", raising=False)
    return _review_run_module(), output


def test_review_agent_rejects_leaked_key_without_artifact(tmp_path, monkeypatch, capsys):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "Review summary.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    encoded = json.dumps(artifact)
    output = "```json\n" + encoded + "\n```"
    body = "printf '%s\\n' \"$LLM_API_KEY\" >&2\n" + "printf '%s\\n' " + shlex.quote(output)
    module, output = _prepare_review_run(tmp_path, monkeypatch, body)

    assert module.main() == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert "test-secret-key" not in captured.out + captured.err


def test_review_agent_rejects_wrong_head_without_artifact(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "c" * 40,
        "summary": "Review summary.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    result = "```json\n" + json.dumps(artifact) + "\n```"
    module, output = _prepare_review_run(
        tmp_path, monkeypatch, "printf '%s\\n' " + shlex.quote(result)
    )

    assert module.main() == 1
    assert not output.exists()


def test_review_agent_supplies_rendered_file_list_as_model_input(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "Review summary.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    response = "```json\n" + json.dumps(artifact) + "\n```"
    captured = tmp_path / "model-prompt.txt"
    body = (
        "printf '%s\\n' \"$*\" > "
        + shlex.quote(str(captured))
        + "\nprintf '%s\\n' "
        + shlex.quote(response)
    )
    module, output = _prepare_review_run(tmp_path, monkeypatch, body)

    assert module.main() == 0
    prompt = captured.read_text()
    assert '"filename": "file.py"' in prompt
    assert '"patch": "@@ -0,0 +1 @@\\n+added"' in prompt
    assert "gate-canary-0123456789abcdef" in prompt
    assert output.exists()


def test_review_agent_rejects_gate_canary_leak_without_artifact(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "gate-canary-0123456789abcdef",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    response = "```json\n" + json.dumps(artifact) + "\n```"
    module, output = _prepare_review_run(
        tmp_path, monkeypatch, "printf '%s\\n' " + shlex.quote(response)
    )

    assert module.main() == 1
    assert not output.exists()


def test_review_agent_requires_gate_canary(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "Review summary.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    response = "```json\n" + json.dumps(artifact) + "\n```"
    module, output = _prepare_review_run(
        tmp_path, monkeypatch, "printf '%s\\n' " + shlex.quote(response)
    )
    monkeypatch.delenv("AGENT_CANARY")

    assert module.main() == 1
    assert not output.exists()


def test_review_agent_rejects_unrecognized_artifact_field(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "Review summary.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
        "event": "APPROVE",
    }
    response = "```json\n" + json.dumps(artifact) + "\n```"
    module, output = _prepare_review_run(
        tmp_path, monkeypatch, "printf '%s\\n' " + shlex.quote(response)
    )

    assert module.main() == 1
    assert not output.exists()


def test_review_agent_routes_scan_budget_review_to_summary_only(tmp_path, monkeypatch):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "x" * 4000,
        "findings": [
            {
                "severity": "low",
                "path": "src/" + "p" * 34 + ".py",
                "line": 1,
                "body": "x" * 1950,
            }
            for _ in range(30)
        ],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    response = "```json\n" + json.dumps(artifact) + "\n```"
    assert len(response) > 65536
    module, output = _prepare_review_run(
        tmp_path, monkeypatch, "printf '%s\\n' " + shlex.quote(response)
    )

    assert module.main() == 0

    written = json.loads(output.read_text())
    assert written["findings"] == []
    assert written["escalate"]["to"] == "maintainer"
    assert "needs-maintainer" in written["escalate"]["reason"]
    assert "x" * 100 not in written["summary"]


def test_summary_only_review_writes_schema_artifact_without_model(tmp_path, monkeypatch):
    module_spec = importlib.util.spec_from_file_location(
        "review_agent_run", "actions/review-agent/run.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    output = tmp_path / "review.json"
    monkeypatch.setenv("OUTPUT_FILE", str(output))
    monkeypatch.setenv("RUN_ID", "run-1")
    monkeypatch.setenv("BASE_SHA", "a" * 40)
    monkeypatch.setenv("HEAD_SHA", "b" * 40)
    monkeypatch.setenv("AGENT_CANARY", "gate-canary-0123456789abcdef")
    monkeypatch.setenv("SUMMARY_ONLY", "true")
    monkeypatch.setenv("SUMMARY_REASON", "Sensitive area: needs-maintainer.")

    assert module.main() == 0

    artifact = json.loads(output.read_text())
    assert artifact["head_sha"] == "b" * 40
    assert artifact["escalate"]["to"] == "maintainer"
    assert "needs-maintainer" in artifact["summary"]
    assert "number" not in artifact and "repo" not in artifact


def test_summary_only_review_requires_gate_canary(tmp_path, monkeypatch):
    module_spec = importlib.util.spec_from_file_location(
        "review_agent_run", "actions/review-agent/run.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    output = tmp_path / "review.json"
    monkeypatch.setenv("OUTPUT_FILE", str(output))
    monkeypatch.setenv("RUN_ID", "run-1")
    monkeypatch.setenv("BASE_SHA", "a" * 40)
    monkeypatch.setenv("HEAD_SHA", "b" * 40)
    monkeypatch.setenv("AGENT_CANARY", "")
    monkeypatch.setenv("SUMMARY_ONLY", "true")
    monkeypatch.setenv("SUMMARY_REASON", "Sensitive area: needs-maintainer.")

    assert module.main() == 1
    assert not output.exists()
