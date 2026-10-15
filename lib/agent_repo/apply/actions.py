import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from agent_repo.charter import Charter
from agent_repo.config import RepoConfig
from agent_repo.event import Event
from agent_repo.github import GitHub
from agent_repo.schema import load_schema, validate


@dataclass(frozen=True)
class Action:
    kind: Literal[
        "add_labels", "remove_labels", "assign", "comment", "close", "review", "push", "open_pr"
    ]
    payload: dict


@dataclass(frozen=True)
class ApplyContext:
    event: Event
    config: RepoConfig
    charter: Charter
    run_id: str
    base_sha: str
    secrets: list[str]
    canary: str
    capped: bool
    github: GitHub | None = None
    off_authorized: bool = False
    bot_login: str = ""


def canonical_bot_login(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", value, re.IGNORECASE) is None:
        return ""
    return value.casefold().removesuffix("[bot]") + "[bot]"


def check_identity(artifact: dict, ctx: ApplyContext, schema: str) -> list[str]:
    # Validator paths can contain untrusted keys, so never publish its diagnostics.
    if validate(artifact, load_schema(schema)):
        return ["Artifact rejected: invalid schema."]
    if artifact["run_id"] != ctx.run_id or artifact["base_sha"] != ctx.base_sha:
        return ["Artifact rejected: identity mismatch."]
    return []


def _summary(path: Path, actions: list[Action], mode: str, alerts: list[str]) -> None:
    lines = [f"Apply ({'live' if mode == 'live' else 'dry-run'})"]
    for action in actions:
        payload = action.payload
        details = {"target": payload["number"]}
        if action.kind in {"add_labels", "remove_labels"}:
            details["labels"] = payload["labels"]
        elif action.kind == "assign":
            details["assignees"] = payload["assignees"]
        elif action.kind == "comment":
            details["body_chars"] = len(payload["body"])
        elif action.kind == "close":
            duplicate_target = payload.get("duplicate_target")
            details["kind"] = "duplicate" if duplicate_target is not None else "out_of_scope"
            if duplicate_target is not None:
                details["duplicate_target"] = duplicate_target
        lines.append(f"- Planned: {action.kind} {json.dumps(details, sort_keys=True)}")
    lines.extend(f"- ALERT: {alert}" for alert in alerts)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def execute(actions: list[Action], gh: GitHub | None, mode: str, summary_path: Path) -> list[str]:
    alerts = [alert for action in actions for alert in action.payload.get("alerts", [])]
    if mode not in {"live", "dry-run"}:
        alerts.append("Apply failed: invalid mode.")
    elif mode == "live" and gh is None:
        alerts.append("Apply failed: GitHub client required.")
    elif mode == "live" and actions:
        try:
            _execute_live(actions, gh)
        except Exception:
            # Transport and payload errors must never echo credentials or model text.
            alerts.append("Apply failed: GitHub action stopped; maintainer attention required.")
            numbers = {action.payload.get("number") for action in actions}
            if len(numbers) == 1:
                number = numbers.pop()
                if type(number) is int and number > 0:
                    try:
                        gh.request(
                            "POST", f"issues/{number}/labels", body={"labels": ["needs-maintainer"]}
                        )
                    except Exception:
                        alerts.append("Apply failed: maintainer label could not be added.")
    _summary(summary_path, actions, mode, alerts)
    return alerts


def _execute_live(actions: list[Action], gh: GitHub) -> None:
    numbers = {action.payload["number"] for action in actions}
    if len(numbers) != 1 or any(type(number) is not int or number < 1 for number in numbers):
        raise ValueError("invalid action target")
    for action in actions:
        if action.kind in {"comment", "close"} and not canonical_bot_login(
            action.payload.get("bot_login", "")
        ):
            raise ValueError("trusted bot identity required")
    number = numbers.pop()
    path = f"issues/{number}"
    item = gh.request("GET", path)
    if not isinstance(item, dict):
        raise ValueError("invalid issue response")
    existing_labels = {label["name"] for label in item["labels"]}
    existing_owners = {owner["login"] for owner in item["assignees"]}
    comments = (
        gh.paginate(f"{path}/comments")
        if any(a.kind in {"comment", "close"} for a in actions)
        else []
    )
    completed_comments = {
        (canonical_bot_login(comment["user"]["login"]), comment["body"])
        for comment in comments
        if isinstance(comment.get("user"), dict)
        and comment["user"].get("type") == "Bot"
        and canonical_bot_login(comment["user"].get("login", ""))
    }
    planned_comments = {
        (canonical_bot_login(action.payload.get("bot_login", "")), action.payload["body"])
        for action in actions
        if action.kind == "comment"
    }
    for action in actions:
        if action.kind == "close":
            explanation = (
                canonical_bot_login(action.payload.get("bot_login", "")),
                action.payload.get("comment_body"),
            )
            if explanation not in completed_comments and explanation not in planned_comments:
                raise ValueError("closure explanation required")
    order = {"add_labels": 0, "remove_labels": 1, "assign": 2, "comment": 3, "close": 4}
    if any(action.kind not in order for action in actions):
        raise ValueError("unsupported action")
    for action in actions:
        if action.kind == "close" and action.payload.get("duplicate_target") is not None:
            target = gh.request("GET", f"issues/{action.payload['duplicate_target']}")
            if (
                not isinstance(target, dict)
                or target.get("state") != "open"
                or "pull_request" in target
            ):
                raise ValueError("duplicate target no longer open")
    for action in sorted(actions, key=lambda action: order[action.kind]):
        payload = action.payload
        if action.kind == "add_labels":
            missing = [label for label in payload["labels"] if label not in existing_labels]
            if missing:
                gh.request("POST", f"{path}/labels", body={"labels": missing})
                existing_labels.update(missing)
        elif action.kind == "remove_labels":
            for label in payload["labels"]:
                if label in existing_labels:
                    gh.request("DELETE", f"{path}/labels/{quote(label, safe='')}")
                    existing_labels.remove(label)
        elif action.kind == "assign":
            missing = [owner for owner in payload["assignees"] if owner not in existing_owners]
            if missing:
                gh.request("POST", f"{path}/assignees", body={"assignees": missing})
                existing_owners.update(missing)
        elif action.kind == "comment":
            identity = (canonical_bot_login(payload.get("bot_login", "")), payload["body"])
            if identity not in completed_comments:
                gh.request("POST", f"{path}/comments", body={"body": payload["body"]})
                completed_comments.add(identity)
        elif action.kind == "close" and item.get("state") == "open":
            if (
                canonical_bot_login(payload.get("bot_login", "")),
                payload.get("comment_body"),
            ) not in completed_comments:
                raise ValueError("closure explanation not posted")
            gh.request("PATCH", path, body={"state": "closed", "state_reason": "not_planned"})
            item["state"] = "closed"
