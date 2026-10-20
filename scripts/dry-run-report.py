"""Report dry-run triage agreement, close readiness and abort criteria for a repository.

Usage: dry-run-report.py --repo OWNER/REPO [--days 30] [--bot-login LOGIN]
                         [--tier2-budget-alerts N]

Reads decision artifacts and current labels through `gh api`, prints Markdown, and
always exits 0 so a failed lookup never blocks the maintainer reading the report.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
sys.path.insert(0, str(SCRIPTS))

from agent_repo.apply.actions import canonical_bot_login  # noqa: E402
from agent_repo.apply.triage import _answer_label  # noqa: E402
from agent_repo.gate import TRIAGE_MARKER_NAME  # noqa: E402
from agent_repo.schema import load_schema, validate  # noqa: E402
from eval import abort_flags, close_enable_ready  # noqa: E402

MARKER_ARTIFACT = re.compile(
    re.escape(TRIAGE_MARKER_NAME).replace(re.escape("{issue_number}"), "([1-9][0-9]*)")
)
TIER2_COUNTER = re.compile(r"tier2-[0-9]{8}(?:-.+)?")
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


def latest_decisions(repo: str, since: datetime) -> dict[int, dict]:
    """Return the newest schema-valid decision per issue from same-repository triage runs."""
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
        entry = runs.setdefault(run["id"], {"issue": None, "created": "", "artifacts": []})
        entry["created"] = max(entry["created"], artifact["created_at"])
        if match := MARKER_ARTIFACT.fullmatch(artifact["name"]):
            entry["issue"] = int(match.group(1))
        elif not artifact.get("expired") and not TIER2_COUNTER.fullmatch(artifact["name"]):
            entry["artifacts"].append(artifact)
    schema = load_schema("decision")
    decisions: dict[int, dict] = {}
    for entry in sorted(runs.values(), key=lambda item: item["created"], reverse=True):
        if entry["issue"] is None or entry["issue"] in decisions:
            continue
        for artifact in entry["artifacts"]:
            archive = zipfile.ZipFile(
                io.BytesIO(_gh([], f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"))
            )
            if "decision.json" not in archive.namelist():
                continue
            try:
                decision = json.loads(archive.read("decision.json"))
            except ValueError:
                continue
            if not validate(decision, schema):
                decisions[entry["issue"]] = decision
                break
    return decisions


def _rate(outcomes: list[bool]) -> float | None:
    return sum(outcomes) / len(outcomes) if outcomes else None


def mode_stats(decisions: dict[int, dict], issues: dict[int, dict]) -> dict[str, dict]:
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
        if decision["close"] is not None:
            stats["close"].append(issues[number]["state"] == "closed")
    for stats in by_mode.values():
        for question in ("type", "area"):
            stats[f"{question}_pairs"] = len(stats[question])
            value = _rate(stats.pop(question))
            if value is not None:
                stats[f"{question}_agreement"] = value
    return by_mode


def out_of_policy_labels(repo: str, numbers: set[int], bot_login: str, since: datetime) -> int:
    count = 0
    for number in sorted(numbers):
        for event in gh_items(f"repos/{repo}/issues/{number}/events?per_page=100"):
            actor = (event.get("actor") or {}).get("login", "")
            label = (event.get("label") or {}).get("name", "")
            if (
                event.get("event") == "labeled"
                and canonical_bot_login(actor) == bot_login
                and _since(event, "created_at", since)
                and not label.startswith(("type:", "area:"))
                and label not in TRIAGE_FIXED_LABELS
            ):
                count += 1
    return count


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def collect(args: argparse.Namespace, now: datetime) -> tuple[list[str], dict]:
    since = now - timedelta(days=args.days)
    lines: list[str] = []
    decisions = latest_decisions(args.repo, since)
    issues = {number: gh_json(f"repos/{args.repo}/issues/{number}") for number in decisions}
    repo_stats: dict = {}
    for mode, stats in sorted(mode_stats(decisions, issues).items()):
        lines += ["", f"## Mode: {mode}", "", f"- Decided issues: {stats['agreement_issues']}"]
        for question in ("type", "area"):
            lines.append(
                f"- {question.capitalize()} agreement: "
                f"{_percent(stats.get(f'{question}_agreement'))} "
                f"({stats[f'{question}_pairs']} maintainer-labelled issues)"
            )
        if mode == "llm-only":
            lines.append("- Close readiness: not applicable; LLM-only mode never auto-closes.")
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
        repo_stats.setdefault("flags", []).extend(flags)

    since_text = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    closed = [
        item
        for item in gh_items(
            f"repos/{args.repo}/issues?labels={quote('agent:closed')}&state=all"
            f"&since={since_text}&per_page=100"
        )
        if "pull_request" not in item
    ]
    reopened = sum(item["state"] == "open" for item in closed)
    repo_stats.update({"agent_closed": len(closed), "reopened": reopened})
    lines += ["", "## Reopen rate for agent:closed", ""]
    lines.append(
        f"- {reopened} of {len(closed)} reopened "
        f"({_percent(_rate([item['state'] == 'open' for item in closed]))})"
    )

    lines += ["", "## Out-of-policy actions", ""]
    bot_login = canonical_bot_login(args.bot_login or "")
    if bot_login:
        numbers = set(decisions) | {item["number"] for item in closed}
        repo_stats["out_of_policy_actions"] = out_of_policy_labels(
            args.repo, numbers, bot_login, since
        )
        lines.append(
            f"- Labels outside the triage allowlist added by {bot_login}: "
            f"{repo_stats['out_of_policy_actions']}"
        )
    else:
        lines.append("- Not checked; pass --bot-login.")

    lines += ["", "## Tier-2 budget", ""]
    if args.tier2_budget_alerts is None:
        lines.append("- Not checked; pass --tier2-budget-alerts from the gateway's budget alerts.")
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
    flags = list(dict.fromkeys(repo_stats.pop("flags", []) + abort_flags(repo_stats)))
    print("\n".join(lines))
    print("\n## Abort criteria\n")
    print("\n".join(f"- ABORT: {flag}" for flag in flags) if flags else "- None triggered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
