import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_repo.charter import parse_charter
from agent_repo.config import load_config
from agent_repo.decide import (
    build_questions,
    route,
    tier2_cap_available,
    tier2_counter_artifact_names,
    to_decision,
)
from agent_repo.event import load_event
from agent_repo.gate import GateContext, evaluate
from agent_repo.github import GitHubError
from agent_repo.schema import load_schema, validate
from agent_repo.systemone import Answer, Question, SystemOneError
from agent_repo.tier0 import Candidate
from agent_repo.yamljson import load_yaml

FIXTURES = Path("tests/fixtures")
EVENT = load_event("issues", FIXTURES / "events/issues.opened.json")
CHARTER = parse_charter((FIXTURES / "charter/example.md").read_text())
CONFIG = load_config(FIXTURES / "agents")
GATE_LIMITS = {
    "triage_runs_per_issue": 3,
    "edit_debounce_minutes": 10,
    "tier2_per_day": 25,
    "tier2_per_author_per_day": 2,
}


def answers(**values):
    complete = {
        "type": ("feature", 0.9),
        "area": ("core", 0.9),
        "priority": ("P3", 0.9),
        "needs_info": (False, 0.9),
        "out_of_scope": (False, 0.9),
        "docs_ref": ("none", 0.9),
        "security_sensitive": (False, 0.9),
    }
    complete.update(values)
    return {key: Answer(key, value, probability) for key, (value, probability) in complete.items()}


def prepare_command_checkout(tmp_path, body, *, llm_only=False):
    checkout = tmp_path / "checkout"
    agents = checkout / ".agents"
    agents.mkdir(parents=True)
    for name in ("areas.yml", "labels.yml"):
        (agents / name).write_text((FIXTURES / "agents" / name).read_text())
    config_text = (FIXTURES / "agents/config.yml").read_text()
    if llm_only:
        config_text = config_text.replace(
            "system_one:\n  provider: databricks-openjev\n  model: system.ai.openjev-qwen35-4b\n",
            "",
        )
    (agents / "config.yml").write_text(config_text)
    (agents / "charter.md").write_text((FIXTURES / "charter/example.md").read_text())
    template_dir = checkout / ".github/ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True)
    (template_dir / "bug.yml").write_text(
        """body:
  - type: input
    attributes:
      label: Version
    validations:
      required: true
  - type: textarea
    attributes:
      label: Steps to reproduce
    validations:
      required: true
"""
    )
    event_path = tmp_path / "event.json"
    event_payload = json.loads((FIXTURES / "events/issues.opened.json").read_text())
    event_payload["issue"]["body"] = body
    event_path.write_text(json.dumps(event_payload))
    return checkout, event_path, tmp_path / "decision.json", tmp_path / "issue.txt"


def set_command_environment(monkeypatch, tmp_path, event_path, *, tier2_allowed="true"):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", EVENT.repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "run-55")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("TIER2_ALLOWED", tier2_allowed)
    monkeypatch.setenv("SYSTEM_ONE_BASE_URL", "https://systemone.example")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("GATE_CANARY", "canary-secret")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))


def test_build_questions_uses_spec_order_and_eight_example_ids():
    questions = build_questions(
        CHARTER,
        CONFIG,
        [Candidate(7, "Similar", 0.5)],
    )

    assert [question.id for question in questions] == [
        "type",
        "area",
        "priority",
        "needs_info",
        "out_of_scope",
        "docs_ref",
        "duplicate_of",
        "security_sensitive",
    ]
    assert "unknown" in questions[1].options
    assert "none" in questions[5].options
    assert questions[2].options == CHARTER.priorities


def test_build_questions_omits_duplicate_when_there_are_no_candidates():
    questions = build_questions(CHARTER, CONFIG, [])

    assert "duplicate_of" not in [question.id for question in questions]


@pytest.mark.parametrize(
    ("answers_value", "expected_label", "expected_escalation"),
    [
        ({"type": ("feature", 0.9)}, "type:feature", False),
        ({"type": ("bug", 0.9)}, "type:bug", True),
        ({"area": ("core", 0.9)}, "area:core", False),
        ({"priority": ("P2", 0.9)}, "P2", False),
        ({"priority": ("P3", 0.9)}, "P3", False),
        ({"priority": ("P0", 0.9)}, None, True),
        ({"priority": ("P1", 0.9)}, None, True),
        ({"needs_info": (True, 0.9)}, None, False),
        ({"type": ("bug", 0.5)}, None, True),
        ({"docs_ref": ("none", 0.5)}, None, False),
        ({"duplicate_of": ("none", 0.5)}, None, False),
    ],
)
def test_route_follows_each_spec_43_rule(answers_value, expected_label, expected_escalation):
    proposal = route(answers(**answers_value), CONFIG, EVENT, True, [])

    assert (expected_label is None or expected_label in proposal.labels) is True
    assert proposal.escalate is expected_escalation


def test_route_priority_p0_is_suggested_but_never_labeled():
    proposal = route(answers(priority=("P0", 0.9)), CONFIG, EVENT, True, [])

    assert proposal.suggest_priority == "P0"
    assert not any(label.startswith("P") for label in proposal.labels)
    assert proposal.escalate


