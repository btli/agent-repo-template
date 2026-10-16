import base64
import re
from urllib.parse import quote

from agent_repo.apply.actions import Action, ApplyContext, check_identity
from agent_repo.github import GitHubError
from agent_repo.markers import marker
from agent_repo.render import fenced
from agent_repo.scan import find_leaks


def _maintainer(ctx: ApplyContext, area: str | None, alerts: list[str]) -> list[Action]:
    labels = ["needs-maintainer"]
    if ctx.capped and "agent:triage-capped" in ctx.config.labels:
        labels.append("agent:triage-capped")
    actions = [
        Action(
            "add_labels",
            {
                "number": ctx.event.number,
                "labels": labels,
                "alerts": alerts,
                "bot_login": ctx.bot_login,
            },
        )
    ]
    owners = [
        owner.removeprefix("@")
        for candidate in ctx.config.areas
        if candidate.name == area
        for owner in candidate.owners
        if "/" not in owner
    ]
    if owners:
        actions.append(
            Action("assign", {"number": ctx.event.number, "assignees": list(dict.fromkeys(owners))})
        )
    return actions


def _off(ctx: ApplyContext) -> list[Action] | None:
    if not ctx.off_authorized:
        return None
    line = next(
        (line.strip() for line in (ctx.event.comment_body or "").splitlines() if line.strip()),
        "",
    )
    if (
        ctx.event.name != "issue_comment"
        or re.fullmatch(r"/agent[ \t]+off", line) is None
        or ctx.event.sender_is_bot
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", ctx.event.sender) is None
        or ctx.github is None
    ):
        return _maintainer(ctx, None, ["Off command rejected: authorization required."])
    try:
        maintainers = set()
        try:
            value = ctx.github.request("GET", "contents/MAINTAINERS", params={"ref": ctx.base_sha})
        except GitHubError as exc:
            if exc.status != 404:
                raise
        else:
            if value["encoding"] != "base64":
                raise ValueError("invalid maintainers encoding")
            content = base64.b64decode("".join(value["content"].split()), validate=True).decode()
            maintainers = {
                login.casefold()
                for line in content.splitlines()
                if (login := line.split("#", 1)[0].strip())
            }
        if ctx.event.sender.casefold() not in maintainers:
            permission = ctx.github.request(
                "GET", f"collaborators/{quote(ctx.event.sender, safe='')}/permission"
            )
            if not isinstance(permission, dict) or permission.get("role_name") not in {
                "admin",
                "maintain",
                "write",
            }:
                return _maintainer(ctx, None, ["Off command rejected: authorization required."])
    except Exception:
        return _maintainer(ctx, None, ["Off command rejected: authorization check failed."])
    return [
        Action(
            "comment",
            {
                "number": ctx.event.number,
                "bot_login": ctx.bot_login,
                "body": f"Agent automation is off for this item.\n\n{marker('off', ctx.run_id)}",
            },
        )
    ]


def _can_close(close: dict, ctx: ApplyContext) -> bool:
    thresholds = ctx.config.raw["thresholds"]
    if (
        not ctx.config.raw["close"]["enabled"]
        or close["p"] < thresholds["close_min"]
        or not close["tier2_agrees"]
    ):
        return False
    if close["kind"] == "out_of_scope":
        return close["docs_ref"] in {doc.url for doc in ctx.charter.docs_index}
    target = close["target"]
    if (
        type(target) is not int
        or target == ctx.event.number
        or close["lexical"] is None
        or close["lexical"] < thresholds["close_lexical_min"]
        or ctx.github is None
    ):
        return False
    try:
        item = ctx.github.request("GET", f"issues/{target}")
    except GitHubError as exc:
        if exc.status in {404, 410}:
            return False
        raise
    if not isinstance(item, dict) or item.get("state") not in {"open", "closed"}:
        raise ValueError("invalid duplicate response")
    return item["state"] == "open" and "pull_request" not in item


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _priority(value: object) -> str:
    return (
        value.strip().casefold().removeprefix("priority:").strip() if isinstance(value, str) else ""
    )


