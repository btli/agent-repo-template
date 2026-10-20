import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from agent_repo.charter import lint_charter, parse_charter

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "evals"


def _load_eval():
    module_spec = importlib.util.spec_from_file_location("eval_script", ROOT / "scripts/eval.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


evals = _load_eval()


def test_accuracy_on_known_set():
    pairs = [("bug", "bug"), ("bug", "feature"), ("docs", "docs"), ("feature", "feature")]
    assert evals.accuracy(pairs) == 0.75
    assert evals.accuracy([]) == 0.0


def test_calibration_bins_ten_synthetic_points():
    points = [
        (0.05, False),
        (0.15, False),
        (0.12, True),
        (0.55, True),
        (0.58, False),
        (0.81, True),
        (0.85, True),
        (0.89, False),
        (0.95, True),
        (1.0, True),
    ]
    bins = evals.calibration(points)
    assert [(round(mean, 4), round(hit, 4), count) for mean, hit, count in bins] == [
        (0.05, 0.0, 1),
        (0.135, 0.5, 2),
        (0.565, 0.5, 2),
        (0.85, 0.6667, 3),
        (0.975, 1.0, 2),
    ]
    assert sum(count for _, _, count in evals.calibration(points, bins=2)) == 10


def test_suggest_apply_min_returns_lowest_p_meeting_precision():
    points = [(0.5, False), (0.6, True), (0.7, False), (0.8, True), (0.85, True), (0.9, True)]
    # From 0.6 up: 4 of 5 correct; from 0.7 up: 3 of 4; from 0.8 up: 3 of 3.
    assert evals.suggest_apply_min(points, precision=0.9) == 0.8
    assert evals.suggest_apply_min(points, precision=0.8) == 0.6
    assert evals.suggest_apply_min([(0.95, True), (0.99, False)], precision=0.9) is None
    assert evals.suggest_apply_min([]) is None


@pytest.mark.parametrize(
    ("agreements", "ready"),
    [
        ([True] * 19, False),
        ([True] * 19 + [False], True),
        ([True] * 18 + [False] * 2, False),
    ],
)
def test_close_enable_ready_needs_twenty_candidates_at_95_percent(agreements, ready):
    assert evals.close_enable_ready(agreements) is ready


@pytest.mark.parametrize(
    ("stats", "flag"),
    [
        ({"out_of_policy_actions": 0}, None),
        ({"out_of_policy_actions": 1}, "any out-of-policy action"),
        ({"type_pairs": 50, "type_agreement": 0.8, "area_pairs": 50, "area_agreement": 0.8}, None),
        (
            {"type_pairs": 50, "type_agreement": 0.79, "area_pairs": 50, "area_agreement": 0.9},
            "dry-run agreement on type or area under 80% after 50 issues",
        ),
        (
            {"type_pairs": 50, "type_agreement": 0.9, "area_pairs": 50, "area_agreement": 0.79},
            "dry-run agreement on type or area under 80% after 50 issues",
        ),
        ({"type_pairs": 49, "type_agreement": 0.5, "area_pairs": 49, "area_agreement": 0.5}, None),
        # Each question needs 50 compared issues of its own, not 50 decided issues.
        ({"type_pairs": 1, "type_agreement": 0.0, "area_pairs": 50, "area_agreement": 0.9}, None),
        ({"agent_closed": 100, "reopened": 5}, None),
        ({"agent_closed": 100, "reopened": 6}, "a reopen rate above 5% for `agent:closed`"),
        ({"tier2_budget_alerts_per_week": 1}, None),
        (
            {"tier2_budget_alerts_per_week": 2},
            "a Tier-2 key reaching 80% of its budget twice in one week. "
            "The response is to set `triage.rca_authors: known`.",
        ),
    ],
)
def test_abort_flags_at_each_spec_boundary(stats, flag):
    assert evals.abort_flags(stats) == ([flag] if flag else [])


def test_larkspur_policy_loads_and_covers_every_golden_label():
    config, charter = evals.load_policy(ROOT / "evals")
    assert config.raw["close"]["enabled"] is True
    assert {area.name for area in config.areas} == {"parser", "validation", "merge", "cli", "docs"}
    for document in evals.load_golden(ROOT / "evals").values():
        assert set(document["expected"]["labels"]) <= set(config.labels) | {"area:unknown"}
    assert charter.docs_index


@pytest.mark.parametrize(
    ("issue_id", "expected"),
    [
        ("system-one/out-of-scope-remote-store", "close"),
        # Tier 2 disagreed, or the close is below close_min: apply only suggests it.
        ("system-one/out-of-scope-gui-editor", "needs-maintainer"),
        ("system-one/out-of-scope-eol-python", "needs-maintainer"),
        ("llm-only/out-of-scope-remote-store", "needs-maintainer"),
        ("system-one/merge-order-env-wins-wrongly", "needs-maintainer"),
        ("system-one/schema-all-errors-blocked", "needs-maintainer"),
        ("system-one/security-env-secret-logged", "needs-maintainer"),
        ("llm-only/security-env-secret-logged", "needs-maintainer"),
        ("llm-only/merge-order-env-wins-wrongly", "needs-maintainer"),
        ("system-one/vague-it-broke", "needs-info"),
        ("system-one/cli-check-exit-code", "label"),
    ],
)
def test_outcome_mirrors_what_apply_would_do(issue_id, expected):
    decision = json.loads((FIXTURES / f"{issue_id}.json").read_text())
    assert evals.outcome(decision, *evals.load_policy(ROOT / "evals")) == expected


def test_golden_set_has_twenty_issues_and_a_lintable_charter():
    charter_text = (ROOT / "evals" / "charter.md").read_text()
    assert lint_charter(charter_text) == []
    charter = parse_charter(charter_text)
    assert all(item.doc for item in charter.out_of_scope)
    golden = evals.load_golden(ROOT / "evals")
    assert len(golden) >= 20
    outcomes = {document["expected"]["outcome"] for document in golden.values()}
    assert outcomes == {"label", "needs-info", "needs-maintainer", "close"}


def test_eval_scores_recorded_decisions_per_mode(capsys):
    assert (
        evals.main(
            ["--decisions", str(FIXTURES / "system-one"), "--decisions", str(FIXTURES / "llm-only")]
        )
        == 0
    )
    report = capsys.readouterr().out
    system_one, llm_only = (
        report.split("## Mode: system-one")[1],
        report.split("## Mode: llm-only")[1],
    )
    assert "| type | 78% | 9 | 0.9 |" in system_one
    # The Tier-2-disagreed and below-close_min closes do not score as closes.
    assert "| outcome | 78% | 9 | n/a |" in system_one
    # LLM-only never closes, so the out-of-scope issue scores as a maintainer suggestion.
    assert "| outcome | 100% | 4 | n/a |" in llm_only.split("## Mode:")[0]


def test_eval_rejects_a_decision_that_fails_the_schema(tmp_path, capsys):
    (tmp_path / "cli-check-exit-code.json").write_text('{"schema_version": "1"}')
    assert evals.main(["--decisions", str(tmp_path)]) == 2
    assert "does not match schema" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [("outcome", "archive"), ("area", "storage"), ("labels", ["type:bug", "area:cli", "P2", "x"])],
)
def test_eval_rejects_a_malformed_golden_issue(tmp_path, capsys, field, value):
    for name in ("charter.md", "config.yml", "areas.yml", "labels.yml"):
        shutil.copy(ROOT / "evals" / name, tmp_path / name)
    (tmp_path / "issues").mkdir()
    expected = {"labels": ["type:bug", "area:cli", "P2"], "area": "cli", "priority": "P2"}
    expected = {**expected, "outcome": "label", field: value}
    document = {"issue": {"title": "Broken", "body": "It fails."}, "expected": expected}
    (tmp_path / "issues" / "broken.yml").write_text(json.dumps(document))
    assert evals.main(["--evals", str(tmp_path), "--decisions", str(tmp_path / "issues")]) == 2
    assert "malformed golden issue: broken.yml" in capsys.readouterr().err