def test_route_low_area_escalates_with_area_reason():
    proposal = route(answers(area=("core", 0.5)), CONFIG, EVENT, True, [])

    assert proposal.escalate
    assert "area" in proposal.escalate_reasons


def test_route_bug_with_enough_information_is_rca_candidate():
    proposal = route(answers(type=("bug", 0.9), needs_info=(False, 0.9)), CONFIG, EVENT, True, [])

    assert proposal.rca_candidate
    assert proposal.escalate
    assert (
        to_decision(
            proposal, answers(type=("bug", 0.9), needs_info=(False, 0.9)), "run", "a" * 40, 1
        )["escalate"]
        is None
    )


def test_route_known_rca_author_rejects_unassociated_author():
    config = replace(CONFIG, raw={**CONFIG.raw, "triage": {"rca_authors": "known"}})
    event = replace(EVENT, author_association="NONE")
    proposal = route(answers(type=("bug", 0.9), needs_info=(False, 0.9)), config, event, True, [])

    assert not proposal.rca_candidate


def test_route_tier2_cap_records_reason_and_maintainer_when_answer_is_low():
    proposal = route(answers(area=("core", 0.5)), CONFIG, EVENT, False, [])
    decision = to_decision(proposal, answers(area=("core", 0.5)), "run", "a" * 40, 1)

    assert not proposal.escalate
    assert "tier2-cap" in proposal.escalate_reasons
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"] is not None


def test_rca_routed_to_tier2_does_not_escalate_when_tier2_is_capped():
    model_answers = answers(type=("bug", 0.9), needs_info=(False, 0.9))

    proposal = route(model_answers, CONFIG, EVENT, False, [])
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert not proposal.escalate
    assert "tier2-cap" in proposal.escalate_reasons
    assert "needs-maintainer" not in proposal.labels
    assert decision["escalate"] is None


def test_rca_routed_to_tier2_escalates_when_cap_check_fails():
    model_answers = answers(type=("bug", 0.95), needs_info=(False, 0.95))

    proposal = route(model_answers, CONFIG, EVENT, False, [], tier2_check_failed=True)
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert not proposal.escalate
    assert "tier2-check-failed" in proposal.escalate_reasons
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"]["to"] == "maintainer"
    assert "tier2-check-failed" in decision["escalate"]["reason"]


def test_route_tier2_cap_escalates_p0_priority():
    proposal = route(answers(priority=("P0", 0.9)), CONFIG, EVENT, False, [])
    decision = to_decision(proposal, answers(priority=("P0", 0.9)), "run", "a" * 40, 1)

    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"]["reason"] == "priority, tier2-cap"


def test_route_adds_needs_info_label_for_confident_needs_info_answer():
    proposal = route(answers(needs_info=(True, 0.9)), CONFIG, EVENT, True, [])

    assert "needs-info" in proposal.labels


def test_tier1_security_answer_is_terminal_without_tier2_routing():
    model_answers = answers(security_sensitive=(True, 0.95))

    proposal = route(model_answers, CONFIG, EVENT, True, [])
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert proposal.security
    assert proposal.escalate is False
    assert decision["security"] is True
    assert decision["escalate"] == {"to": "maintainer", "reason": "security"}


def test_route_missing_answers_escalate_fail_closed():
    proposal = route(
        {
            key: None
            for key in (
                "type",
                "area",
                "priority",
                "needs_info",
                "out_of_scope",
                "docs_ref",
                "duplicate_of",
                "security_sensitive",
            )
        },
        CONFIG,
        EVENT,
        True,
        [],
    )

    assert proposal.escalate


def test_system_one_failure_with_tier2_unavailable_escalates_to_maintainer():
    missing_answers = {
        key: None
        for key in (
            "type",
            "area",
            "priority",
            "needs_info",
            "out_of_scope",
            "docs_ref",
            "security_sensitive",
        )
    }

    proposal = route(missing_answers, CONFIG, EVENT, False, [])
    decision = to_decision(proposal, missing_answers, "run", "a" * 40, 1)

    assert not proposal.escalate
    assert "tier2-cap" in proposal.escalate_reasons
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"] is not None


def test_route_normalizes_system_one_yes_no_answers_to_booleans():
    model_answers = {
        "type": Answer("type", "bug", 0.9),
        "area": Answer("area", "core", 0.9),
        "priority": Answer("priority", "P3", 0.9),
        "needs_info": Answer("needs_info", "yes", 0.9),
        "out_of_scope": Answer("out_of_scope", "no", 0.9),
        "docs_ref": Answer("docs_ref", "none", 0.9),
        "duplicate_of": Answer("duplicate_of", "none", 0.9),
        "security_sensitive": Answer("security_sensitive", "no", 0.9),
    }
    proposal = route(model_answers, CONFIG, EVENT, True, [])
    artifact = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert proposal.needs_info
    assert proposal.rca_candidate is False
    assert artifact["answers"]["needs_info"]["value"] is True
    assert artifact["answers"]["out_of_scope"]["value"] is False


