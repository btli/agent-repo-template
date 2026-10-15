import os
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

from agent_repo.config import RepoConfig
from agent_repo.event import Event
from agent_repo.github import GitHub, GitHubError
from agent_repo.markers import find_markers

# The triage propose job uploads a zero-byte artifact with this exact name when it executes.
TRIAGE_MARKER_NAME = "agent-triage-{issue_number}"


class _RequestBudgetExceeded(Exception):
    pass


class _CounterOverflow(Exception):
    pass


@dataclass
class _Budget:
    gh: GitHub
    remaining: int = 30

    def request(self, method: str, path: str, *, params: dict | None = None) -> object:
        if self.remaining <= 0:
            raise _RequestBudgetExceeded
        self.remaining -= 1
        return self.gh.request(method, path, params=params)


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


def authorize(gh: GitHub | _Budget, login: str, maintainers: frozenset[str]) -> bool:
    if login.casefold().endswith("[bot]") or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", login
    ):
        return False
    if login.casefold() in {name.casefold() for name in maintainers}:
        return True
    try:
        data = gh.request("GET", f"collaborators/{quote(login, safe='')}/permission")
    except GitHubError as exc:
        if exc.status == 404:
            return False
        raise
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


def _agent_pr(event: Event, bot_login: str) -> bool:
    return bool(
        event.is_pr
        and event.author.casefold() == bot_login.casefold()
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
    except _RequestBudgetExceeded:
        return GateResult(False, "request-budget", False, False, command, canary)
    except _CounterOverflow:
        return GateResult(False, "counter-overflow", False, False, command, canary)
    except GitHubError:
        return GateResult(False, "api-error", False, False, command, canary)
    except (ValueError, TypeError, KeyError, AttributeError):
        return GateResult(False, "invalid-metadata", False, False, command, canary)


def _evaluate(event: Event, ctx: GateContext, command: Command | None, canary: str) -> GateResult:
    gh = _Budget(ctx.gh)

    def result(run: bool, reason: str, tier2: bool = False, capped: bool = False) -> GateResult:
        return GateResult(run, reason, tier2, capped, command, canary)

    if not ctx.enabled:
        return result(False, "disabled")
    if not ctx.bot_login or not event.sender or not event.author or event.number <= 0:
        return result(False, "invalid-metadata")
    agent_review = (
        ctx.workflow == "review"
        and event.name == "pull_request_target"
        and event.action in {"opened", "reopened", "ready_for_review", "synchronize"}
        and _agent_pr(event, ctx.bot_login)
        and event.sender.casefold() == ctx.bot_login.casefold()
    )
    if event.sender.casefold() == ctx.bot_login.casefold() and not agent_review:
        return result(False, "self")
    if (event.sender_is_bot or event.sender.casefold().endswith("[bot]")) and not agent_review:
        return result(False, "bot")
    if event.name == "issue_comment" and event.action != "created":
        return result(False, "unsupported-event")
    if ctx.workflow == "triage" and event.author.casefold().endswith("[bot]"):
        return result(False, "bot")
    if "agent:skip" in event.labels:
        return result(False, "skip-label")
    if ctx.workflow == "triage" and event.is_pr:
        return result(False, "unsupported-event")
    if ctx.workflow == "review" and event.draft and not _agent_pr(event, ctx.bot_login):
        return result(False, "draft")
    if (
        ctx.workflow == "triage"
        and event.name == "issues"
        and event.action == "edited"
        and not ({"title", "body"} & set(event.edited_fields))
    ):
        return result(False, "no-op-edit")

    if (
        command is not None
        and command.name != ctx.workflow
        and not (command.name == "agent-off" and ctx.workflow == "triage")
    ):
        return result(False, "wrong-command")
    if ctx.workflow == "review" and not event.is_pr:
        return result(False, "unsupported-event")
    if command is not None:
        if not authorize(gh, event.sender, ctx.maintainers):
            return result(False, "unauthorized")
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
        if not authorize(gh, event.sender, ctx.maintainers):
            return result(False, "unauthorized")
    elif ctx.workflow == "review":
        if (
            event.name != "pull_request_target"
            or event.action not in {"opened", "reopened", "ready_for_review", "synchronize"}
            or (event.action == "synchronize" and not _agent_pr(event, ctx.bot_login))
        ):
            return result(False, "unsupported-event")
        if not _agent_pr(event, ctx.bot_login) and event.author_association not in {
            "OWNER",
            "MEMBER",
            "COLLABORATOR",
            "CONTRIBUTOR",
        }:
            return result(False, "needs-review-command")
    else:
        return result(False, "unsupported-event")

    if event.name == "issue_comment" and event.is_pr:
        # Comment webhooks contain a PR link, so fetch trusted head and draft metadata.
        pull = _mapping(gh.request("GET", f"pulls/{event.number}"))
        head = _mapping(pull.get("head"))
        event = replace(
            event,
            head_ref=head.get("ref"),
            head_repo=_mapping(head.get("repo") or {}).get("full_name"),
            draft=pull["draft"],
            author=_mapping(pull["user"])["login"],
        )
    if ctx.workflow == "review" and event.draft and not _agent_pr(event, ctx.bot_login):
        return result(False, "draft")
    if (
        ctx.workflow == "fix"
        and event.is_pr
        and not (_agent_pr(event, ctx.bot_login) and event.draft)
    ):
        return result(False, "not-agent-draft")

    workflow_path = ""
    if ctx.workflow == "triage":
        # Gate and propose run in the same caller workflow.
        workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
        prefix = f"{event.repo}/"
        workflow_path, separator, ref = workflow_ref.removeprefix(prefix).partition("@")
        if not workflow_ref.startswith(prefix) or not separator or not ref:
            return result(False, "invalid-metadata")

    comments = _items(gh, f"issues/{event.number}/comments", None)
    for comment in comments:
        item = _mapping(comment)
        login = _mapping(item.get("user") or {}).get("login", "")
        if login.casefold() == ctx.bot_login.casefold() and any(
            marker.role == "off" for marker in find_markers(item.get("body") or "")
        ):
            return result(False, "off")

    if command is not None and command.name == "agent-off":
        return result(True, "agent-off")

    repository_id = _mapping(gh.request("GET", f"/repos/{event.repo}"))["id"]
    limits = ctx.config.raw["limits"]
    if ctx.workflow == "triage":
        prior = 0
        verified = 0
        latest = None
        cap = limits["triage_runs_per_issue"]
        if cap <= 0:
            return result(False, "cycle-cap", capped=True)
        name = TRIAGE_MARKER_NAME.format(issue_number=event.number)
        for artifact in _items(gh, "actions/artifacts", "artifacts", {"name": name}, max_pages=10):
            if artifact["name"] != name:
                continue
            run = _mapping(artifact["workflow_run"])
            if run["repository_id"] != repository_id or run["head_repository_id"] != repository_id:
                continue
            if verified >= cap + 1:
                raise _CounterOverflow
            verified += 1
            source = _mapping(gh.request("GET", f"actions/runs/{run['id']}"))
            if source["path"] != workflow_path:
                continue
            # Expired markers prove execution; the cap holds only for the marker retention window.
            prior += 1
            started = _timestamp(artifact["created_at"])
            latest = max(latest, started) if latest else started
            if prior >= cap:
                return result(False, "cycle-cap", capped=True)
        if (
            event.action == "edited"
            and latest
            and (ctx.now - latest < timedelta(minutes=limits["edit_debounce_minutes"]))
        ):
            return result(False, "debounce")

    day = ctx.now.astimezone(UTC).strftime("%Y%m%d")
    for name, limit, reason in (
        (f"tier2-{day}", limits["tier2_per_day"], "tier2-global-cap"),
        (f"tier2-{day}-{event.author}", limits["tier2_per_author_per_day"], "tier2-author-cap"),
    ):
        count = 0
        if limit <= 0:
            return result(True, reason)
        for artifact in _items(gh, "actions/artifacts", "artifacts", {"name": name}):
            if artifact["name"] != name:
                continue
            run = _mapping(artifact["workflow_run"])
            if run["repository_id"] == repository_id and run["head_repository_id"] == repository_id:
                count += 1
                if count >= limit:
                    return result(True, reason)
    return result(True, "ready", tier2=True)


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("invalid timestamp")
    return timestamp


def _items(
    gh: _Budget,
    path: str,
    key: str | None,
    params: dict | None = None,
    *,
    max_pages: int | None = None,
) -> Iterator[dict]:
    page = 1
    while True:
        data = gh.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
        items = _mapping(data)[key] if key is not None else data
        if not isinstance(items, list):
            raise ValueError("invalid metadata")
        for item in items:
            yield _mapping(item)
        if len(items) < 100 or (
            max_pages is not None and page * 100 >= _mapping(data)["total_count"]
        ):
            return
        if max_pages is not None and page >= max_pages:
            raise _CounterOverflow
        page += 1
