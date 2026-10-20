"""Score golden issues against recorded decision artifacts, per triage mode.

Usage: eval.py --decisions DIR [--decisions DIR ...] [--evals DIR]

Each DIR holds one decision.json per golden issue, named <issue-id>.json. The decisions
under tests/fixtures/evals are hand-authored examples; live recordings come from running
decide and the Tier-2 merge against evals/ (labels.yml, areas.yml, config.yml, charter.md).
Results are reported, not gated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from agent_repo.apply.actions import ApplyContext  # noqa: E402
from agent_repo.apply.triage import _answer_label, _can_close  # noqa: E402
from agent_repo.charter import Charter, lint_charter, parse_charter  # noqa: E402
from agent_repo.config import ConfigError, RepoConfig, load_config  # noqa: E402
from agent_repo.event import Event  # noqa: E402
from agent_repo.schema import load_schema, validate  # noqa: E402
from agent_repo.yamljson import YamlError, load_yaml  # noqa: E402

QUESTIONS = ("type", "area", "priority", "needs_info")
OUTCOMES = {"label", "needs-info", "needs-maintainer", "close"}
ABORT_OUT_OF_POLICY = "any out-of-policy action"
ABORT_AGREEMENT = "dry-run agreement on type or area under 80% after 50 issues"
ABORT_REOPEN = "a reopen rate above 5% for `agent:closed`"
ABORT_TIER2_BUDGET = (
    "a Tier-2 key reaching 80% of its budget twice in one week. "
    "The response is to set `triage.rca_authors: known`."
)


class EvalError(Exception):
    """Raised when golden issues or recorded decisions are malformed."""


def accuracy(pairs: list[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    return sum(expected == predicted for expected, predicted in pairs) / len(pairs)


def calibration(points: list[tuple[float, bool]], bins: int = 10) -> list[tuple[float, float, int]]:
    """Return (mean p, accuracy, n) for each non-empty equal-width bin, lowest first."""
    grouped: dict[int, list[tuple[float, bool]]] = {}
    for probability, correct in points:
        grouped.setdefault(min(int(probability * bins), bins - 1), []).append(
            (probability, correct)
        )
    return [
        (
            sum(probability for probability, _ in members) / len(members),
            sum(correct for _, correct in members) / len(members),
            len(members),
        )
        for _, members in sorted(grouped.items())
    ]


def suggest_apply_min(points: list[tuple[float, bool]], precision: float = 0.9) -> float | None:
    """Return the lowest threshold whose answers at or above it meet the precision target."""
    for threshold in sorted({probability for probability, _ in points}):
        kept = [correct for probability, correct in points if probability >= threshold]
        if sum(kept) / len(kept) >= precision:
            return threshold
    return None


def close_enable_ready(agreements: list[bool]) -> bool:
    return len(agreements) >= 20 and sum(agreements) / len(agreements) >= 0.95


def abort_flags(stats: dict) -> list[str]:
    """Flag the spec's abort criteria; a statistic that is absent is not evaluated."""
    flags = []
    if stats.get("out_of_policy_actions", 0) > 0:
        flags.append(ABORT_OUT_OF_POLICY)
    if any(
        stats.get(f"{question}_pairs", 0) >= 50 and stats.get(f"{question}_agreement", 1.0) < 0.8
        for question in ("type", "area")
    ):
        flags.append(ABORT_AGREEMENT)
    if stats.get("agent_closed", 0) and stats.get("reopened", 0) / stats["agent_closed"] > 0.05:
        flags.append(ABORT_REOPEN)
    if stats.get("tier2_budget_alerts_per_week", 0) >= 2:
        flags.append(ABORT_TIER2_BUDGET)
    return flags