@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
def test_route_close_disabled_keeps_close_empty_and_records_suggestion(kind):
    candidate = Candidate(30, "Validation fails for empty input", 0.5)
    docs_ref = CHARTER.docs_index[0].url
    model_answers = answers(
        duplicate_of=("#30" if kind == "duplicate" else "none", 0.95),
        out_of_scope=(kind == "out_of_scope", 0.95),
        docs_ref=(docs_ref if kind == "out_of_scope" else "none", 0.95),
    )
    proposal = route(model_answers, CONFIG, EVENT, True, [candidate])

    assert proposal.close is None
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)
    suggestion = "out-of-scope" if kind == "out_of_scope" else "duplicate"
    assert any(suggestion in note.casefold() for note in decision["notes"])


def test_route_duplicate_below_close_min_hands_off_without_close():
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    candidate = Candidate(30, "Validation fails for empty input", 0.5)
    model_answers = answers(duplicate_of=("#30", 0.90))

    proposal = route(model_answers, config, EVENT, True, [candidate])
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert proposal.close == {
        "kind": "duplicate",
        "target": 30,
        "docs_ref": None,
        "p": 0.90,
        "lexical": 0.5,
        "tier2_agrees": False,
    }
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"] == {"to": "maintainer", "reason": "close-suggestion"}
    assert not any("suggested duplicate" in note.casefold() for note in decision["notes"])


def test_route_duplicate_p91_below_close_min_emits_close_suggestion():
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    candidate = Candidate(30, "Validation fails for empty input", 0.5)

    proposal = route(answers(duplicate_of=("#30", 0.91)), config, EVENT, True, [candidate])

    assert proposal.close is not None
    assert proposal.close["p"] == 0.91
    assert proposal.close["tier2_agrees"] is False
    assert "needs-maintainer" in proposal.labels


def test_route_close_proposal_requires_tier2_agreement():
    config = replace(
        CONFIG,
        raw={
            **CONFIG.raw,
            "close": {"enabled": True},
            "thresholds": {**CONFIG.raw["thresholds"], "close_lexical_min": 0.45},
        },
    )
    candidate = Candidate(30, "Validation fails for empty input", 0.5)
    proposal = route(answers(duplicate_of=("#30", 0.95)), config, EVENT, True, [candidate])

    assert proposal.close is not None
    assert proposal.close["tier2_agrees"] is False


def test_route_rejects_close_when_lexical_score_is_below_threshold():
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    candidate = Candidate(30, "Unrelated", 0.4)
    model_answers = answers(duplicate_of=("#30", 0.95), area=("core", 0.5))

    proposal = route(model_answers, config, EVENT, True, [candidate])
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert proposal.close is not None
    assert proposal.close["p"] == 0.95
    assert proposal.close["lexical"] == 0.4
    assert proposal.close["tier2_agrees"] is False
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"] is None
    assert "close-suggestion" in decision["notes"][0]


def test_route_duplicate_none_never_targets_the_first_candidate():
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    candidate = Candidate(30, "Validation fails for empty input", 0.9)

    proposal = route(answers(duplicate_of=("none", 0.95)), config, EVENT, True, [candidate])

    assert proposal.close is None


def test_route_out_of_scope_below_close_min_hands_off_without_close():
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    docs_ref = CHARTER.docs_index[0].url
    model_answers = answers(out_of_scope=(True, 0.90), docs_ref=(docs_ref, 0.95))

    proposal = route(model_answers, config, EVENT, True, [])
    decision = to_decision(proposal, model_answers, "run", "a" * 40, 1)

    assert proposal.close == {
        "kind": "out_of_scope",
        "target": None,
        "docs_ref": docs_ref,
        "p": 0.90,
        "lexical": None,
        "tier2_agrees": False,
    }
    assert "needs-maintainer" in proposal.labels
    assert decision["escalate"]["reason"] == "close-suggestion"
    assert not any("suggested out-of-scope close" in note.casefold() for note in decision["notes"])


@pytest.mark.parametrize(("out_of_scope_p", "docs_ref_p"), [(0.91, 0.95), (0.95, 0.90)])
def test_route_out_of_scope_below_close_min_emits_close_suggestion(out_of_scope_p, docs_ref_p):
    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    docs_ref = CHARTER.docs_index[0].url

    proposal = route(
        answers(out_of_scope=(True, out_of_scope_p), docs_ref=(docs_ref, docs_ref_p)),
        config,
        EVENT,
        True,
        [],
    )

    assert proposal.close is not None
    assert proposal.close["p"] == min(out_of_scope_p, docs_ref_p)
    assert proposal.close["tier2_agrees"] is False
    assert "needs-maintainer" in proposal.labels


