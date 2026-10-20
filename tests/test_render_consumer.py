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

    assert "__TEMPLATE_SHA__" not in workflow
    assert "__TEMPLATE_VERSION__" not in workflow
    assert "@" + "c" * 40 + " # v0.0.0" in workflow
    assert "name: agent-decision-${{ github.run_id }}" in workflow
    assert "retention-days: 30" in workflow
    assert "name: agent-handoff-tier1-${{ github.run_id }}" in workflow
