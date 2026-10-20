from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Literal

from agent_repo.charter import Charter
from agent_repo.config import RepoConfig, is_llm_only
from agent_repo.event import Event
from agent_repo.gate import _Budget, _items, _mapping
from agent_repo.github import GitHub
from agent_repo.systemone import Answer, Question
from agent_repo.tier0 import Candidate


@dataclass(frozen=True)
class Proposal:
    labels: tuple[str, ...]
    suggest_priority: str | None
    needs_info: bool
    escalate: bool
    escalate_reasons: tuple[str, ...]
    rca_candidate: bool
    close: dict | None
    security: bool
    stop: bool


def _decision_note(text: str) -> str:
    if len(text) <= 500:
        return text
    return text[:499] + "…"


def build_questions(
    charter: Charter, config: RepoConfig, candidates: list[Candidate]
) -> list[Question]:
    type_options = {
        label.removeprefix("type:"): label.removeprefix("type:")
        for label in config.labels
        if label.startswith("type:")
    }
    area_options = {area.name: area.description for area in config.areas}
    area_options["unknown"] = "Unknown or unclear area"
    priority_options = {
        priority: charter.priorities[priority] for priority in ("P0", "P1", "P2", "P3")
    }
    docs_options = {doc.url: doc.title for doc in charter.docs_index}
    docs_options["none"] = "No matching documentation"

    questions = [
        Question("type", "choice", "Choose the issue type.", type_options),
        Question("area", "choice", "Choose the most relevant area.", area_options),
        Question(
            "priority", "choice", "Choose the priority using these criteria.", priority_options
        ),
        Question(
            "needs_info",
            "noul",
            "Does the report lack information needed to act? The charter checklist is: "
            + "; ".join(charter.good_report),
            None,
        ),
        Question(
            "out_of_scope",
            "noul",
            "Is this request outside the project scope? "
            + "; ".join(item.text for item in charter.out_of_scope),
            None,
        ),
        Question(
            "docs_ref", "choice", "Choose the most relevant documentation reference.", docs_options
        ),
    ]
    if candidates:
        duplicate_options = {"none": "No likely duplicate"}
        duplicate_options.update(
            {f"#{candidate.number}": candidate.title for candidate in candidates}
        )
        questions.append(
            Question(
                "duplicate_of", "choice", "Choose a likely duplicate issue.", duplicate_options
            )
        )
    questions.append(
        Question("security_sensitive", "noul", "Does this report contain a security issue?", None)
    )
    return questions


def _value(answers: dict[str, Answer | None], question: str) -> tuple[object, float]:
    answer = answers.get(question)
    if not isinstance(answer, Answer):
        return None, 0.0
    value: object = answer.value
    probability = answer.p
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not isfinite(probability)
        or not 0 <= probability <= 1
    ):
        return None, 0.0
    if question in {"needs_info", "out_of_scope", "security_sensitive"}:
        if value == "yes":
            value = True
        elif value == "no":
            value = False
        elif not isinstance(value, bool):
            return None, 0.0
    elif not isinstance(value, str):
        return None, 0.0
    return value, probability