@pytest.mark.parametrize(
    ("kind", "probability", "lexical", "should_close"),
    [
        ("duplicate", 0.90, 0.5, False),
        ("duplicate", 0.95, 0.4, False),
        ("out_of_scope", 0.90, None, False),
        ("duplicate", 0.95, 0.5, True),
    ],
)
def test_decide_close_matches_apply_after_tier2_agreement(kind, probability, lexical, should_close):
    from agent_repo.config import Area
    from test_apply_triage import API, bodies, labels, plan
    from test_apply_triage import context as apply_context

    config = replace(CONFIG, raw={**CONFIG.raw, "close": {"enabled": True}})
    candidate = Candidate(99, "Validation fails for empty input", lexical or 0.5)
    docs_ref = CHARTER.docs_index[0].url
    answer_values = {
        "type": ("bug", 0.95),
        "area": ("core", 0.95),
        "priority": ("P2", 0.95),
        "needs_info": (False, 0.95),
        "out_of_scope": (kind == "out_of_scope", probability),
        "docs_ref": (docs_ref if kind == "out_of_scope" else "none", 0.95),
        "security_sensitive": (False, 0.95),
    }
    if kind == "duplicate":
        answer_values["duplicate_of"] = ("#99", probability)
    model_answers = answers(**answer_values)
    proposal = route(
        model_answers,
        config,
        EVENT,
        True,
        [candidate] if kind == "duplicate" else [],
    )
    artifact = to_decision(proposal, model_answers, "run-1", "a" * 40, 1)
    artifact["close"]["tier2_agrees"] = True

    api = API()
    ctx = apply_context(api, enabled=True)
    ctx = replace(
        ctx,
        config=replace(
            ctx.config,
            areas=(Area("core", "Core project behavior", ("src/**",), ("octocat",), False),),
            labels=tuple(dict.fromkeys((*ctx.config.labels, "area:core"))),
        ),
    )
    actions = plan(artifact, ctx)

    assert artifact["close"] is not None
    assert any(action.kind == "close" for action in actions) is should_close
    assert ("agent:closed" in labels(actions)) is should_close
    if should_close:
        assert "needs-maintainer" not in artifact["labels"]
    else:
        assert "needs-maintainer" in artifact["labels"]
        assert "needs-maintainer" in labels(actions)
        assert "Suggested closure requires maintainer confirmation." in bodies(actions)


@pytest.mark.parametrize(
    ("artifacts", "expected_available"),
    [
        ("matching", True),
        ("wrong-name", True),
        ("foreign-repository", True),
        ("foreign-head-repository", True),
        ("below-limit", True),
        ("at-limit", False),
    ],
)
def test_decide_tier2_cap_counting_matches_gate(artifacts, expected_available, monkeypatch):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"{EVENT.repo}/.github/workflows/agent-triage.yml@refs/heads/main",
    )
    now = datetime(2026, 10, 14, 12, tzinfo=UTC)
    event = replace(EVENT, author="CoNtRiBuToR")
    _, author_name = tier2_counter_artifact_names(event.author, now)
    valid = {"name": author_name, "workflow_run": {"repository_id": 7, "head_repository_id": 7}}
    foreign_repo = {
        "name": author_name,
        "workflow_run": {"repository_id": 8, "head_repository_id": 7},
    }
    foreign_head = {
        "name": author_name,
        "workflow_run": {"repository_id": 7, "head_repository_id": 8},
    }
    wrong_name = {
        "name": "unrelated-artifact",
        "workflow_run": {"repository_id": 7, "head_repository_id": 7},
    }
    listings = {
        "matching": [valid],
        "wrong-name": [wrong_name, wrong_name],
        "foreign-repository": [valid, foreign_repo],
        "foreign-head-repository": [valid, foreign_head],
        "below-limit": [valid],
        "at-limit": [valid, valid],
    }

    class FakeGitHub:
        def request(self, method, path, *, params=None):
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == f"issues/{event.number}/comments":
                return []
            if path == "actions/artifacts":
                return {"artifacts": listings[artifacts]}
            raise AssertionError(path)

        def paginate(self, path):
            return []

    limits = {
        "triage_runs_per_issue": 3,
        "edit_debounce_minutes": 10,
        "tier2_per_day": 25,
        "tier2_per_author_per_day": 2,
    }
    config = replace(CONFIG, raw={"limits": limits})
    gh = FakeGitHub()
    gate = GateContext(
        workflow="triage",
        enabled=True,
        bot_login="helper[bot]",
        config=config,
        gh=gh,
        now=now,
        maintainers=frozenset(),
        run_id="99",
    )

    gate_available = evaluate(event, gate).tier2_allowed
    decide_available = tier2_cap_available(gh, config, event, now)

    assert gate_available is expected_available
    assert decide_available is expected_available


def test_to_decision_emits_schema_valid_artifact_with_input_identity():
    proposal = route({}, CONFIG, EVENT, True, [])
    artifact = to_decision(proposal, {}, "run-55", "a" * 40, 1)

    assert artifact["run_id"] == "run-55"
    assert artifact["base_sha"] == "a" * 40
    assert validate(artifact, load_schema("decision")) == []


