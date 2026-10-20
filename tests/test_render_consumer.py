import re
import shutil
import sys
from pathlib import Path

import pytest
from agent_repo.charter import lint_charter
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_rendered_consumer_policy_is_valid_and_stays_in_temporary_directory(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("b" * 40, "v0.0.0", temporary_parent=tmp_path)
    consumer = rendered / "consumer"
    config = load_yaml(consumer / ".agents/config.yml")
    charter = (consumer / ".agents/charter.md").read_text()

    assert rendered.is_relative_to(tmp_path)
    assert not rendered.is_relative_to(Path.cwd() / ".github")
    assert validate(config, load_schema("config")) == []
    assert lint_charter(charter) == []


def test_rendered_workflow_has_no_template_tokens(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("c" * 40, "v0.0.0", temporary_parent=tmp_path)
    workflow = (rendered / "consumer/.github/workflows/agent-triage.yml").read_text()
    workflow_data = load_yaml(rendered / "consumer/.github/workflows/agent-triage.yml")
    assert "concurrency" not in workflow_data["jobs"]["apply"]
    assert not _secret_references(workflow_data.get("env", {}))
    assert not any(
        _secret_references(job.get("env", {})) or _secret_references(job.get("secrets", {}))
        for job in workflow_data["jobs"].values()
    )
    apply_steps = workflow_data["jobs"]["apply"]["steps"]
    apply_index = next(
        i for i, step in enumerate(apply_steps) if step.get("name") == "Apply triage decision"
    )
    record_index = next(
        i
        for i, step in enumerate(apply_steps)
        if step.get("name") == "Share applied decision record"
    )
    record_step = apply_steps[record_index]

    assert "__TEMPLATE_SHA__" not in workflow
    assert "__TEMPLATE_VERSION__" not in workflow
    assert "@" + "c" * 40 + " # v0.0.0" in workflow
    assert "name: agent-decision-${{ github.run_id }}" in workflow
    assert "retention-days: 30" in workflow
    assert "name: agent-handoff-tier1-${{ github.run_id }}" in workflow
    assert record_index > apply_index
    assert record_step["if"] == "success()"
    assert record_step["uses"].startswith("actions/upload-artifact@")
    assert record_step["with"]["name"] == "agent-decision-${{ github.run_id }}"
    assert record_step["with"]["path"].endswith("/decision.json")


def test_rendered_triage_workflow_pins_security_contract(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("d" * 40, "v0.0.0", temporary_parent=tmp_path)
    workflow = load_yaml(rendered / "consumer/.github/workflows/agent-triage.yml")
    jobs = workflow["jobs"]
    gate_steps = jobs["gate"]["steps"]
    gate_index = next(i for i, step in enumerate(gate_steps) if step.get("id") == "gate")
    marker_step = gate_steps[gate_index + 1]
    propose_steps = jobs["propose"]["steps"]
    tier2_steps = jobs["tier2"]["steps"]
    apply_steps = jobs["apply"]["steps"]

    assert not any(step.get("name") == "Share marker source" for step in gate_steps)

    assert gate_steps[0]["name"] == "Prepare triage marker source"
    assert marker_step["name"] == "Record triage cycle marker"
    assert marker_step["if"] == (
        "${{ steps.gate.outputs.run == 'true' && steps.gate.outputs.reason != 'agent-off' }}"
    )
    assert marker_step["with"]["name"] == "agent-triage-${{ github.event.issue.number }}"
    assert marker_step["with"]["retention-days"] == 90
    assert (
        next(
            step
            for step in gate_steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        == marker_step
    )
    assert jobs["propose"]["needs"] == "gate"
    assert not any("marker" in str(step.get("with", {}).get("name", "")) for step in propose_steps)
    assert all(
        step.get("name") not in {"Checkout triage policy", "Download triage context"}
        for step in propose_steps
    )
    assert jobs["propose"]["concurrency"]["cancel-in-progress"] is True
    assert "needs.propose.result != 'cancelled'" in jobs["apply"]["if"]

    expected_permissions = {
        "gate": {"actions": "read", "contents": "read", "issues": "read"},
        "propose": {"actions": "read", "contents": "read", "issues": "read"},
        "tier2": {},
        "apply": {},
    }
    assert {name: job.get("permissions") for name, job in jobs.items()} == expected_permissions
    assert all(
        "run" not in step
        for job_name in ("propose", "tier2", "apply")
        for step in jobs[job_name]["steps"]
    )

    secret_uses = {}
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            for value in [*step.get("env", {}).values(), *step.get("with", {}).values()]:
                for secret in re.findall(r"\$\{\{\s*secrets\.([A-Z0-9_]+)", str(value)):
                    secret_uses.setdefault(secret, []).append((job_name, step.get("name")))
    assert secret_uses == {
        "AGENT_SYSTEM_ONE_KEY": [("propose", "Build Tier-1 decision")],
        "AGENT_TRIAGE_KEY": [("tier2", "Run Tier-2 triage")],
        "AGENT_APP_PRIVATE_KEY": [("apply", "Create issue token")],
        "AGENT_SCAN_KEYS": [("apply", "Apply triage decision")],
    }

    token_step = next(step for step in apply_steps if step.get("id") == "app-token")
    assert token_step["with"]["permission-issues"] == "write"
    assert token_step["with"]["permission-contents"] == "read"
    decision_step = next(step for step in propose_steps if step.get("id") == "decide")
    assert decision_step["with"]["canary"] == "${{ needs.gate.outputs.canary }}"
    apply_decision = next(
        step for step in apply_steps if step.get("name") == "Apply triage decision"
    )
    assert apply_decision["with"]["canary"] == "${{ needs.gate.outputs.canary }}"
    assert apply_decision["with"]["scan-keys"] == "${{ secrets.AGENT_SCAN_KEYS }}"
    decision_record = next(
        step for step in apply_steps if step.get("name") == "Share applied decision record"
    )
    assert apply_steps.index(decision_record) > apply_steps.index(apply_decision)
    assert decision_record["if"] == "success()"
    assert decision_record["with"]["name"] == "agent-decision-${{ github.run_id }}"
    assert decision_record["with"]["retention-days"] == 30
    assert any(step.get("id") == "triage" for step in tier2_steps)


def _secret_references(value: object) -> list[str]:
    if isinstance(value, dict):
        return [secret for child in value.values() for secret in _secret_references(child)]
    if isinstance(value, list):
        return [secret for child in value for secret in _secret_references(child)]
    return re.findall(r"\$\{\{\s*secrets\.[A-Z0-9_]+", str(value))


def test_render_consumer_rejects_destination_inside_repository_root(monkeypatch):
    import tools.render_consumer as render_module
    from tools.render_consumer import ROOT, render_consumer

    rendered = None
    try:
        monkeypatch.setattr(render_module.tempfile, "gettempdir", lambda: str(ROOT))
        with pytest.raises(ValueError, match="repository root"):
            rendered = render_consumer("e" * 40, "v0.0.0", temporary_parent=ROOT / ".github")
    finally:
        if rendered is not None:
            shutil.rmtree(rendered)