def _choice_label(question: str, value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.startswith(f"{question}:") else f"{question}:{value}"


def route(
    answers: dict[str, Answer | None],
    config: RepoConfig,
    event: Event,
    tier2_allowed: bool,
    candidates: list[Candidate],
    *,
    tier2_check_failed: bool = False,
) -> Proposal:
    thresholds = config.raw["thresholds"]
    apply_min = thresholds["apply_min"]
    labels: list[str] = []
    reasons: list[str] = []
    low_action_answer = False
    suggest_priority = None

    for question in ("type", "area"):
        value, probability = _value(answers, question)
        if probability >= apply_min:
            label = _choice_label(question, value)
            allowed = (
                {item for item in config.labels if item.startswith("type:")}
                if question == "type"
                else {f"area:{area.name}" for area in config.areas} | {"area:unknown"}
            )
            if label in allowed:
                labels.append(label)
            else:
                low_action_answer = True
                reasons.append(question)
        else:
            low_action_answer = True
            reasons.append(question)

    priority, priority_p = _value(answers, "priority")
    if priority_p < apply_min or priority not in {"P0", "P1", "P2", "P3"}:
        low_action_answer = True
        reasons.append("priority")
    elif priority in {"P0", "P1"}:
        suggest_priority = priority
        reasons.append("priority")
    elif priority in config.labels:
        labels.append(priority)

    needs_info_value, needs_info_p = _value(answers, "needs_info")
    needs_info = needs_info_value is True and needs_info_p >= apply_min
    if needs_info:
        labels.append("needs-info")
    if needs_info_p < apply_min or type(needs_info_value) is not bool:
        low_action_answer = True
        if "needs_info" not in reasons:
            reasons.append("needs_info")

    security_value, security_p = _value(answers, "security_sensitive")
    security = security_value is True and security_p >= apply_min
    security_uncertain = (
        not is_llm_only(config)
        and not security
        and (security_value is None or security_p < apply_min)
    )
    if security:
        labels.append("needs-maintainer")
        reasons.append("security")
    elif security_uncertain:
        labels.append("needs-maintainer")
        reasons.append("security-uncertain")
    elif security_p < apply_min or type(security_value) is not bool:
        low_action_answer = True
        reasons.append("security_sensitive")

    type_value, type_p = _value(answers, "type")
    rca_allowed = config.raw["triage"]["rca_authors"] == "all" or event.author_association in {
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
        "CONTRIBUTOR",
    }
    rca_candidate = (
        type_value == "bug"
        and type_p >= apply_min
        and needs_info_value is False
        and needs_info_p >= apply_min
        and rca_allowed
    )
    if rca_candidate:
        reasons.append("rca")

    close = None
    duplicate_value, duplicate_p = _value(answers, "duplicate_of")
    duplicate = next(
        (candidate for candidate in candidates if duplicate_value == f"#{candidate.number}"), None
    )
    close_enabled = config.raw["close"]["enabled"]
    close_suggestion = False
    out_of_scope, out_of_scope_p = _value(answers, "out_of_scope")
    docs_ref, docs_ref_p = _value(answers, "docs_ref")
    out_of_scope_selected = (
        out_of_scope is True and isinstance(docs_ref, str) and docs_ref != "none"
    )
    duplicate_selected = duplicate is not None
    duplicate_eligible = (
        duplicate_selected
        and duplicate_p >= thresholds["close_min"]
        and duplicate.similarity >= thresholds["close_lexical_min"]
    )
    out_of_scope_eligible = (
        out_of_scope_selected
        and out_of_scope_p >= thresholds["close_min"]
        and docs_ref_p >= thresholds["close_min"]
    )
    if close_enabled and not security_uncertain:
        if duplicate_eligible:
            close = {
                "kind": "duplicate",
                "target": duplicate.number,
                "docs_ref": None,
                "p": duplicate_p,
                "lexical": duplicate.similarity,
                "tier2_agrees": False,
            }
        elif out_of_scope_eligible:
            close = {
                "kind": "out_of_scope",
                "target": None,
                "docs_ref": docs_ref,
                "p": min(out_of_scope_p, docs_ref_p),
                "lexical": None,
                "tier2_agrees": False,
            }
        elif duplicate_selected:
            close_suggestion = True
            close = {
                "kind": "duplicate",
                "target": duplicate.number,
                "docs_ref": None,
                "p": duplicate_p,
                "lexical": duplicate.similarity,
                "tier2_agrees": False,
            }
        elif out_of_scope_selected:
            close_suggestion = True
            close = {
                "kind": "out_of_scope",
                "target": None,
                "docs_ref": docs_ref,
                "p": min(out_of_scope_p, docs_ref_p),
                "lexical": None,
                "tier2_agrees": False,
            }
    close_vote = close is not None and close_enabled and not close_suggestion
    if close_vote:
        reasons.append("close-vote")

    unique_reasons = ["security-uncertain"] if security_uncertain else list(dict.fromkeys(reasons))
    needs_tier2 = bool(unique_reasons) and not security_uncertain
    if needs_tier2 and not tier2_allowed and not security:
        unique_reasons.append("tier2-check-failed" if tier2_check_failed else "tier2-cap")
        needs_tier2 = False
        if tier2_check_failed or low_action_answer or suggest_priority or close_vote:
            labels.append("needs-maintainer")
    if security:
        needs_tier2 = False
    if close_suggestion:
        labels.append("needs-maintainer")
        unique_reasons.append("close-suggestion")

    return Proposal(
        tuple(dict.fromkeys(labels)),
        suggest_priority,
        needs_info,
        needs_tier2,
        tuple(unique_reasons),
        rca_candidate,
        close,
        security,
        False,
    )


def to_decision(
    proposal: Proposal,
    answers: dict[str, Answer | None],
    run_id: str,
    base_sha: str,
    tier: int,
) -> dict:
    encoded_answers = {}
    for question, answer in answers.items():
        if isinstance(answer, Answer):
            value, probability = _value(answers, question)
            encoded_answers[question] = {"value": value, "p": probability}
    notes = []
    if proposal.escalate_reasons:
        notes.append("Escalation reasons: " + ", ".join(proposal.escalate_reasons))
    duplicate = encoded_answers.get("duplicate_of", {}).get("value")
    if isinstance(duplicate, str) and duplicate != "none" and proposal.close is None:
        notes.append(f"Suggested duplicate: {duplicate}. Automatic closing was not proposed.")
    docs_ref = encoded_answers.get("docs_ref", {}).get("value")
    out_of_scope = encoded_answers.get("out_of_scope", {}).get("value") is True
    docs_ref_in_close = (
        proposal.close is not None
        and proposal.close.get("kind") == "out_of_scope"
        and proposal.close.get("docs_ref") == docs_ref
    )
    if isinstance(docs_ref, str) and docs_ref != "none" and not docs_ref_in_close:
        if out_of_scope:
            notes.append(
                f"Suggested out-of-scope close with documentation reference {docs_ref}; "
                "automatic closing was not proposed."
            )
        else:
            notes.append(f"Suggested documentation reference: {docs_ref}.")
    if proposal.rca_candidate:
        notes.append("This sufficiently specified bug is a candidate for root cause analysis.")
    if proposal.security:
        escalate = {"to": "maintainer", "reason": "security"}
    elif "security-uncertain" in proposal.escalate_reasons:
        escalate = {"to": "maintainer", "reason": "security-uncertain"}
    elif "credential-in-issue" in proposal.escalate_reasons:
        escalate = {"to": "maintainer", "reason": "credential-in-issue"}
    elif "context-unscannable" in proposal.escalate_reasons:
        escalate = {"to": "maintainer", "reason": "context-unscannable"}
    elif proposal.escalate:
        escalate = None
    elif {"tier2-cap", "tier2-check-failed"} & set(
        proposal.escalate_reasons
    ) and "needs-maintainer" in proposal.labels:
        escalate = {
            "to": "maintainer",
            "reason": ", ".join(proposal.escalate_reasons)[:500] or "Decision requires review.",
        }
    elif "close-suggestion" in proposal.escalate_reasons:
        escalate = {"to": "maintainer", "reason": "close-suggestion"}
    else:
        escalate = None
    return {
        "schema_version": "1",
        "run_id": run_id,
        "base_sha": base_sha,
        "tier": tier,
        "answers": encoded_answers,
        "labels": list(proposal.labels),
        "suggestions": {
            "priority": proposal.suggest_priority,
            "labels": [proposal.suggest_priority] if proposal.suggest_priority else [],
        },
        "needs_info": {
            "text": "Please provide the missing information requested by the project charter."
        }
        if proposal.needs_info
        else None,
        "rca": None,
        "close": proposal.close,
        "escalate": escalate,
        "security": proposal.security,
        "notes": [_decision_note(note) for note in notes[:20]],
    }


def model_context(gh: GitHub, event: Event, is_writer: Callable[[str], bool]) -> str:
    lines = [f"Title: {event.title}", "", "Issue body:", event.body]
    for comment in gh.paginate(f"issues/{event.number}/comments"):
        if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
            continue
        user = comment["user"]
        login = user.get("login")
        if (
            not isinstance(login, str)
            or user.get("type") == "Bot"
            or login.casefold().endswith("[bot]")
        ):
            continue
        if login.casefold() != event.author.casefold() and not is_writer(login):
            continue
        body = comment.get("body")
        if isinstance(body, str):
            lines.extend(("", f"Comment by {login}:", body))
    return "\n".join(lines).strip()


def tier2_counter_artifact_names(author: str, now: datetime | None = None) -> tuple[str, str]:
    day = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d")
    return f"tier2-{day}", f"tier2-{day}-{author.casefold()}"


def tier2_cap_status(
    gh: GitHub, config: RepoConfig, event: Event, now: datetime
) -> Literal["available", "capped", "error"]:
    """Distinguish a real daily cap from a failed preflight check."""
    try:
        counter_gh = _Budget(gh)
        repository_id = _mapping(counter_gh.request("GET", f"/repos/{event.repo}"))["id"]
        global_name, author_name = tier2_counter_artifact_names(event.author, now)
        limits = config.raw["limits"]
        for name, limit in (
            (global_name, limits["tier2_per_day"]),
            (author_name, limits["tier2_per_author_per_day"]),
        ):
            count = 0
            for artifact in _items(counter_gh, "actions/artifacts", "artifacts", {"name": name}):
                if artifact["name"] != name:
                    continue
                run = _mapping(artifact["workflow_run"])
                if (
                    run["repository_id"] == repository_id
                    and run["head_repository_id"] == repository_id
                ):
                    count += 1
                    if count >= limit:
                        return "capped"
    except Exception:
        return "error"
    return "available"