@pytest.mark.parametrize(
    ("stage", "count", "reason"),
    [("global", 25, "tier2-global-cap"), ("author", 2, "tier2-author-cap")],
)
def test_decide_counter_names_trip_gate_caps_for_mixed_case_author(
    stage, count, reason, monkeypatch
):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"{EVENT.repo}/.github/workflows/agent-triage.yml@refs/heads/main",
    )
    names = tier2_counter_artifact_names("CoNtRiBuToR", datetime(2026, 10, 14, tzinfo=UTC))
    selected_name = names[0 if stage == "global" else 1]
    queried_names = []
    artifacts = [
        {"name": selected_name, "workflow_run": {"repository_id": 7, "head_repository_id": 7}}
        for _ in range(count)
    ]

    class FakeGitHub:
        def paginate(self, path):
            assert path == f"issues/{EVENT.number}/comments"
            return []

        def request(self, method, path, *, params=None):
            if path == f"issues/{EVENT.number}/comments":
                return []
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                name = params["name"]
                queried_names.append(name)
                return {"artifacts": [item for item in artifacts if item["name"] == name]}
            raise AssertionError(path)

    gate_config = replace(
        CONFIG,
        raw={
            "limits": {
                "triage_runs_per_issue": 3,
                "edit_debounce_minutes": 10,
                "tier2_per_day": 25,
                "tier2_per_author_per_day": 2,
            }
        },
    )
    event = replace(EVENT, author="CoNtRiBuToR")
    gate = GateContext(
        workflow="triage",
        enabled=True,
        bot_login="helper[bot]",
        config=gate_config,
        gh=FakeGitHub(),
        now=datetime(2026, 10, 14, 12, tzinfo=UTC),
        maintainers=frozenset(),
        run_id="99",
    )

    result = evaluate(event, gate)

    assert not result.tier2_allowed
    assert result.reason == reason
    tier2_queries = [name for name in queried_names if name.startswith("tier2-")]
    assert tier2_queries == list(names[: 1 if stage == "global" else 2])


@pytest.mark.parametrize(
    "failure", ["system-one-error", "malformed-answer", "out-of-range-probability", "off-option"]
)
def test_system_one_error_or_malformed_answer_becomes_none(monkeypatch, failure):
    from agent_repo.commands import decide as command

    question = Question("type", "choice", "Choose a type", {"bug": "Bug"})
    monkeypatch.setenv("SYSTEM_ONE_BASE_URL", "https://systemone.example")
    monkeypatch.setenv("LLM_API_KEY", "credential")
    if failure == "system-one-error":

        def broken_ask(*args, **kwargs):
            raise SystemOneError("provider request failed")

        monkeypatch.setattr(command, "ask", broken_ask)
    elif failure == "malformed-answer":
        monkeypatch.setattr(
            command,
            "ask",
            lambda *args, **kwargs: {"type": Answer("wrong-id", "bug", 0.9)},
        )
    elif failure == "out-of-range-probability":
        monkeypatch.setattr(
            command, "ask", lambda *args, **kwargs: {"type": Answer("type", "bug", 1.1)}
        )
    else:
        monkeypatch.setattr(
            command, "ask", lambda *args, **kwargs: {"type": Answer("type", "feature", 0.9)}
        )

    answers_from_model = command._model_answers(
        CONFIG,
        "Charter text",
        "Filtered issue text",
        [question],
        "canary",
    )

    assert answers_from_model == {"type": None}
    assert route(answers_from_model, CONFIG, EVENT, True, []).escalate


def test_decide_action_uploads_both_gate_counter_artifacts():
    action = load_yaml(Path("actions/decide/action.yml"))
    steps = action["runs"]["steps"]
    uploads = [
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    ]

    assert len(uploads) == 2
    assert all(step["if"] == "steps.decide.outputs.tier2 == 'true'" for step in uploads)
    assert {step["with"]["name"] for step in uploads} == {
        "${{ steps.decide.outputs.tier2-global-name }}",
        "${{ steps.decide.outputs.tier2-author-name }}",
    }


