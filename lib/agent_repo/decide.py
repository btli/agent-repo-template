from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_repo.charter import Charter
from agent_repo.config import RepoConfig
from agent_repo.event import Event
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
    if question in {"needs_info", "out_of_scope", "security_sensitive"}:
        if value == "yes":
            value = True
        elif value == "no":
            value = False
    return value, answer.p


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
    if security:
        labels.append("needs-maintainer")
        reasons.append("security")
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
    if config.raw["close"]["enabled"] and duplicate and duplicate_p >= thresholds["close_min"]:
        if duplicate.similarity >= thresholds["close_lexical_min"]:
            close = {
                "kind": "duplicate",
                "target": duplicate.number,
                "docs_ref": None,
                "p": duplicate_p,
                "lexical": duplicate.similarity,
                "tier2_agrees": False,
            }
    out_of_scope, out_of_scope_p = _value(answers, "out_of_scope")
    docs_ref, docs_ref_p = _value(answers, "docs_ref")
    if (
        close is None
        and config.raw["close"]["enabled"]
        and out_of_scope is True
        and out_of_scope_p >= thresholds["close_min"]
        and isinstance(docs_ref, str)
        and docs_ref != "none"
        and docs_ref_p >= thresholds["close_min"]
    ):
        close = {
            "kind": "out_of_scope",
            "target": None,
            "docs_ref": docs_ref,
            "p": min(out_of_scope_p, docs_ref_p),
            "lexical": None,
            "tier2_agrees": False,
        }

    unique_reasons = list(dict.fromkeys(reasons))
    needs_tier2 = bool(unique_reasons)
    if needs_tier2 and not tier2_allowed and not security:
        unique_reasons.append("tier2-cap")
        needs_tier2 = False
        if low_action_answer or suggest_priority:
            labels.append("needs-maintainer")
    if security:
        needs_tier2 = False

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
            value, _ = _value(answers, question)
            encoded_answers[question] = {"value": value, "p": answer.p}
    notes = []
    if proposal.escalate_reasons:
        notes.append("Escalation reasons: " + ", ".join(proposal.escalate_reasons))
    duplicate = encoded_answers.get("duplicate_of", {}).get("value")
    if isinstance(duplicate, str) and duplicate != "none" and proposal.close is None:
        notes.append(f"Suggested duplicate: {duplicate}. Automatic closing was not proposed.")
    if (
        encoded_answers.get("out_of_scope", {}).get("value") is True
        and isinstance(encoded_answers.get("docs_ref", {}).get("value"), str)
        and encoded_answers["docs_ref"]["value"] != "none"
        and proposal.close is None
    ):
        notes.append(
            "Suggested out-of-scope close with the selected documentation reference; "
            "automatic closing was not proposed."
        )
    if proposal.rca_candidate:
        notes.append("This sufficiently specified bug is a candidate for root cause analysis.")
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
        "escalate": {
            "to": "maintainer",
            "reason": ", ".join(proposal.escalate_reasons)[:500] or "Decision requires review.",
        }
        if proposal.escalate or "tier2-cap" in proposal.escalate_reasons or proposal.security
        else None,
        "security": proposal.security,
        "notes": notes[:20],
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
