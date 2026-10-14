import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

from agent_repo.config import RepoConfig
from agent_repo.event import Event
from agent_repo.github import GitHub, GitHubError
from agent_repo.markers import find_markers


@dataclass(frozen=True)
class Command:
    name: Literal["triage", "review", "fix", "agent-off"]
    args: str


def parse_command(body: str) -> Command | None:
    line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if re.fullmatch(r"/agent[ \t]+off", line):
        return Command("agent-off", "")
    match = re.fullmatch(r"/(triage|review|fix)(?:[ \t]+(.*))?", line)
    if match:
        return Command(match[1], (match[2] or "").strip())
    return None


def authorize(gh: GitHub, login: str, maintainers: frozenset[str]) -> bool:
    if login.casefold().endswith("[bot]") or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login
    ):
        return False
    if login.casefold() in {name.casefold() for name in maintainers}:
        return True
    try:
        data = gh.request("GET", f"collaborators/{quote(login, safe='')}/permission")
    except GitHubError:
        return False
    return isinstance(data, dict) and data.get("role_name") in ("admin", "maintain", "write")


@dataclass(frozen=True)
class GateContext:
    workflow: Literal["triage", "review", "fix"]
    enabled: bool
    bot_login: str
    config: RepoConfig
    gh: GitHub
    now: datetime
    maintainers: frozenset[str]
    run_id: str


@dataclass(frozen=True)
class GateResult:
    run: bool
    reason: str
    tier2_allowed: bool
    capped: bool
    command: Command | None
    canary: str