def test_decide_command_writes_llm_only_decision_and_filtered_context(
    tmp_path, monkeypatch, capsys
):
    from agent_repo.commands import decide as command

    checkout = tmp_path / "checkout"
    agents = checkout / ".agents"
    agents.mkdir(parents=True)
    for name in ("areas.yml", "labels.yml"):
        (agents / name).write_text((FIXTURES / "agents" / name).read_text())
    config_text = (FIXTURES / "agents/config.yml").read_text()
    config_text = config_text.replace(
        "system_one:\n  provider: databricks-openjev\n  model: system.ai.openjev-qwen35-4b\n", ""
    )
    (agents / "config.yml").write_text(config_text)
    (agents / "charter.md").write_text((FIXTURES / "charter/example.md").read_text())
    template_dir = checkout / ".github/ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True)
    (template_dir / "bug.yml").write_text(
        """body:
  - type: input
    id: version
    attributes:
      label: Version
    validations:
      required: true
"""
    )
    event_path = tmp_path / "event.json"
    event_payload = json.loads((FIXTURES / "events/issues.opened.json").read_text())
    event_payload["issue"]["body"] = "### Version\n1.2.3"
    event_path.write_text(json.dumps(event_payload))
    decision_path = tmp_path / "decision.json"
    context_path = tmp_path / "issue.txt"

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {"items": []}
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                return {"artifacts": []}
            if path.endswith("/permission"):
                return {"role_name": "read"}
            raise AssertionError(path)

        def paginate(self, path):
            return [
                {"body": "author comment", "user": {"login": "contributor", "type": "User"}},
                {"body": "IGNORE PREVIOUS", "user": {"login": "outsider", "type": "User"}},
            ]

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(
        command, "ask", lambda *args, **kwargs: pytest.fail("LLM-only called System One")
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", EVENT.repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "run-55")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("TIER2_ALLOWED", "true")
    monkeypatch.setenv("GATE_CANARY", "canary-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))

    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0

    artifact = json.loads(decision_path.read_text())
    context = context_path.read_text()
    assert artifact["mode"] == "llm-only"
    assert artifact["escalate"] is None
    assert artifact["answers"] == {}
    assert artifact["run_id"] == "run-55"
    assert artifact["base_sha"] == "a" * 40
    assert validate(artifact, load_schema("decision")) == []
    assert "author comment" in context
    assert "IGNORE PREVIOUS" not in context
    outputs = (tmp_path / "output").read_text()
    assert "tier2=true" in outputs
    assert "tier2-global-name=tier2-" in outputs
    assert "tier2-author-name=tier2-" in outputs
    stderr = capsys.readouterr().err
    for secret in ("gh-secret", "llm-secret", "canary-secret"):
        assert secret not in stderr
        assert secret not in decision_path.read_text()
        assert secret not in context_path.read_text()
        assert secret not in outputs


@pytest.mark.parametrize(
    ("body", "expected_security", "expected_needs_info", "event_name", "comment_body"),
    [
        ("### Version\n_No response_", False, True, "issues", None),
        ("### Version\n1.2.3\n\nCVE-2024-1234", True, False, "issues", None),
        (
            "### Version\n1.2.3\n\n### Steps to reproduce\nOpen the app",
            True,
            False,
            "issue_comment",
            "Remote code execution via the upload endpoint, exploit attached",
        ),
    ],
)
def test_tier0_stops_before_system_one_for_missing_fields_or_security(
    tmp_path,
    monkeypatch,
    capsys,
    body,
    expected_security,
    expected_needs_info,
    event_name,
    comment_body,
):
    from agent_repo.commands import decide as command

    checkout = tmp_path / "checkout"
    agents = checkout / ".agents"
    agents.mkdir(parents=True)
    for name in ("areas.yml", "labels.yml", "config.yml"):
        (agents / name).write_text((FIXTURES / "agents" / name).read_text())
    (agents / "charter.md").write_text((FIXTURES / "charter/example.md").read_text())
    template_dir = checkout / ".github/ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True)
    (template_dir / "bug.yml").write_text(
        """body:
  - type: input
    attributes:
      label: Version
    validations:
      required: true
"""
    )
    event_path = tmp_path / "event.json"
    fixture = (
        "issue_comment.created.issue.json"
        if event_name == "issue_comment"
        else "issues.opened.json"
    )
    event_payload = json.loads((FIXTURES / "events" / fixture).read_text())
    event_payload["issue"]["body"] = body
    if comment_body:
        event_payload["comment"]["body"] = comment_body
    event_path.write_text(json.dumps(event_payload))

    class FakeGitHub:
        def __init__(self, token, repo):
            pass

        def request(self, *args, **kwargs):
            pytest.fail("Tier 0 should stop before GitHub searches")

        def paginate(self, path):
            if comment_body:
                return [
                    {
                        "body": comment_body,
                        "user": {"login": "contributor", "type": "User"},
                    }
                ]
            return []

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    ask_calls = []
    monkeypatch.setattr(command, "ask", lambda *args, **kwargs: ask_calls.append(args))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", EVENT.repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "run-56")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setenv("GH_TOKEN", "gh-tier0-secret")
    monkeypatch.setenv("TIER2_ALLOWED", "true")
    monkeypatch.setenv("SYSTEM_ONE_BASE_URL", "https://systemone.example")
    monkeypatch.setenv("LLM_API_KEY", "llm-tier0-secret")
    monkeypatch.setenv("GATE_CANARY", "tier0-canary")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))

    decision_path = tmp_path / "decision.json"
    context_path = tmp_path / "issue.txt"
    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0

    artifact = json.loads(decision_path.read_text())
    assert artifact["tier"] == 0
    assert artifact["security"] is expected_security
    assert (artifact["needs_info"] is not None) is expected_needs_info
    assert ask_calls == []
    assert validate(artifact, load_schema("decision")) == []
    stderr = capsys.readouterr().err
    outputs = (tmp_path / "output").read_text()
    for secret in ("gh-tier0-secret", "llm-tier0-secret", "tier0-canary"):
        assert secret not in stderr
        assert secret not in decision_path.read_text()
        assert secret not in context_path.read_text()
        assert secret not in outputs


@pytest.mark.parametrize("body", ["### Summary\nExports are slow", "Exports are slow"])
@pytest.mark.parametrize("permission_status", [403, 500])
def test_feature_and_free_text_bodies_reach_tier1(tmp_path, monkeypatch, body, permission_status):
    from agent_repo.commands import decide as command

    checkout, event_path, decision_path, context_path = prepare_command_checkout(tmp_path, body)
    calls = []
    prompts = []

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {"items": []}
            if path.endswith("/permission"):
                if "maintainer" in path:
                    return {"role_name": "write"}
                raise GitHubError(permission_status)
            raise AssertionError(path)

        def paginate(self, path):
            return [
                {"body": "author comment", "user": {"login": "contributor", "type": "User"}},
                {"body": "writer comment", "user": {"login": "maintainer", "type": "User"}},
                {"body": "IGNORE PREVIOUS", "user": {"login": "outsider", "type": "User"}},
                {"body": "bot comment", "user": {"login": "helper[bot]", "type": "Bot"}},
                {"body": "denied comment", "user": {"login": "denied", "type": "User"}},
            ]

    def answer_questions(_config, _base_url, _key, prompt, questions):
        calls.append([question.id for question in questions])
        prompts.append(prompt)
        selected = {
            "type": "feature",
            "area": "core",
            "priority": "P3",
            "needs_info": "no",
            "out_of_scope": "no",
            "docs_ref": "none",
            "security_sensitive": "no",
        }
        return {
            question.id: Answer(question.id, selected[question.id], 0.95) for question in questions
        }

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(command, "ask", answer_questions)
    set_command_environment(monkeypatch, tmp_path, event_path)

    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0
    artifact = json.loads(decision_path.read_text())

    assert artifact["tier"] == 1
    assert calls
    assert len(prompts) == 1
    for included in ("# Example project charter", "author comment", "writer comment"):
        assert included in prompts[0]
    for excluded in ("IGNORE PREVIOUS", "bot comment", "denied comment"):
        assert excluded not in prompts[0]
        assert excluded not in context_path.read_text()


