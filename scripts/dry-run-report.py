"""Report dry-run triage agreement, close readiness and abort criteria for a repository.

Usage: dry-run-report.py --repo OWNER/REPO [--days 30] [--bot-login LOGIN]
                         [--tier2-budget-alerts N]

Reads each triage run's `agent-decision-<run_id>` record, the `.agents/` policy, current
labels and issue events through `gh api`, prints Markdown, and always exits 0 so a failed
lookup never blocks the maintainer reading the report.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
sys.path.insert(0, str(SCRIPTS))

from agent_repo.apply.actions import canonical_bot_login  # noqa: E402
from agent_repo.apply.triage import _answer_label  # noqa: E402
from agent_repo.charter import parse_charter  # noqa: E402
from agent_repo.config import ConfigError, is_llm_only, load_config  # noqa: E402
from agent_repo.gate import TRIAGE_MARKER_NAME  # noqa: E402
from agent_repo.schema import load_schema, validate  # noqa: E402
from eval import (  # noqa: E402
    ABORT_OUT_OF_POLICY,
    ABORT_TIER2_BUDGET,
    abort_flags,
    close_eligible,
    close_enable_ready,
)

MARKER_ARTIFACT = re.compile(
    re.escape(TRIAGE_MARKER_NAME).replace(re.escape("{issue_number}"), "([1-9][0-9]*)")
)
# Apply uploads the applied decision under this exact name; hand-off artifacts are ignored.
DECISION_RECORD = "agent-decision-{run_id}"
# Labels the triage apply allowlist may add; anything else from the bot is out of policy.
TRIAGE_FIXED_LABELS = {
    "P2",
    "P3",
    "needs-info",
    "needs-maintainer",
    "agent:closed",
    "agent:triage-capped",
}


class GhError(Exception):
    """Raised when a `gh api` call fails; the message names only the endpoint."""


def _gh(arguments: list[str], endpoint: str) -> bytes:
    try:
        result = subprocess.run(
            ["gh", "api", *arguments, endpoint],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh api failed: {endpoint}") from exc
    if result.returncode != 0:
        raise GhError(f"gh api failed: {endpoint}")
    return result.stdout


def gh_json(endpoint: str) -> dict:
    try:
        return json.loads(_gh([], endpoint))
    except ValueError as exc:
        raise GhError(f"gh api returned invalid JSON: {endpoint}") from exc


def gh_items(endpoint: str, key: str | None = None) -> list:
    output = _gh(["--paginate", "--jq", f".{key}[]" if key else ".[]"], endpoint)
    try:
        return [json.loads(line) for line in output.splitlines() if line.strip()]
    except ValueError as exc:
        raise GhError(f"gh api returned invalid JSON: {endpoint}") from exc


def _since(item: dict, field: str, since: datetime) -> bool:
    value = item.get(field)
    return isinstance(value, str) and datetime.fromisoformat(value) >= since


def latest_decisions(
    repo: str, since: datetime
) -> tuple[dict[int, dict], list[tuple[int, int]], set[int]]:
    """Return the newest applied decision per issue, the (issue, run) pairs whose triage
    run left a marker but no readable `agent-decision-<run_id>` record, and every marker
    issue."""
    repository_id = gh_json(f"repos/{repo}")["id"]
    runs: dict[int, dict] = {}
    for artifact in gh_items(f"repos/{repo}/actions/artifacts?per_page=100", "artifacts"):
        run = artifact.get("workflow_run") or {}
        if (
            run.get("repository_id") != repository_id
            or run.get("head_repository_id") != repository_id
            or not _since(artifact, "created_at", since)
        ):
            continue
        entry = runs.setdefault(run["id"], {"issue": None, "created": "", "record": None})
        entry["created"] = max(entry["created"], artifact["created_at"])
        if match := MARKER_ARTIFACT.fullmatch(artifact["name"]):
            entry["issue"] = int(match.group(1))
        elif artifact["name"] == DECISION_RECORD.format(run_id=run["id"]) and not artifact.get(
            "expired"
        ):
            entry["record"] = artifact
    schema = load_schema("decision")
    decisions: dict[int, dict] = {}
    missing: list[tuple[int, int]] = []
    ordered = sorted(runs.items(), key=lambda item: item[1]["created"], reverse=True)
    for run_id, entry in ordered:
        if entry["issue"] is None:
            continue
        decision = None
        if entry["record"] is not None:
            try:
                content = _gh([], f"repos/{repo}/actions/artifacts/{entry['record']['id']}/zip")
                decision = json.loads(zipfile.ZipFile(io.BytesIO(content)).read("decision.json"))
            except (GhError, KeyError, ValueError, zipfile.BadZipFile):
                decision = None
        if decision is None or validate(decision, schema):
            missing.append((entry["issue"], run_id))
        elif entry["issue"] not in decisions:
            decisions[entry["issue"]] = decision
    marked = {entry["issue"] for entry in runs.values() if entry["issue"] is not None}
    return decisions, missing, marked


def load_policy(repo: str):
    """Load the repository's `.agents` policy from its default branch, or None."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            for name in ("config.yml", "areas.yml", "labels.yml", "charter.md"):
                content = gh_json(f"repos/{repo}/contents/.agents/{name}")["content"]
                (Path(directory) / name).write_bytes(base64.b64decode(content))
            charter = parse_charter((Path(directory) / "charter.md").read_text(encoding="utf-8"))
            return load_config(Path(directory)), charter
    except (GhError, ConfigError, KeyError, TypeError, ValueError):
        return None


