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
from agent_repo.config import ConfigError, RepoConfig, load_config  # noqa: E402
from agent_repo.gate import TRIAGE_MARKER_NAME  # noqa: E402
from agent_repo.schema import load_schema, validate  # noqa: E402
from eval import (  # noqa: E402
    ABORT_OUT_OF_POLICY,
    ABORT_REOPEN,
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
# Fixed labels the triage apply allowlist may add besides the configured taxonomy.
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


def triage_runs(
    repo: str, since: datetime
) -> tuple[
    dict[int, dict],
    list[tuple[int, int]],
    dict[int, list[tuple[str, dict | None]]],
    list[tuple[int, int]],
]:
    """Return the newest applied decision per issue, the (issue, run) pairs whose triage
    run left a marker but no readable `agent-decision-<run_id>` record, each marker issue's
    runs as (run start, decision or None) newest first, and the (issue, run) pairs whose
    start time could not be read."""
    repository_id = gh_json(f"repos/{repo}")["id"]
    runs: dict[int, dict] = {}
    for artifact in gh_items(f"repos/{repo}/actions/artifacts?per_page=100", "artifacts"):
        run = artifact.get("workflow_run") or {}
        if (
            run.get("repository_id") != repository_id
            or run.get("head_repository_id") != repository_id
            # An expired marker or record no longer proves anything about its run.
            or artifact.get("expired")
        ):
            continue
        entry = runs.setdefault(run["id"], {"issue": None, "started": "", "record": None})
        if match := MARKER_ARTIFACT.fullmatch(artifact["name"]):
            entry["issue"] = int(match.group(1))
        elif artifact["name"] == DECISION_RECORD.format(run_id=run["id"]):
            entry["record"] = artifact
    unstarted: list[tuple[int, int]] = []
    for run_id, entry in runs.items():
        if entry["issue"] is None:
            continue
        # A run starts before it uploads anything, so order runs by the run's own start.
        try:
            started = gh_json(f"repos/{repo}/actions/runs/{run_id}")["run_started_at"]
            # A start that is not an aware timestamp cannot be ordered or windowed.
            if datetime.fromisoformat(started).tzinfo is None:
                started = None
        except (GhError, KeyError, TypeError, ValueError):
            started = None
        if isinstance(started, str):
            entry["started"] = started
            # The window selects runs by their own start, not by when they uploaded anything.
            if not _since(entry, "started", since):
                entry["issue"] = None
        else:
            unstarted.append((entry["issue"], run_id))
            entry["issue"] = None
    schema = load_schema("decision")
    decisions: dict[int, dict] = {}
    missing: list[tuple[int, int]] = []
    by_issue: dict[int, list[tuple[str, dict | None]]] = {}
    ordered = sorted(runs.items(), key=lambda item: item[1]["started"], reverse=True)
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
        if decision is not None and (
            validate(decision, schema) or decision["run_id"] != str(run_id)
        ):
            decision = None
        by_issue.setdefault(entry["issue"], []).append((entry["started"], decision))
        if decision is None:
            missing.append((entry["issue"], run_id))
        elif entry["issue"] not in decisions:
            decisions[entry["issue"]] = decision
    return decisions, missing, by_issue, sorted(unstarted)


def load_policy(repo: str, ref: str | None = None):
    """Load the repository's `.agents` policy from the default branch, or at `ref`; None
    when it cannot be loaded."""
    query = f"?ref={ref}" if ref else ""
    try:
        with tempfile.TemporaryDirectory() as directory:
            for name in ("config.yml", "areas.yml", "labels.yml", "charter.md"):
                content = gh_json(f"repos/{repo}/contents/.agents/{name}{query}")["content"]
                (Path(directory) / name).write_bytes(base64.b64decode(content))
            charter = parse_charter((Path(directory) / "charter.md").read_text(encoding="utf-8"))
            return load_config(Path(directory)), charter
    except (GhError, ConfigError, KeyError, TypeError, ValueError):
        return None


def label_allowlist(config: RepoConfig) -> set[str]:
    """Labels triage apply may add: the configured type and area taxonomy plus fixed labels."""
    taxonomy = {label for label in config.labels if label.startswith("type:")}
    # Apply needs an area label in labels.yml and its name in areas.yml.
    taxonomy |= {f"area:{area.name}" for area in config.areas} & set(config.labels)
    return taxonomy | TRIAGE_FIXED_LABELS


class _IssueLookup:
    """Lets apply's close predicate re-check a duplicate target through `gh api`."""

    def __init__(self, repo: str):
        self.repo = repo

    def request(self, method: str, path: str, *, params: dict | None = None) -> object:
        return gh_json(f"repos/{self.repo}/{path}")


class TargetStateUnknown(Exception):
    """Raised when a duplicate target's state at a past moment cannot be established."""


class _TargetAt:
    """Answers apply's duplicate-target lookup with the target's state at a past moment,
    rebuilt from its dated close and reopen events."""

    def __init__(self, repo: str, moment: str):
        self.repo = repo
        self.moment = moment

    def request(self, method: str, path: str, *, params: dict | None = None) -> object:
        issue = gh_json(f"repos/{self.repo}/{path}")
        changes = sorted(
            (event["created_at"], event["event"])
            for event in gh_items(f"repos/{self.repo}/{path}/events?per_page=100")
            if event.get("event") in {"closed", "reopened"} and event["created_at"] <= self.moment
        )
        if changes:
            return {**issue, "state": "closed" if changes[-1][1] == "closed" else "open"}
        created = issue.get("created_at")
        if not isinstance(created, str) or created > self.moment:
            raise TargetStateUnknown(path)
        return {**issue, "state": "open"}


def _rate(outcomes: list[bool]) -> float | None:
    return sum(outcomes) / len(outcomes) if outcomes else None


def mode_stats(
    decisions: dict[int, dict], issues: dict[int, dict], policy, repo: str
) -> tuple[dict[str, dict], list[str]]:
    """Compare each decision with the issue's current labels, grouped by triage mode."""
    by_mode: dict[str, dict] = {}
    skipped: list[str] = []
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
        if policy is None:
            continue
        try:
            eligible = close_eligible(
                decision, *policy, number=number, repo=repo, github=_IssueLookup(repo)
            )
        except (GhError, ValueError):
            skipped.append(f"- #{number}: duplicate target lookup failed")
            continue
        if eligible:
            stats["close"].append(issues[number]["state"] == "closed")
    for stats in by_mode.values():
        for question in ("type", "area"):
            stats[f"{question}_pairs"] = len(stats[question])
            value = _rate(stats.pop(question))
            if value is not None:
                stats[f"{question}_agreement"] = value
    return by_mode, skipped


def issue_events(repo: str, numbers: set[int]) -> tuple[dict[int, list], list[int]]:
    events, skipped = {}, []
    for number in sorted(numbers):
        try:
            events[number] = gh_items(f"repos/{repo}/issues/{number}/events?per_page=100")
        except GhError:
            skipped.append(number)
    return events, skipped


def _by_bot(event: dict, bot_login: str) -> bool:
    # As apply does for comments, the actor must be a Bot account, not a look-alike user.
    actor = event.get("actor") or {}
    return actor.get("type") == "Bot" and canonical_bot_login(actor.get("login", "")) == bot_login


def reopen_counts(events: dict[int, list], bot_login: str, since: datetime) -> tuple[int, int]:
    """Count in-window bot close events correlated with the bot's agent:closed label, and
    how many of those closes were later reopened. A close correlates when the bot added the
    label at or after it (apply closes, then labels) and before any later reopen, or when a
    bot-added label was still present at it (a re-close of a reopened issue)."""
    closed = reopened = 0
    for timeline in events.values():
        reopens = [event["created_at"] for event in timeline if event.get("event") == "reopened"]
        removals = [
            event["created_at"]
            for event in timeline
            if event.get("event") == "unlabeled"
            and (event.get("label") or {}).get("name") == "agent:closed"
        ]
        labelled = [
            event["created_at"]
            for event in timeline
            if event.get("event") == "labeled"
            and (event.get("label") or {}).get("name") == "agent:closed"
            and _by_bot(event, bot_login)
        ]
        for event in timeline:
            close_time = event["created_at"] if event.get("event") == "closed" else None
            if (
                close_time is None
                or not _by_bot(event, bot_login)
                or not _since(event, "created_at", since)
                or not any(
                    (
                        close_time <= label_time
                        and not any(close_time < reopen < label_time for reopen in reopens)
                    )
                    or (
                        label_time < close_time
                        and not any(label_time < removal <= close_time for removal in removals)
                    )
                    for label_time in labelled
                )
            ):
                continue
            closed += 1
            reopened += any(close_time < reopen for reopen in reopens)
    return closed, reopened


def out_of_policy_labels(
    events: dict[int, list], bot_login: str, since: datetime, allowed: set[str] | None
) -> tuple[int, int]:
    """Count bot labels outside the allowlist; without a policy, count type:/area: labels as
    unchecked instead."""
    count = unchecked = 0
    for timeline in events.values():
        for event in timeline:
            if (
                event.get("event") != "labeled"
                or not _by_bot(event, bot_login)
                or not _since(event, "created_at", since)
            ):
                continue
            label = (event.get("label") or {}).get("name", "")
            if allowed is not None:
                count += label not in allowed
            elif label.startswith(("type:", "area:")):
                unchecked += 1
            else:
                count += label not in TRIAGE_FIXED_LABELS
    return count, unchecked


def out_of_policy_closes(
    repo: str,
    events: dict[int, list],
    runs: dict[int, list[tuple[str, dict | None]]],
    bot_login: str,
    since: datetime,
) -> tuple[list[str], list[str]]:
    """Judge each in-window bot close against the decision of the newest triage run that
    started before it, under the policy at that decision's base_sha; closes without a
    readable record or loadable policy are unverifiable."""
    violations, unverifiable = [], []
    policies: dict[str, object] = {}
    for number, timeline in sorted(events.items()):
        for event in timeline:
            if (
                event.get("event") != "closed"
                or not _by_bot(event, bot_login)
                or not _since(event, "created_at", since)
            ):
                continue
            earlier = [item for item in runs.get(number, []) if item[0] <= event["created_at"]]
            decision = earlier[0][1] if earlier else None
            if decision is None:
                unverifiable.append(
                    f"- #{number}: closed {event['created_at']} with no readable decision record"
                )
                continue
            sha = decision["base_sha"]
            if sha not in policies:
                policies[sha] = load_policy(repo, sha)
            if policies[sha] is None:
                unverifiable.append(
                    f"- #{number}: closed {event['created_at']} with no loadable policy at {sha}"
                )
                continue
            if policies[sha][0].mode_for("triage") != "live":
                # A dry-run apply executes no actions, so no close from that run is allowed.
                violations.append(
                    f"- #{number}: closed {event['created_at']}; triage was dry-run in the "
                    f"policy at {sha}"
                )
                continue
            try:
                eligible = close_eligible(
                    decision,
                    *policies[sha],
                    number=number,
                    repo=repo,
                    github=_TargetAt(repo, event["created_at"]),
                )
            except (GhError, TargetStateUnknown, ValueError):
                unverifiable.append(
                    f"- #{number}: closed {event['created_at']} with no established duplicate-"
                    "target state at that time"
                )
                continue
            if not eligible:
                violations.append(
                    f"- #{number}: closed {event['created_at']}; its run "
                    f"{decision['run_id']}'s decision does not allow a close"
                )
    return violations, unverifiable


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def collect(args: argparse.Namespace, now: datetime) -> tuple[list[str], dict]:
    since = now - timedelta(days=args.days)
    bot_login = canonical_bot_login(args.bot_login or "")
    lines: list[str] = []
    decisions, missing, runs, unstarted = triage_runs(args.repo, since)
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
    by_mode, lookup_skipped = mode_stats(decided, issues, policy, args.repo)
    skipped += lookup_skipped
    for mode, stats in sorted(by_mode.items()):
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
                "(needs at least 20 candidates and at least 95% agreement; duplicate "
                "targets are checked against their current state)"
            )
        flags = abort_flags({key: stats[key] for key in stats if key != "close"})
        lines.extend(f"- ABORT: {flag}" for flag in flags)
        repo_stats["flags"].extend(flags)

    lines += ["", "## Missing decision records", ""]
    lines += [
        f"- #{number}: run {run_id} has no readable {DECISION_RECORD.format(run_id=run_id)}"
        for number, run_id in sorted(missing)
    ] or ["- None."]

    gaps = []
    try:
        labelled = {
            item["number"]
            for item in gh_items(
                f"repos/{args.repo}/issues?labels={quote('agent:closed')}&state=all&per_page=100"
            )
            if "pull_request" not in item
        }
    except GhError:
        labelled = set()
        skipped.append("- agent:closed issue listing failed")
        gaps.append("the agent:closed issue listing failed")
    skipped += [f"- #{number}: run {run_id} start is unreadable" for number, run_id in unstarted]
    if unstarted:
        gaps.append(f"{len(unstarted)} triage run starts are unreadable")
    numbers = set(runs) | labelled
    events, events_skipped = issue_events(args.repo, numbers)
    skipped += [f"- #{number}: issue events lookup failed" for number in events_skipped]
    if events_skipped:
        gaps.insert(0, f"{len(events_skipped)} issue event lookups failed")
    lines += ["", "## Skipped issues", "", *(skipped or ["- None."])]

    lines += ["", "## Reopen rate for agent:closed", ""]
    # Criteria read from issue events cannot be judged when lookups they depend on failed.
    events_gap = "; ".join(gaps)
    if bot_login:
        closed, reopened = reopen_counts(events, bot_login, since)
        repo_stats.update({"agent_closed": closed, "reopened": reopened})
        lines.append(
            f"- {reopened} of {closed} bot closes with agent:closed in the window were reopened "
            f"({_percent(reopened / closed if closed else None)})"
        )
        if events_gap:
            lines.append(f"- Not evaluated: {events_gap} (listed under Skipped issues).")
            repo_stats["not_evaluated"].append(f"{ABORT_REOPEN} ({events_gap})")
    else:
        lines.append("- Not evaluated; pass --bot-login.")
        repo_stats["not_evaluated"].append(f"{ABORT_REOPEN} (pass --bot-login)")

    lines += ["", "## Out-of-policy actions", ""]
    unverifiable: list[str] = []
    if bot_login:
        allowed = label_allowlist(policy[0]) if policy is not None else None
        labels, unchecked = out_of_policy_labels(events, bot_login, since, allowed)
        violations, unverifiable = out_of_policy_closes(args.repo, events, runs, bot_login, since)
        closes = len(violations)
        repo_stats["out_of_policy_actions"] = labels + closes
        lines.append(
            f"- {labels + closes} actions by {bot_login} across {len(events)} issues from "
            "triage marker runs and agent:closed items: labels outside the configured "
            "triage taxonomy, and closes that the run's own decision record and the policy at "
            "its base_sha do not allow."
        )
        lines.extend(violations)
        skipped_parts = []
        if policy is None:
            lines.append(
                f"- Not evaluated: {unchecked} type:/area: labels, because the default "
                "branch's `.agents/` could not be loaded."
            )
            skipped_parts.append("taxonomy labels: `.agents/` could not be loaded")
        if events_gap:
            lines.append(f"- Not evaluated: {events_gap} (listed under Skipped issues).")
            skipped_parts.append(events_gap)
        if unverifiable:
            # A close that cannot be judged must not let the criterion read as clean.
            lines.append(
                f"- Not evaluated: {len(unverifiable)} bot closes are unverifiable (listed below)."
            )
            skipped_parts.append(f"{len(unverifiable)} unverifiable bot closes")
        if skipped_parts:
            repo_stats["not_evaluated"].append(
                f"{ABORT_OUT_OF_POLICY} ({'; '.join(skipped_parts)})"
            )
    else:
        lines.append("- Not evaluated; pass --bot-login.")
        repo_stats["not_evaluated"].append(f"{ABORT_OUT_OF_POLICY} (pass --bot-login)")
    lines += ["", "## Unverifiable bot closes", "", *(unverifiable or ["- None."])]

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