def close_eligible(
    decision: dict,
    config: RepoConfig,
    charter: Charter,
    *,
    number: int = 1,
    repo: str = "your-org/example",
    github: object | None = None,
) -> bool:
    """Whether apply would execute the decision's close, using apply's own predicate."""
    close = decision["close"]
    if close is None or decision.get("mode") == "llm-only":
        return False
    event = Event(
        name="issues",
        action="opened",
        repo=repo,
        number=number,
        is_pr=False,
        sender="",
        sender_is_bot=False,
        author="",
        author_association="NONE",
        comment_body=None,
        comment_author=None,
        label=None,
        head_ref=None,
        head_repo=None,
        base_sha=decision["base_sha"],
        head_sha=None,
        default_branch="main",
        title="",
        body="",
        labels=(),
        draft=False,
    )
    context = ApplyContext(
        event, config, charter, decision["run_id"], decision["base_sha"], [], "", False, github
    )
    duplicate_of = decision["answers"].get("duplicate_of", {}).get("value")
    return _can_close(close, context, duplicate_of)


class TargetNotRecorded(Exception):
    """Raised when a duplicate close's target has no recorded facts in the golden issue."""


class RecordedTargets:
    """Answers apply's duplicate-target lookup from a golden issue's `duplicate_target`."""

    def __init__(self, facts: dict | None):
        self.facts = facts

    def request(self, method: str, path: str, *, params: dict | None = None) -> object:
        if self.facts is None or path != f"issues/{self.facts['number']}":
            raise TargetNotRecorded(path)
        return {"state": self.facts["state"]}


def load_policy(evals_dir: Path) -> tuple[RepoConfig, Charter]:
    charter_text = (evals_dir / "charter.md").read_text(encoding="utf-8")
    errors = lint_charter(charter_text)
    if errors:
        raise EvalError("evals/charter.md: " + "; ".join(errors))
    try:
        return load_config(evals_dir), parse_charter(charter_text)
    except ConfigError as exc:
        raise EvalError(f"evals policy: {exc}") from exc


def load_golden(evals_dir: Path) -> dict[str, dict]:
    config, _ = load_policy(evals_dir)
    labels = set(config.labels) | {"area:unknown"}
    golden = {}
    for path in sorted((evals_dir / "issues").glob("*.yml")):
        try:
            document = load_yaml(path)
        except YamlError as exc:
            raise EvalError(str(exc)) from exc
        if not isinstance(document, dict) or not (
            {"issue", "expected"} <= set(document) <= {"issue", "expected", "duplicate_target"}
        ):
            raise EvalError(f"malformed golden issue: {path.name}")
        target = document.get("duplicate_target", {"number": 1, "state": "open"})
        if (
            not isinstance(target, dict)
            or set(target) != {"number", "state"}
            or type(target["number"]) is not int
            or target["state"] not in {"open", "closed"}
        ):
            raise EvalError(f"malformed golden issue: {path.name}")
        issue, expected = document["issue"], document["expected"]
        if (
            not isinstance(issue, dict)
            or set(issue) != {"title", "body"}
            or not all(isinstance(issue[key], str) and issue[key] for key in issue)
            or not isinstance(expected, dict)
            or set(expected) != {"labels", "area", "priority", "outcome"}
            or not all(isinstance(expected[key], str) for key in ("area", "priority", "outcome"))
            or not isinstance(expected["labels"], list)
            or sum(str(label).startswith("type:") for label in expected["labels"]) != 1
            or f"area:{expected['area']}" not in expected["labels"]
            or not set(expected["labels"]) <= labels
            or expected["priority"] not in {"P0", "P1", "P2", "P3"}
            or expected["priority"] not in expected["labels"]
            or expected["outcome"] not in OUTCOMES
        ):
            raise EvalError(f"malformed golden issue: {path.name}")
        golden[path.stem] = document
    return golden


def _expected(document: dict) -> dict[str, object]:
    labels = document["expected"]["labels"]
    return {
        "type": next(label for label in labels if label.startswith("type:")),
        "area": f"area:{document['expected']['area']}",
        "priority": document["expected"]["priority"],
        "needs_info": "needs-info" in labels,
    }


def outcome(
    decision: dict, config: RepoConfig, charter: Charter, targets: RecordedTargets | None = None
) -> str:
    """Name the visible result apply would produce for a decision; raises
    TargetNotRecorded when a duplicate close cannot be judged offline."""
    if close_eligible(decision, config, charter, github=targets or RecordedTargets(None)):
        return "close"
    if (
        decision["close"] is not None
        or decision["security"]
        or decision["escalate"] is not None
        or decision["suggestions"]["priority"] is not None
        or "needs-maintainer" in decision["labels"]
    ):
        return "needs-maintainer"
    if decision["needs_info"] is not None:
        return "needs-info"
    return "label"