class _IssueLookup:
    """Lets apply's close predicate re-check a duplicate target through `gh api`."""

    def __init__(self, repo: str):
        self.repo = repo

    def request(self, method: str, path: str, *, params: dict | None = None) -> object:
        return gh_json(f"repos/{self.repo}/{path}")


def _rate(outcomes: list[bool]) -> float | None:
    return sum(outcomes) / len(outcomes) if outcomes else None


def mode_stats(
    decisions: dict[int, dict], issues: dict[int, dict], policy, repo: str
) -> dict[str, dict]:
    """Compare each decision with the issue's current labels, grouped by triage mode."""
    by_mode: dict[str, dict] = {}
    for number, decision in decisions.items():
        mode = decision.get("mode", "system-one")
        stats = by_mode.setdefault(
            mode, {"agreement_issues": 0, "type": [], "area": [], "close": []}
        )
        stats["agreement_issues"] += 1
        labels = [label["name"] for label in issues[number]["labels"]]
        for question in ("type", "area"):
            current = [label for label in labels if label.startswith(f"{question}:")]
            answer = decision["answers"].get(question)
            if len(current) == 1:
                predicted = _answer_label(question, answer["value"]) if answer else None
                stats[question].append(predicted == current[0])
        if policy is not None and close_eligible(
            decision, *policy, number=number, repo=repo, github=_IssueLookup(repo)
        ):
            stats["close"].append(issues[number]["state"] == "closed")
    for stats in by_mode.values():
        for question in ("type", "area"):
            stats[f"{question}_pairs"] = len(stats[question])
            value = _rate(stats.pop(question))
            if value is not None:
                stats[f"{question}_agreement"] = value
    return by_mode


def issue_events(repo: str, numbers: set[int]) -> tuple[dict[int, list], list[int]]:
    events, skipped = {}, []
    for number in sorted(numbers):
        try:
            events[number] = gh_items(f"repos/{repo}/issues/{number}/events?per_page=100")
        except GhError:
            skipped.append(number)
    return events, skipped


def reopen_counts(events: dict[int, list], bot_login: str, since: datetime) -> tuple[int, int]:
    """Count in-window agent closes (agent:closed added) and those later reopened."""
    closed = reopened = 0
    for timeline in events.values():
        closes = [
            event["created_at"]
            for event in timeline
            if event.get("event") == "labeled"
            and (event.get("label") or {}).get("name") == "agent:closed"
            and (
                not bot_login
                or canonical_bot_login((event.get("actor") or {}).get("login", "")) == bot_login
            )
            and _since(event, "created_at", since)
        ]
        if not closes:
            continue
        closed += 1
        reopened += any(
            event.get("event") == "reopened" and event["created_at"] > min(closes)
            for event in timeline
        )
    return closed, reopened


