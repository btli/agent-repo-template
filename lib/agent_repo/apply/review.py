import re
from pathlib import Path

from agent_repo.apply.actions import (
    Action,
    ApplyContext,
    _AutomationDisabled,
    _check_enabled,
    check_identity,
    execute,
)
from agent_repo.apply.triage import _strings
from agent_repo.github import GitHub
from agent_repo.render import fenced
from agent_repo.scan import find_leaks


def _alert(ctx: ApplyContext, message: str) -> list[Action]:
    return [
        Action(
            "add_labels",
            {
                "number": ctx.event.number,
                "labels": ["needs-maintainer"],
                "alerts": [message],
                "bot_login": ctx.bot_login,
            },
        )
    ]


def plan_review(review: dict, ctx: ApplyContext, index: dict[str, set[int]]) -> list[Action]:
    try:
        errors = check_identity(review, ctx, "review")
    except (KeyError, TypeError, ValueError):
        return []
    if errors or not ctx.event.is_pr or ctx.event.head_sha is None:
        return []
    if re.fullmatch(r"[0-9a-f]{40}", ctx.event.head_sha) is None:
        return []
    if review["head_sha"] != ctx.event.head_sha:
        return []
    if find_leaks("".join(_strings(review)), ctx.secrets, ctx.canary):
        return _alert(ctx, "Outbound scan alert: proposed review dropped.")

    summary = [review["summary"]]
    if not review["scope_ok"]:
        summary.append("The change may exceed the intended scope.")
    if review["tests_missing"]:
        summary.append("Tests appear to be missing for changed behavior.")
    if review["docs_needed"]:
        summary.append("Documentation may need an update.")
    if review["escalate"] is not None:
        summary.append(f"Maintainer review requested: {review['escalate']['reason']}")

    inline = []
    for finding in review["findings"]:
        if finding["line"] in index.get(finding["path"], set()):
            body = fenced(f"{finding['severity'].upper()}: {finding['body']}", 2000)
            inline.append(
                {
                    "path": finding["path"],
                    "line": finding["line"],
                    "side": "RIGHT",
                    "body": body,
                }
            )
        else:
            summary.append(
                f"Finding outside the diff ({finding['severity']}) at "
                f"{finding['path']}:{finding['line']}: {finding['body']}"
            )
    actions = []
    if review["escalate"] is not None or not review["scope_ok"]:
        actions.append(
            Action(
                "add_labels",
                {
                    "number": ctx.event.number,
                    "labels": ["needs-maintainer"],
                    "bot_login": ctx.bot_login,
                },
            )
        )
    body = "Automated review\n\n" + fenced("\n\n".join(filter(None, summary)), 12000)
    if find_leaks(body + "".join(item["body"] for item in inline), ctx.secrets, ctx.canary):
        return _alert(ctx, "Outbound scan alert: rendered review dropped.")
    actions.append(
        Action(
            "review",
            {
                "number": ctx.event.number,
                "bot_login": ctx.bot_login,
                "event": "COMMENT",
                "commit_id": ctx.event.head_sha,
                "body": body,
                "comments": inline,
            },
        )
    )
    return actions


def _summary(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"Apply review\n- {message}\n")


def apply_review(
    review: dict,
    ctx: ApplyContext,
    index: dict[str, set[int]],
    gh: GitHub | None,
    mode: str,
    summary_path: Path,
) -> list[str]:
    actions = plan_review(review, ctx, index)
    if not actions:
        _summary(summary_path, "No review posted: artifact or head identity rejected.")
        return []
    labels = [action for action in actions if action.kind != "review"]
    reviews = [action for action in actions if action.kind == "review"]
    if mode == "live" and gh is not None:
        number = actions[0].payload["number"]
        expected_head = ctx.event.head_sha
        try:
            pull = gh.request("GET", f"pulls/{number}")
            if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != expected_head:
                _summary(summary_path, "No review posted: pull request head moved.")
                return []
            _check_enabled(actions, gh, number)
            pull = gh.request("GET", f"pulls/{number}")
            if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != expected_head:
                _summary(summary_path, "No review posted: pull request head moved.")
                return []
        except _AutomationDisabled:
            alert = "Apply skipped: automation disabled for this issue."
            _summary(summary_path, alert)
            return [alert]
        except Exception:
            alert = "Apply failed: pull request review stopped; maintainer attention required."
            _summary(summary_path, alert)
            return [alert]
    alerts = execute(labels, gh, mode, summary_path) if labels else []
    if not reviews:
        return alerts
    if any(alert.startswith(("Apply skipped", "Apply failed")) for alert in alerts):
        return alerts
    if mode == "dry-run":
        execute(reviews, gh, mode, summary_path)
        return alerts
    if mode != "live" or gh is None:
        return [*alerts, "Apply failed: GitHub client required."]

    action = reviews[0]
    number = action.payload["number"]
    head_sha = action.payload["commit_id"]
    try:
        pull = gh.request("GET", f"pulls/{number}")
        if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != head_sha:
            _summary(summary_path, "No review posted: pull request head moved.")
            return alerts
        _check_enabled([action], gh, number)
        pull = gh.request("GET", f"pulls/{number}")
        if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != head_sha:
            _summary(summary_path, "No review posted: pull request head moved.")
            return alerts
        gh.request(
            "POST",
            f"pulls/{number}/reviews",
            body={
                "commit_id": head_sha,
                "body": action.payload["body"],
                "event": "COMMENT",
                "comments": action.payload["comments"],
            },
        )
    except _AutomationDisabled:
        alert = "Apply skipped: automation disabled for this issue."
        _summary(summary_path, alert)
        return [*alerts, alert]
    except Exception:
        alert = "Apply failed: pull request review stopped; maintainer attention required."
        _summary(summary_path, alert)
        return [*alerts, alert]
    _summary(summary_path, "COMMENT review posted.")
    return alerts