def _valid_answers(answers: dict, ctx: ApplyContext) -> bool:
    choices = {
        "type": {label for label in ctx.config.labels if label.startswith("type:")},
        "area": {area.name for area in ctx.config.areas} | {"unknown"},
        "priority": set(ctx.charter.priorities),
        "docs_ref": {doc.url for doc in ctx.charter.docs_index} | {"none"},
        # This context has no trusted duplicate candidates; never accept a model-supplied set.
        "duplicate_of": {"none"},
    }
    for question, answer in answers.items():
        value = answer["value"]
        if question in choices:
            if not isinstance(value, str) or value not in choices[question]:
                return False
        elif question in {"needs_info", "out_of_scope", "security_sensitive"}:
            if type(value) is not bool:
                return False
    return True


def plan_triage(decision: dict, ctx: ApplyContext) -> list[Action]:
    off = _off(ctx)
    if off is not None:
        return off
    errors = check_identity(decision, ctx, "decision")
    if errors:
        return _maintainer(ctx, None, errors)
    answers = decision["answers"]
    if not _valid_answers(answers, ctx):
        return _maintainer(ctx, None, ["Artifact rejected: invalid schema."])
    apply_min = ctx.config.raw["thresholds"]["apply_min"]
    area_answer = answers.get("area", {})
    area = area_answer.get("value") if area_answer.get("p", 0) >= apply_min else None
    area = area.removeprefix("area:") if isinstance(area, str) else None
    # Scan before rendering: escaping or truncation must not hide a credential.
    if find_leaks("".join(_strings(decision)), ctx.secrets, ctx.canary) or find_leaks(
        "".join(decision["answers"]), ctx.secrets, ctx.canary
    ):
        return _maintainer(ctx, area, ["Outbound scan alert: proposed action dropped."])
    if decision["security"]:
        actions = _maintainer(ctx, area, [])
        guidance = fenced("Please use SECURITY.md for the private security reporting process.", 200)
        body = f"Agent triage\n\n{guidance}\n\n{marker('triage', ctx.run_id)}"
        if find_leaks(body, ctx.secrets, ctx.canary):
            return _maintainer(ctx, area, ["Outbound scan alert: rendered action dropped."])
        actions.append(
            Action(
                "comment", {"number": ctx.event.number, "body": body, "bot_login": ctx.bot_login}
            )
        )
        return actions
    allowed = set(ctx.config.labels)
    selected: list[str] = []
    text: list[str] = []
    needs_maintainer = bool(decision["escalate"] or ctx.capped)
    proposed_priorities = {
        _priority(value)
        for value in [
            *decision["labels"],
            *decision["suggestions"]["labels"],
        ]
        if value.strip().casefold().startswith("priority:")
        or re.match(r"p\d", value.strip(), re.IGNORECASE)
    }
    if value := _priority(answers.get("priority", {}).get("value")):
        proposed_priorities.add(value)
    if decision["suggestions"]["priority"] is not None:
        proposed_priorities.add(decision["suggestions"]["priority"].casefold())
    if proposed_priorities - {"p2", "p3"}:
        actions = _maintainer(ctx, area, [])
        for priority in ("P0", "P1"):
            if priority.casefold() in proposed_priorities:
                text.append(f"Suggested priority: {priority}. A maintainer should confirm.")
        if text:
            content = "\n\n".join(text)
            body = f"Agent triage\n\n{fenced(content, 10000)}\n\n{marker('triage', ctx.run_id)}"
            if find_leaks(body, ctx.secrets, ctx.canary):
                return _maintainer(ctx, area, ["Outbound scan alert: rendered action dropped."])
            actions.append(
                Action(
                    "comment",
                    {"number": ctx.event.number, "body": body, "bot_login": ctx.bot_login},
                )
            )
        return actions
    for key in ("type", "area", "priority", "needs_info"):
        answer = answers.get(key)
        if (
            answer is None
            or answer["p"] < apply_min
            or (key == "priority" and not _priority(answer["value"]))
        ):
            needs_maintainer = True
    for key, prefix in (("type", "type:"), ("area", "area:"), ("priority", "")):
        answer = answers.get(key, {})
        value = answer.get("value")
        if not isinstance(value, str):
            continue
        label = value if not prefix or value.startswith(prefix) else prefix + value
        if (
            answer.get("p", 0) >= apply_min
            and label in decision["labels"]
            and label in allowed
            and (
                (key == "priority" and label in {"P2", "P3"})
                or (key != "priority" and label.startswith(prefix))
            )
        ):
            if key != "area" or value.removeprefix("area:") in {
                candidate.name for candidate in ctx.config.areas
            }:
                selected.append(label)
    info = answers.get("needs_info", {})
    if info.get("value") is True and info.get("p", 0) >= apply_min:
        request = decision["needs_info"]
        text.append(request["text"] if request is not None else "\n".join(ctx.charter.good_report))
        if "needs-info" in allowed:
            selected.append("needs-info")
    rca = decision["rca"]
    if rca:
        locations = "\n".join(
            f"{location['path']}:{location['line']}" for location in rca["locations"]
        )
        text.append(
            f"RCA hypothesis ({rca['confidence']} confidence):\n{rca['hypothesis']}\n"
            f"{locations}\nConfirm with: {rca['confirm_with']}"
        )
    close = decision["close"]
    try:
        can_close = close is not None and "agent:closed" in allowed and _can_close(close, ctx)
    except Exception:
        return _maintainer(ctx, area, ["Apply failed: duplicate lookup unavailable."])
    closure = ""
    if close is not None:
        if can_close:
            reference = (
                f"Duplicate of #{close['target']}"
                if close["kind"] == "duplicate"
                else close["docs_ref"]
            )
            link = (
                f"https://github.com/{ctx.event.repo}/issues/{close['target']}"
                if close["kind"] == "duplicate"
                else close["docs_ref"]
            )
            closure = (
                f"\n\nClosing: {reference}. If this is incorrect, "
                f"ask a maintainer to reopen this issue.\n\nReference: <{link}>"
            )
            selected.append("agent:closed")
        else:
            text.append("Suggested closure requires maintainer confirmation.")
            needs_maintainer = True
    actions: list[Action] = []
    if selected:
        actions.append(
            Action(
                "add_labels",
                {
                    "number": ctx.event.number,
                    "labels": list(dict.fromkeys(selected)),
                    "bot_login": ctx.bot_login,
                },
            )
        )
    if needs_maintainer:
        actions.extend(_maintainer(ctx, area, []))
    if text or can_close:
        content = "\n\n".join(text)
        prefix = "Agent triage\n\n"
        suffix = f"{closure}\n\n{marker('triage', ctx.run_id)}"
        budget = 10000 - len(prefix) - len(suffix)
        if budget < 8:
            return _maintainer(ctx, area, ["Apply failed: comment explanation exceeds limit."])
        prose = fenced(content, budget)
        if len(prose) > budget:
            # Budget for both fences as well as the neutralized model prose.
            overhead = 2 * len(prose.partition("\n")[0]) + 2
            prose = fenced(content, max(0, budget - overhead))
        body = prefix + prose + suffix
        if find_leaks(body, ctx.secrets, ctx.canary):
            return _maintainer(ctx, area, ["Outbound scan alert: rendered action dropped."])
        actions.append(
            Action(
                "comment", {"number": ctx.event.number, "body": body, "bot_login": ctx.bot_login}
            )
        )
    if can_close:
        actions.append(
            Action(
                "close",
                {
                    "number": ctx.event.number,
                    "duplicate_target": close["target"] if close["kind"] == "duplicate" else None,
                    "comment_body": body,
                    "bot_login": ctx.bot_login,
                },
            )
        )
    return actions
