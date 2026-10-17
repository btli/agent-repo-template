import importlib.util
import json
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
    assert set(workflow["jobs"]) == {"agent-propose-review", "agent-apply-review"}
    assert workflow["jobs"]["agent-apply-review"]["permissions"]["pull-requests"] == "write"
    assert workflow["jobs"]["agent-propose-review"]["permissions"]["pull-requests"] == "read"


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
    monkeypatch.setenv("SUMMARY_ONLY", "true")
    monkeypatch.setenv("SUMMARY_REASON", "Sensitive area: needs-maintainer.")

    assert module.main() == 0

    artifact = json.loads(output.read_text())
    assert artifact["head_sha"] == "b" * 40
    assert artifact["escalate"]["to"] == "maintainer"
    assert "needs-maintainer" in artifact["summary"]
    assert "number" not in artifact and "repo" not in artifact
