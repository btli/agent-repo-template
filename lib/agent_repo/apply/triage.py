from urllib.parse import quote

from agent_repo.apply.actions import Action, ApplyContext, check_identity
from agent_repo.markers import marker
from agent_repo.render import fenced
from agent_repo.scan import find_leaks


def _maintainer(ctx: ApplyContext, area: str | None, alerts: list[str]) -> list[Action]:
    actions = [
        Action(
            "add_labels",
            {"number": ctx.event.number, "labels": ["needs-maintainer"], "alerts": alerts},
        )
    ]
    owners = [
        owner.removeprefix("@")
        for candidate in ctx.config.areas
        if candidate.name == area
        for owner in candidate.owners
    ]
    if owners:
        actions.append(
            Action("assign", {"number": ctx.event.number, "assignees": list(dict.fromkeys(owners))})
        )
    return actions


def _off(ctx: ApplyContext) -> list[Action] | None:
    if not ctx.off_authorized:
        return None
    if (
        ctx.event.name != "issue_comment"
        or (ctx.event.comment_body or "").strip() != "/agent off"
        or ctx.github is None
    ):
        return _maintainer(ctx, None, ["Off command rejected: authorization required."])
    try:
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
    item = ctx.github.request("GET", f"issues/{target}")
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


def plan_triage(decision: dict, ctx: ApplyContext) -> list[Action]:
    off = _off(ctx)
    if off is not None:
        return off
    errors = check_identity(decision, ctx, "decision")
    if errors:
        return _maintainer(ctx, None, errors)
    answers = decision["answers"]
    apply_min = ctx.config.raw["thresholds"]["apply_min"]
    area_answer = answers.get("area", {})
    area = area_answer.get("value") if area_answer.get("p", 0) >= apply_min else None
    area = area.removeprefix("area:") if isinstance(area, str) else None
    # Scan before rendering: escaping or truncation must not hide a credential.
    if find_leaks("".join(_strings(decision)), ctx.secrets, ctx.canary):
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
    proposed_priorities = [
        *decision["labels"],
        decision["suggestions"]["priority"],
        answers.get("priority", {}).get("value"),
    ]
    for priority in ("P0", "P1"):
        if priority in proposed_priorities:
            text.append(f"Suggested priority: {priority}. A maintainer should confirm.")
            needs_maintainer = True
    for key in ("type", "area", "priority", "needs_info"):
        answer = answers.get(key)
        if answer is None or answer["p"] < apply_min:
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
        return _maintainer(ctx, None, ["Apply failed: duplicate lookup unavailable."])
    if close is not None:
        if can_close:
            reference = (
                f"Duplicate of #{close['target']}"
                if close["kind"] == "duplicate"
                else close["docs_ref"]
            )
            text.append(
                f"Closing: {reference}. If this is incorrect, "
                "ask a maintainer to reopen this issue."
            )
            selected.append("agent:closed")
        else:
            text.append("Suggested closure requires maintainer confirmation.")
            needs_maintainer = True
    if ctx.capped and "agent:triage-capped" in allowed:
        selected.append("agent:triage-capped")
    actions: list[Action] = []
    if selected:
        actions.append(
            Action(
                "add_labels", {"number": ctx.event.number, "labels": list(dict.fromkeys(selected))}
            )
        )
    if needs_maintainer:
        actions.extend(_maintainer(ctx, area, []))
    if text:
        content = "\n\n".join(text)
        body = f"Agent triage\n\n{fenced(content, 10000)}"
        if can_close:
            link = (
                f"https://github.com/{ctx.event.repo}/issues/{close['target']}"
                if close["kind"] == "duplicate"
                else close["docs_ref"]
            )
            body += f"\n\nReference: <{link}>"
        body += f"\n\n{marker('triage', ctx.run_id)}"
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