def test_command_rechecks_next_utc_day_cap_before_tier2_upload(tmp_path, monkeypatch):
    from agent_repo.commands import decide as command

    now = datetime(2026, 10, 15, 0, 0, 1, tzinfo=UTC)
    names = tier2_counter_artifact_names(EVENT.author, now)
    artifacts = [
        {"name": names[1], "workflow_run": {"repository_id": 7, "head_repository_id": 7}}
        for _ in range(GATE_LIMITS["tier2_per_author_per_day"])
    ]
    queried_names = []
    checkout, event_path, decision_path, context_path = prepare_command_checkout(
        tmp_path, "### Version\n1.2.3\n\n### Steps to reproduce\nOpen the app", llm_only=True
    )

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return now

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {"items": []}
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                name = params["name"]
                queried_names.append(name)
                return {"artifacts": [item for item in artifacts if item["name"] == name]}
            raise AssertionError(path)

        def paginate(self, path):
            return []

    monkeypatch.setattr(command, "datetime", FixedDateTime)
    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(
        command, "ask", lambda *args, **kwargs: pytest.fail("LLM-only called System One")
    )
    set_command_environment(monkeypatch, tmp_path, event_path)

    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0
    outputs = (tmp_path / "output").read_text()
    artifact = json.loads(decision_path.read_text())

    assert "tier2=false" in outputs
    assert "tier2-counter-file=" not in outputs
    assert "tier2-global-name=" not in outputs
    assert "tier2-author-name=" not in outputs
    assert queried_names == list(names)
    assert "needs-maintainer" in artifact["labels"]
    assert artifact["escalate"]["reason"].endswith("tier2-cap")


def test_command_cap_check_error_for_confident_bug_hands_off_in_apply_plan(tmp_path, monkeypatch):
    from agent_repo.commands import decide as command
    from agent_repo.config import Area
    from test_apply_triage import API, labels, plan
    from test_apply_triage import context as apply_context

    checkout, event_path, decision_path, context_path = prepare_command_checkout(
        tmp_path,
        "### Version\n1.2.3\n\n### Steps to reproduce\nOpen the app",
    )

    def confident_bug_answers(_config, _base_url, _key, _prompt, questions):
        values = {
            "type": "bug",
            "area": "core",
            "priority": "P3",
            "needs_info": "no",
            "out_of_scope": "no",
            "docs_ref": "none",
            "duplicate_of": "none",
            "security_sensitive": "no",
        }
        return {
            question.id: Answer(question.id, values[question.id], 0.95) for question in questions
        }

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {"items": []}
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                raise GitHubError(403)
            raise AssertionError(path)

        def paginate(self, path):
            return []

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(command, "ask", confident_bug_answers)
    set_command_environment(monkeypatch, tmp_path, event_path)

    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0
    artifact = json.loads(decision_path.read_text())
    output = (tmp_path / "output").read_text()

    assert artifact["escalate"]["reason"].endswith("tier2-check-failed")
    assert "tier2-cap" not in artifact["escalate"]["reason"]
    assert "needs-maintainer" in artifact["labels"]
    assert "tier2=false" in output
    assert "tier2-counter-file=" not in output

    api = API()
    ctx = apply_context(api, enabled=True)
    ctx = replace(
        ctx,
        config=replace(
            ctx.config,
            areas=(Area("core", "Core project behavior", ("src/**",), ("octocat",), False),),
            labels=tuple(dict.fromkeys((*ctx.config.labels, "area:core"))),
        ),
    )
    assert "needs-maintainer" in labels(plan(artifact, ctx))


