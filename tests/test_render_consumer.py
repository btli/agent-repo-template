import sys
from pathlib import Path

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