def score(
    golden: dict[str, dict], decisions: list[tuple[str, dict]], config: RepoConfig, charter: Charter
) -> dict[str, dict]:
    """Group recorded decisions by mode and score each question and the outcome."""
    by_mode: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()
    for issue_id, decision in decisions:
        mode = decision.get("mode", "system-one")
        if (mode, issue_id) in seen:
            raise EvalError(f"{issue_id} recorded twice in mode {mode}")
        seen.add((mode, issue_id))
        result = by_mode.setdefault(
            mode,
            {
                "issues": 0,
                "pairs": {q: [] for q in (*QUESTIONS, "outcome")},
                "points": {},
                "not_evaluated": [],
            },
        )
        result["issues"] += 1
        expected = _expected(golden[issue_id])
        for question in QUESTIONS:
            answer = decision["answers"].get(question)
            if answer is None:
                predicted = None
            elif question == "needs_info":
                predicted = answer["value"]
            else:
                predicted = _answer_label(question, answer["value"])
            result["pairs"][question].append((expected[question], predicted))
            if answer is not None:
                result["points"].setdefault(question, []).append(
                    (answer["p"], expected[question] == predicted)
                )
        wanted = golden[issue_id]["expected"]["outcome"]
        # LLM-only mode never auto-closes, so a close becomes a maintainer suggestion.
        if mode == "llm-only" and wanted == "close":
            wanted = "needs-maintainer"
        targets = RecordedTargets(golden[issue_id].get("duplicate_target"))
        try:
            result["pairs"]["outcome"].append((wanted, outcome(decision, config, charter, targets)))
        except TargetNotRecorded:
            result["not_evaluated"].append(issue_id)
    return by_mode


def load_decisions(directories: list[Path], golden: dict[str, dict]) -> list[tuple[str, dict]]:
    schema = load_schema("decision")
    decisions = []
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            if path.stem not in golden:
                raise EvalError(f"decision for unknown golden issue: {path.name}")
            try:
                decision = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvalError(f"unreadable decision: {path.name}") from exc
            if validate(decision, schema):
                raise EvalError(f"decision does not match schema: {path.name}")
            decisions.append((path.stem, decision))
    return decisions


def render(golden: dict[str, dict], by_mode: dict[str, dict]) -> str:
    lines = ["# Golden eval report", "", f"Golden issues: {len(golden)}"]
    for mode in sorted(by_mode):
        result = by_mode[mode]
        lines += ["", f"## Mode: {mode}", "", f"Scored issues: {result['issues']}", ""]
        lines += ["| Question | Accuracy | n | Suggested apply_min (90% precision) |"]
        lines += ["|---|---|---|---|"]
        for question in (*QUESTIONS, "outcome"):
            pairs = result["pairs"][question]
            suggested = suggest_apply_min(result["points"].get(question, []))
            shown = (
                "n/a" if question == "outcome" else (suggested if suggested is not None else "none")
            )
            lines.append(f"| {question} | {accuracy(pairs):.0%} | {len(pairs)} | {shown} |")
        if result["not_evaluated"]:
            lines += [
                "",
                "Outcome not evaluated (no recorded duplicate-target facts): "
                + ", ".join(sorted(result["not_evaluated"])),
            ]
        for question in QUESTIONS:
            lines += ["", f"### Calibration: {question}", "", "| Mean p | Accuracy | n |"]
            lines += ["|---|---|---|"]
            for mean_p, bin_accuracy, count in calibration(result["points"].get(question, [])):
                lines.append(f"| {mean_p:.2f} | {bin_accuracy:.0%} | {count} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Score golden issues per triage mode.")
    parser.add_argument("--evals", type=Path, default=ROOT / "evals")
    parser.add_argument("--decisions", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    try:
        golden = load_golden(args.evals)
        by_mode = score(golden, load_decisions(args.decisions, golden), *load_policy(args.evals))
    except (EvalError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write(render(golden, by_mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
