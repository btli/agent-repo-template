import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.workflow_lint import lint

FIXTURES = Path("tests/fixtures/workflows")


@pytest.mark.parametrize(
    ("filename", "rule"),
    [
        ("bad_a.yml", "model job trigger"),
        ("bad_b.yml", "cache write"),
        ("bad_b_setup.yml", "cache write"),
        ("bad_c.yml", "apply job model action"),
        ("bad_c_setup_cache.yml", "cache restore"),
        ("bad_d.yml", "agent-apply environment"),
        ("bad_d_map.yml", "agent-apply environment"),
        ("bad_e.yml", "SHA-pinned"),
        ("bad_e_flow.yml", "SHA-pinned"),
        ("bad_e_job.yml", "SHA-pinned"),
        ("bad_f.yml", "event expression in run"),
        ("bad_f_format.yml", "event expression in run"),
        ("bad_f_tojson.yml", "event expression in run"),
        ("bad_f_index.yml", "event expression in run"),
    ],
)
def test_lint_reports_each_workflow_rule(filename: str, rule: str):
    errors = lint([FIXTURES / filename])

    assert errors
    assert any(rule in error for error in errors)


def test_lint_accepts_rendered_consumer_workflows(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("a" * 40, "v0.0.0", temporary_parent=tmp_path)
    workflows = sorted((rendered / "consumer/.github/workflows").glob("*.yml"))

    assert {path.name for path in workflows} == {"agent-review.yml", "agent-triage.yml"}
    assert lint(workflows) == []