def out_of_policy_actions(
    events: dict[int, list], bot_login: str, since: datetime, no_close: set[int]
) -> int:
    """Count bot label additions outside the triage allowlist, and bot closes on issues
    where closing is not allowed (LLM-only mode or close disabled)."""
    count = 0
    for number, timeline in events.items():
        for event in timeline:
            actor = (event.get("actor") or {}).get("login", "")
            if canonical_bot_login(actor) != bot_login or not _since(event, "created_at", since):
                continue
            label = (event.get("label") or {}).get("name", "")
            if event.get("event") == "labeled" and not (
                label.startswith(("type:", "area:")) or label in TRIAGE_FIXED_LABELS
            ):
                count += 1
            elif event.get("event") == "closed" and number in no_close:
                count += 1
    return count


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def collect(args: argparse.Namespace, now: datetime) -> tuple[list[str], dict]:
    since = now - timedelta(days=args.days)
    bot_login = canonical_bot_login(args.bot_login or "")
    lines: list[str] = []
    decisions, missing, marked = latest_decisions(args.repo, since)
    policy = load_policy(args.repo)
    issues, skipped = {}, []
    for number in sorted(decisions):
        try:
            issues[number] = gh_json(f"repos/{args.repo}/issues/{number}")
        except GhError:
            skipped.append(f"- #{number}: issue lookup failed")
    decided = {number: decisions[number] for number in issues}
    close_enabled = policy is not None and policy[0].raw["close"]["enabled"]
    repo_stats: dict = {"flags": [], "not_evaluated": []}
    for mode, stats in sorted(mode_stats(decided, issues, policy, args.repo).items()):
        lines += ["", f"## Mode: {mode}", "", f"- Decided issues: {stats['agreement_issues']}"]
        for question in ("type", "area"):
            lines.append(
                f"- {question.capitalize()} agreement: "
                f"{_percent(stats.get(f'{question}_agreement'))} "
                f"({stats[f'{question}_pairs']} maintainer-labelled issues)"
            )
        if mode == "llm-only":
            lines.append("- Close readiness: not applicable; LLM-only mode never auto-closes.")
        elif policy is None:
            lines.append("- Close readiness: not evaluated; `.agents/` could not be loaded.")
        elif not close_enabled:
            lines.append(
                "- Close candidates: none; candidates are only collected while "
                "`close.enabled: true` runs in dry-run mode."
            )
        else:
            ready = close_enable_ready(stats["close"])
            lines.append(
                f"- Close candidates: {len(stats['close'])}, maintainer agreement "
                f"{_percent(_rate(stats['close']))}; "
                f"close_enable_ready: {'yes' if ready else 'no'} "
                "(needs at least 20 candidates and at least 95% agreement)"
            )
        flags = abort_flags({key: stats[key] for key in stats if key != "close"})
        lines.extend(f"- ABORT: {flag}" for flag in flags)
        repo_stats["flags"].extend(flags)

    lines += ["", "## Missing decision records", ""]
    lines += [
        f"- #{number}: run {run_id} has no readable {DECISION_RECORD.format(run_id=run_id)}"
        for number, run_id in sorted(missing)
    ] or ["- None."]

    labelled = {
        item["number"]
        for item in gh_items(
            f"repos/{args.repo}/issues?labels={quote('agent:closed')}&state=all&per_page=100"
        )
        if "pull_request" not in item
    }
    numbers = marked | labelled
    events, events_skipped = issue_events(args.repo, numbers)
    skipped += [f"- #{number}: issue events lookup failed" for number in events_skipped]
    lines += ["", "## Skipped issues", "", *(skipped or ["- None."])]

    closed, reopened = reopen_counts(events, bot_login, since)
    repo_stats.update({"agent_closed": closed, "reopened": reopened})
    lines += ["", "## Reopen rate for agent:closed", ""]
    lines.append(
        f"- {reopened} of {closed} agent closes in the window were reopened "
        f"({_percent(reopened / closed if closed else None)})"
    )

    lines += ["", "## Out-of-policy actions", ""]
    if bot_login:
        no_close = {
            number for number in numbers if decisions.get(number, {}).get("mode") == "llm-only"
        }
        if policy is not None and (is_llm_only(policy[0]) or not close_enabled):
            no_close = set(numbers)
        repo_stats["out_of_policy_actions"] = out_of_policy_actions(
            events, bot_login, since, no_close
        )
        lines.append(
            f"- {repo_stats['out_of_policy_actions']} actions by {bot_login} across "
            f"{len(events)} issues from triage marker runs and agent:closed items: labels "
            "outside the triage allowlist, and closes in LLM-only mode or with close disabled."
        )
    else:
        lines.append("- Not evaluated; pass --bot-login.")
        repo_stats["not_evaluated"].append(f"{ABORT_OUT_OF_POLICY} (pass --bot-login)")

    lines += ["", "## Tier-2 budget", ""]
    if args.tier2_budget_alerts is None:
        lines.append("- Not evaluated; pass --tier2-budget-alerts from the gateway's alerts.")
        repo_stats["not_evaluated"].append(f"{ABORT_TIER2_BUDGET} (pass --tier2-budget-alerts)")
    else:
        repo_stats["tier2_budget_alerts_per_week"] = args.tier2_budget_alerts
        lines.append(
            f"- Most 80% budget alerts for one key in one week: {args.tier2_budget_alerts}"
        )
    return lines, repo_stats


def main(argv: list[str], now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report dry-run triage agreement.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--bot-login")
    parser.add_argument("--tier2-budget-alerts", type=int)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    print(f"# Dry-run report for {args.repo} (last {args.days} days)")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) is None or args.days < 1:
        print("\nReport incomplete: --repo must be OWNER/REPO and --days at least 1.")
        return 0
    try:
        lines, repo_stats = collect(args, now or datetime.now(UTC))
    except GhError as exc:
        print(f"\nReport incomplete: {exc}")
        return 0
    except Exception as exc:
        print(f"\nReport incomplete: unexpected {type(exc).__name__}.")
        return 0
    flags = list(dict.fromkeys(repo_stats.pop("flags") + abort_flags(repo_stats)))
    skipped = repo_stats.pop("not_evaluated")
    print("\n".join(lines))
    print("\n## Abort criteria\n")
    results = [f"- ABORT: {flag}" for flag in flags]
    results += [f"- NOT EVALUATED: {criterion}" for criterion in skipped]
    if not flags:
        # A skipped criterion must not read as a clean result.
        results.append("- No evaluated criterion triggered." if skipped else "- None triggered.")
    print("\n".join(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