def _run_close_command(
    tmp_path, monkeypatch, kind, *, capped=False, area_probability=0.95, close_probability=0.95
):
    from agent_repo.commands import decide as command

    body = "### Version\n1.2.3\n\n### Steps to reproduce\nOpen the app"
    checkout, event_path, decision_path, context_path = prepare_command_checkout(tmp_path, body)
    config_path = checkout / ".agents/config.yml"
    config_path.write_text(config_path.read_text().replace("enabled: false", "enabled: true", 1))
    answer_values = {
        "type": "feature",
        "area": "core",
        "priority": "P3",
        "needs_info": "no",
        "out_of_scope": "yes" if kind == "out_of_scope" else "no",
        "docs_ref": CHARTER.docs_index[0].url if kind == "out_of_scope" else "none",
        "duplicate_of": "#99" if kind == "duplicate" else "none",
        "security_sensitive": "no",
    }

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {
                    "items": [
                        {"number": 99, "title": EVENT.title, "body": body},
                    ]
                }
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                if not capped:
                    return {"artifacts": []}
                return {
                    "artifacts": [
                        {
                            "name": params["name"],
                            "workflow_run": {
                                "repository_id": 7,
                                "head_repository_id": 7,
                            },
                        }
                        for _ in range(GATE_LIMITS["tier2_per_day"])
                    ]
                }
            raise AssertionError(path)

        def paginate(self, path):
            return []

    def answer_questions(_config, _base_url, _key, _prompt, questions):
        return {
            question.id: Answer(
                question.id,
                answer_values[question.id],
                area_probability
                if question.id == "area"
                else close_probability
                if question.id in {"duplicate_of", "out_of_scope"}
                else 0.95,
            )
            for question in questions
        }

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(command, "ask", answer_questions)
    set_command_environment(monkeypatch, tmp_path, event_path)
    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0
    return json.loads(decision_path.read_text()), (tmp_path / "output").read_text()


@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
def test_command_routes_close_vote_through_tier2_merge_and_apply(tmp_path, monkeypatch, kind):
    from agent_repo.commands.triage_merge import merge
    from agent_repo.config import Area
    from test_apply_triage import API, labels, plan
    from test_apply_triage import context as apply_context

    artifact, output = _run_close_command(tmp_path, monkeypatch, kind)

    assert artifact["close"]["kind"] == kind
    assert artifact["notes"] == ["Escalation reasons: close-vote"]
    assert artifact["escalate"] is None
    assert "tier2=true" in output

    for tier2_agrees, should_close in ((True, True), (False, False)):
        tier2 = json.loads(json.dumps(artifact))
        tier2["tier"] = 2
        tier2["close"]["tier2_agrees"] = tier2_agrees
        merged = merge(
            artifact,
            tier2,
            None,
            allowed_labels={"type:feature", "area:core"},
        )
        api = API()
        ctx = apply_context(api, enabled=True)
        ctx = replace(
            ctx,
            run_id=artifact["run_id"],
            base_sha=artifact["base_sha"],
            config=replace(
                ctx.config,
                areas=(Area("core", "Core project behavior", ("src/**",), ("octocat",), False),),
                labels=tuple(dict.fromkeys((*ctx.config.labels, "type:feature", "area:core"))),
            ),
        )
        actions = plan(merged, ctx)

        assert (merged["close"]["tier2_agrees"] is True) is should_close
        assert any(action.kind == "close" for action in actions) is should_close
        assert ("agent:closed" in labels(actions)) is should_close


@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
def test_command_caps_eligible_close_vote_and_hands_off(tmp_path, monkeypatch, kind):
    artifact, output = _run_close_command(tmp_path, monkeypatch, kind, capped=True)

    assert artifact["close"]["kind"] == kind
    assert artifact["close"]["tier2_agrees"] is False
    assert "needs-maintainer" in artifact["labels"]
    assert artifact["escalate"]["to"] == "maintainer"
    assert "tier2-cap" in artifact["escalate"]["reason"]
    assert "tier2=false" in output
    assert "tier2-counter-file=" not in output


def test_command_mixed_close_suggestion_routes_tier2_without_final_escalation(
    tmp_path, monkeypatch
):
    artifact, output = _run_close_command(
        tmp_path,
        monkeypatch,
        "duplicate",
        area_probability=0.5,
        close_probability=0.90,
    )

    assert artifact["close"]["tier2_agrees"] is False
    assert "needs-maintainer" in artifact["labels"]
    assert "area" in artifact["notes"][0]
    assert "close-suggestion" in artifact["notes"][0]
    assert artifact["escalate"] is None
    assert "tier2=true" in output


def test_system_one_error_cannot_leak_credentials_or_canary(tmp_path, monkeypatch, capsys):
    from agent_repo.commands import decide as command

    checkout, event_path, decision_path, context_path = prepare_command_checkout(
        tmp_path, "### Version\n1.2.3\n\n### Steps to reproduce\nOpen the app"
    )

    class FakeGitHub:
        def __init__(self, token, repo):
            assert token == "gh-secret"
            assert repo == EVENT.repo

        def request(self, method, path, *, params=None):
            if path == "/search/issues":
                return {"items": []}
            if path == f"/repos/{EVENT.repo}":
                return {"id": 7}
            if path == "actions/artifacts":
                return {"artifacts": []}
            raise AssertionError(path)

        def paginate(self, path):
            return []

    def broken_ask(*args, **kwargs):
        raise SystemOneError("provider detail included llm-secret and canary-secret")

    monkeypatch.setattr(command, "GitHub", FakeGitHub)
    monkeypatch.setattr(command, "ask", broken_ask)
    set_command_environment(monkeypatch, tmp_path, event_path)

    assert command.main([str(checkout), str(decision_path), str(context_path)]) == 0
    stderr = capsys.readouterr().err
    output = (tmp_path / "output").read_text()
    surfaces = [stderr, decision_path.read_text(), context_path.read_text(), output]

    for secret in ("gh-secret", "llm-secret", "canary-secret"):
        assert all(secret not in surface for surface in surfaces)