def _mapping(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid metadata")
    return value


def _agent_pr(event: Event) -> bool:
    return bool(
        event.is_pr
        and event.head_ref
        and event.head_ref.startswith("agent/")
        and event.head_repo
        and event.head_repo.casefold() == event.repo.casefold()
    )


def evaluate(event: Event, ctx: GateContext) -> GateResult:
    canary = secrets.token_hex(16)
    command = parse_command(event.comment_body or "")
    try:
        return _evaluate(event, ctx, command, canary)
    except GitHubError:
        return GateResult(False, "api-error", False, False, command, canary)
    except (ValueError, TypeError, KeyError, AttributeError):
        return GateResult(False, "invalid-metadata", False, False, command, canary)


def _evaluate(event: Event, ctx: GateContext, command: Command | None, canary: str) -> GateResult:
    def result(run: bool, reason: str, tier2: bool = False, capped: bool = False) -> GateResult:
        return GateResult(run, reason, tier2, capped, command, canary)

    if not ctx.enabled:
        return result(False, "disabled")
    if not ctx.bot_login or not event.sender or not event.author or event.number <= 0:
        return result(False, "invalid-metadata")
    if event.name == "issue_comment" and event.action != "created":
        return result(False, "unsupported-event")
    agent_sync = (
        ctx.workflow == "review"
        and event.name == "pull_request_target"
        and event.action == "synchronize"
        and _agent_pr(event)
        and event.sender.casefold() == ctx.bot_login.casefold()
    )
    if event.sender.casefold() == ctx.bot_login.casefold() and not agent_sync:
        return result(False, "self")
    if (event.sender_is_bot or event.sender.casefold().endswith("[bot]")) and not agent_sync:
        return result(False, "bot")
    if ctx.workflow == "triage" and event.author.casefold().endswith("[bot]"):
        return result(False, "bot")
    if "agent:skip" in event.labels:
        return result(False, "skip-label")
    if ctx.workflow == "triage" and event.is_pr:
        return result(False, "unsupported-event")
    if event.name == "issue_comment" and event.is_pr:
        # Comment webhooks contain a PR link, so fetch trusted head and draft metadata.
        pull = _mapping(ctx.gh.request("GET", f"pulls/{event.number}"))
        head = _mapping(pull.get("head"))
        event = replace(
            event,
            head_ref=head.get("ref"),
            head_repo=_mapping(head.get("repo") or {}).get("full_name"),
            draft=pull["draft"],
        )
    if ctx.workflow == "review" and event.draft and not _agent_pr(event):
        return result(False, "draft")
    comments = ctx.gh.paginate(f"issues/{event.number}/comments", params={"per_page": 100})
    for comment in comments:
        item = _mapping(comment)
        login = _mapping(item.get("user") or {}).get("login", "")
        if login.casefold() == ctx.bot_login.casefold() and any(
            marker.role == "off" for marker in find_markers(item.get("body") or "")
        ):
            return result(False, "off")

    limits = ctx.config.raw["limits"]
    if ctx.workflow == "triage":
        if (
            event.name == "issues"
            and event.action == "edited"
            and not ({"title", "body"} & set(event.edited_fields))
        ):
            return result(False, "no-op-edit")
        runs = ctx.gh.paginate("actions/runs", params={"per_page": 100}, key="workflow_runs")
        prior = [
            _mapping(run)
            for run in runs
            if _mapping(run).get("display_title") == f"agent-triage #{event.number}"
            and str(run["id"]) != ctx.run_id
        ]
        if len(prior) >= limits["triage_runs_per_issue"]:
            return result(False, "cycle-cap", capped=True)
        if event.name == "issues" and event.action == "edited" and prior:
            times = [
                datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")) for run in prior
            ]
            if any(time.tzinfo is None for time in times):
                raise ValueError("invalid timestamp")
            if ctx.now - max(times) < timedelta(minutes=limits["edit_debounce_minutes"]):
                return result(False, "debounce")

    if command is not None:
        if command.name != ctx.workflow and not (
            command.name == "agent-off" and ctx.workflow == "triage"
        ):
            return result(False, "wrong-command")
        if not authorize(ctx.gh, event.sender, ctx.maintainers):
            return result(False, "unauthorized")
        if command.name == "agent-off":
            return result(True, "agent-off")
    elif ctx.workflow == "triage":
        if event.name == "issue_comment" and event.action == "created":
            if event.sender.casefold() != event.author.casefold():
                return result(False, "third-party-comment")
            if "needs-info" not in event.labels:
                return result(False, "not-needs-info")
        elif event.name != "issues" or event.action not in {"opened", "edited", "reopened"}:
            return result(False, "unsupported-event")
    elif ctx.workflow == "fix":
        if (
            event.name != "issues"
            or event.action != "labeled"
            or event.label != "agent:fix"
            or event.is_pr
        ):
            return result(False, "unsupported-event")
        if not authorize(ctx.gh, event.sender, ctx.maintainers):
            return result(False, "unauthorized")
    elif ctx.workflow == "review":
        if (
            event.name != "pull_request_target"
            or event.action not in {"opened", "reopened", "ready_for_review", "synchronize"}
            or (event.action == "synchronize" and not _agent_pr(event))
        ):
            return result(False, "unsupported-event")
        if not _agent_pr(event) and event.author_association not in {
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
            "CONTRIBUTOR",
        }:
            return result(False, "needs-review-command")
    else:
        return result(False, "unsupported-event")

    if ctx.workflow == "review":
        if not event.is_pr:
            return result(False, "unsupported-event")
    if ctx.workflow == "fix" and event.is_pr and not (_agent_pr(event) and event.draft):
        return result(False, "not-agent-draft")

    day = ctx.now.astimezone(UTC).strftime("%Y%m%d")
    counts = []
    for name in (f"tier2-{day}", f"tier2-{day}-{event.author}"):
        artifacts = ctx.gh.paginate(
            "actions/artifacts", params={"name": name, "per_page": 100}, key="artifacts"
        )
        counts.append(sum(_mapping(artifact)["name"] == name for artifact in artifacts))
    if counts[0] >= limits["tier2_per_day"]:
        return result(True, "tier2-global-cap")
    if counts[1] >= limits["tier2_per_author_per_day"]:
        return result(True, "tier2-author-cap")
    return result(True, "ready", tier2=True)
