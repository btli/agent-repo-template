import copy
import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from agent_repo.config import RepoConfig
from agent_repo.event import load_event
from agent_repo.github import GitHub, GitHubError
from agent_repo.yamljson import load_yaml

FIXTURES = Path("tests/fixtures/github/gate")
CASES = json.loads((FIXTURES / "cases.json").read_text())
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
TOKEN = "test-" + "credential"
LIMITS = {
    "triage_runs_per_issue": 3,
    "tier2_per_author_per_day": 2,
    "tier2_per_day": 25,
    "edit_debounce_minutes": 10,
}
EVENT_SHA = "c" * 40
AGENT_BASE = "d" * 40
EXPECTED_OVERRIDES = {
    "fix-agent-draft": {"run": False, "reason": "unlinked-issue", "tier2_allowed": False}
}
# The collaborator API pairs every role name with its effective base permission.
BASE_PERMISSIONS = {"admin": "admin", "maintain": "write", "write": "write"}


def _typed_user(user):
    # GitHub always reports the account type; only App accounts carry the [bot] suffix.
    if isinstance(user, dict) and "type" not in user:
        login = str(user.get("login", ""))
        return dict(user, type="Bot" if login.casefold().endswith("[bot]") else "User")
    return user


@pytest.fixture(autouse=True)
def triage_workflow_ref(monkeypatch):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "your-org/project/.github/workflows/agent-triage.yml@refs/heads/main",
    )


class FakeTransport:
    def __init__(self, case):
        self.case = case
        self.requests = []

    def __call__(self, request, timeout):
        assert request.method == "GET"
        parsed = urlparse(request.full_url)
        path = parsed.path.removeprefix("/repos/your-org/project/")
        query = parse_qs(parsed.query)
        self.requests.append((path, query))
        if self.case.get("failure") == "transport":
            raise RuntimeError(TOKEN)
        if self.case.get("failure") == "http":
            return 503, {}, b"{}"
        if self.case.get("fail_path") == path:
            return self.case.get("fail_status", 503), {}, b"{}"
        if path.endswith("/permission"):
            status = self.case.get("permission_status", 200)
            data = self.case.get("permission", {"role_name": "read", "permission": "read"})
            data = {"permission": BASE_PERMISSIONS.get(str(data.get("role_name")), "read"), **data}
        elif path.endswith("/comments"):
            status, data = 200, self.case.get("comments", [])
            start = (int(query.get("page", ["1"])[0]) - 1) * 100
            data = [dict(item, user=_typed_user(item.get("user"))) for item in data]
            data = data[start : start + 100]
        elif path.startswith("git/commits/"):
            message = f"Fix the parser\n\nAgent-Base: {AGENT_BASE}\nAgent-Run: 41\n"
            status, data = 200, {"sha": path.rsplit("/", 1)[1], "message": message}
            data = self.case.get("commit", data)
        elif path == "/repos/your-org/project":
            status, data = 200, {"id": 7, "full_name": "your-org/project"}
        elif path == "actions/artifacts":
            if query.get("page") == [str(self.case.get("fail_artifact_page", 0))]:
                return 503, {}, b"{}"
            name = query.get("name", [None])[0]
            artifacts = self.case.get("artifacts", [])
            data = {
                "artifacts": [
                    a
                    for a in artifacts
                    if (self.case.get("unfiltered_artifacts") or name is None or a["name"] == name)
                ]
            }
            status = 200
        elif path.startswith("actions/runs/"):
            run_id = path.rsplit("/", 1)[1]
            status, data = (
                200,
                {
                    "path": self.case.get("run_paths", {}).get(
                        run_id, ".github/workflows/agent-triage.yml"
                    )
                },
            )
        elif path.startswith("pulls/"):
            default = json.loads(
                Path("tests/fixtures/events/pull_request_target.opened.json").read_text()
            )
            status, data = 200, copy.deepcopy(self.case.get("pull", default["pull_request"]))
            data["user"] = _typed_user(data.get("user"))
        else:
            raise AssertionError("unexpected API endpoint")
        if self.case.get("failure") == "malformed":
            data = None
        headers = {}
        if isinstance(data, dict) and "artifacts" in data:
            items = data["artifacts"]
            data["total_count"] = len(items)
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * 100
            data = dict(data, artifacts=items[start : start + 100])
            if len(items) > start + 100:
                next_query = dict(query, page=[str(page + 1)])
                from urllib.parse import urlencode

                next_url = parsed._replace(query=urlencode(next_query, doseq=True)).geturl()
                headers = {"Link": f'<{next_url}>; rel="next"'}
        return status, headers, json.dumps(data).encode()


def evaluate_case(case, tmp_path):
    from agent_repo.gate import GateContext, evaluate

    path = tmp_path / "event.json"
    path.write_text(json.dumps(case["payload"]))
    event = load_event(case["event_name"], path)
    transport = FakeTransport(case)
    ctx = GateContext(
        workflow=case.get("workflow", "triage"),
        enabled=case.get("enabled", True),
        bot_login="helper[bot]",
        config=RepoConfig({"limits": case.get("limits", LIMITS)}, (), ()),
        gh=GitHub(TOKEN, event.repo, transport=transport),
        now=NOW,
        maintainers=frozenset(),
        run_id="99",
        event_sha=case.get("event_sha", EVENT_SHA),
    )
    return evaluate(event, ctx), event, ctx, transport


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_gate_event_rules(case, tmp_path):
    result, _, _, _ = evaluate_case(case, tmp_path)
    expected = {**case["expected"], **EXPECTED_OVERRIDES.get(case["id"], {})}
    observed = {key: getattr(result, key) for key in expected}
    assert observed == expected
    assert TOKEN not in result.reason


@pytest.mark.parametrize(
    "body,expected",
    [
        ("/fix handle the None case", ("fix", "handle the None case")),
        ("> /fix", None),
        ("/fixme", None),
        ("thanks\n/fix", None),
        ("/agent off", ("agent-off", "")),
        ("\n \n /review \nother text", ("review", "")),
        ("/triage", ("triage", "")),
        ("/fix", ("fix", "")),
        ("/fix\tfeedback", ("fix", "feedback")),
        ("/agent offset", None),
        ("/agent off trailing", None),
        ("```\n/fix\n```", None),
        ("", None),
        (" \n", None),
        ("/REVIEW", None),
        ("/fix args\nrun=true", ("fix", "args")),
    ],
)
def test_parse_command_first_nonblank_line(body, expected):
    from agent_repo.gate import Command, parse_command

    assert parse_command(body) == (Command(*expected) if expected else None)


@pytest.mark.parametrize(
    "role,permission,status,login,maintainers,expected",
    [
        ("write", "write", 200, "human", (), True),
        ("maintain", "write", 200, "human", (), True),
        ("admin", "admin", 200, "human", (), True),
        ("triage", "read", 200, "human", (), False),
        ("read", "read", 200, "human", (), False),
        ("none", "none", 200, "outsider", (), False),
        (None, "admin", 200, "human", (), True),
        ("custom", "admin", 200, "human", (), True),
        ("release-writer", "write", 200, "human", (), True),
        ("write", "read", 200, "human", (), False),
        ("custom-triager", "read", 200, "human", (), False),
        ("admin", "admin", 404, "human", (), False),
        ("read", "read", 200, "alice_corp", ("ALICE_CORP",), True),
        ("write", "write", 200, "alice_corp", (), True),
        ("maintain", "write", 200, "alice_corp", (), True),
        ("admin", "admin", 200, "alice_corp", (), True),
        ("read", "read", 200, "alice_corp", (), False),
        ("write", "write", 404, "alice_corp", (), False),
        ("admin", "admin", 200, "alice.corp", (), False),
        ("admin", "admin", 200, "alice%2fcorp", (), False),
        ("read", "read", 200, "HuMaN", ("human",), True),
        ("read", "read", 200, "human", ("HUMAN",), True),
        ("admin", "admin", 200, "helper[bot]", ("helper[bot]",), False),
        ("admin", "admin", 200, "helper[BOT]", ("helper[BOT]",), False),
        ("admin", "admin", 200, "", (), False),
        ("admin", "admin", 200, "../human", (), False),
    ],
)
def test_authorize_uses_effective_permission_and_maintainers(
    role, permission, status, login, maintainers, expected
):
    from agent_repo.gate import authorize

    transport = FakeTransport(
        {"permission": {"role_name": role, "permission": permission}, "permission_status": status}
    )
    gh = GitHub(TOKEN, "your-org/project", transport=transport)
    assert authorize(gh, login, frozenset(maintainers)) is expected
    if login in {"human", "alice_corp"} and not maintainers:
        assert transport.requests[0][0] == f"collaborators/{login}/permission"


@pytest.mark.parametrize("status", [0, 403, 429, 503])
def test_authorize_surfaces_api_errors(status):
    from agent_repo.gate import authorize

    gh = GitHub(TOKEN, "your-org/project", transport=FakeTransport({"permission_status": status}))
    with pytest.raises(GitHubError) as caught:
        authorize(gh, "alice_corp", frozenset())
    assert caught.value.status == status
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "api-error-401"),
        (403, "api-error-403"),
        (429, "api-error-429"),
        (404, "unauthorized"),
        (500, "api-error"),
        (503, "api-error"),
    ],
)
def test_permission_outages_report_distinct_api_errors(status, reason, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    case["permission_status"] = status
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and not result.tier2_allowed
    assert result.reason == reason
    assert TOKEN not in result.reason


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("path", ["issues/12/comments", "/repos/your-org/project"])
def test_counter_reads_report_distinct_api_errors(status, path, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-boundary"))
    case["fail_path"], case["fail_status"] = path, status
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == f"api-error-{status}"


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "synchronize"])
@pytest.mark.parametrize(
    "untrusted", ["human-author", "other-bot-author", "branch", "fork", "other-bot-sender"]
)
def test_bot_gets_agent_pr_reviews_only(action, untrusted, tmp_path):
    case_id = "agent-synchronize-self" if untrusted == "other-bot-sender" else "agent-draft"
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id))
    case["payload"]["action"] = action
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run
    pull = case["payload"]["pull_request"]
    if untrusted == "human-author":
        pull["user"] = {"login": "contributor", "type": "User"}
    elif untrusted == "other-bot-author":
        pull["user"] = {"login": "other[bot]", "type": "Bot"}
    elif untrusted == "branch":
        pull["head"]["ref"] = "feature/fix"
    elif untrusted == "other-bot-sender":
        case["payload"]["sender"] = {"login": "other[bot]", "type": "Bot"}
    else:
        pull["head"]["repo"]["full_name"] = "your-org/fork"
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == (
        "bot" if untrusted == "other-bot-sender" else "self"
    )


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_other_self_bot_events_stay_self(action, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-triage"))
    case["payload"]["action"] = action
    case["payload"]["sender"] = {"login": "helper[bot]", "type": "Bot"}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "self"


@pytest.mark.parametrize("workflow", ["review", "fix"])
@pytest.mark.parametrize("author", ["contributor", "other[bot]", "HELPER[bot]"])
def test_comment_exceptions_use_api_pr_authorship(workflow, author, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "fix-agent-draft"))
    case["workflow"] = workflow
    case["payload"]["comment"]["body"] = "/" + workflow
    case["pull"]["user"] = {"login": author}
    case["payload"]["issue"]["user"] = {"login": "spoofed-author"}
    case["pull"]["body"] = "Fixes #3"
    result, _, _, _ = evaluate_case(case, tmp_path)
    if author.casefold() == "helper[bot]":
        assert result.run
    else:
        assert not result.run
        assert result.reason == ("draft" if workflow == "review" else "not-agent-draft")


@pytest.mark.parametrize("count,capped", [(0, False), (2, False), (3, True), (4, True)])
def test_triage_marker_cap_and_common_request_count(count, capped, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    case["artifacts"] = [dict(case["artifacts"][0], id=i) for i in range(count)]
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.capped is capped and result.run is not capped
    assert result.reason == ("cycle-cap" if capped else "ready")
    queries = [
        q
        for p, q in transport.requests
        if p == "actions/artifacts" and q.get("name") == ["agent-triage-12"]
    ]
    assert queries == [{"name": ["agent-triage-12"], "per_page": ["100"], "page": ["1"]}]
    assert len(transport.requests) == (3 if capped else 5) + min(count, 3)
    assert sum(p.startswith("actions/runs/") for p, _ in transport.requests) == min(count, 3)


@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        {"workflow_run": {"repository_id": 7, "head_repository_id": 8}},
        {"workflow_run": {"repository_id": 8, "head_repository_id": 7}},
        {"name": "agent-triage-120"},
        {"name": "agent-triage-12-extra"},
        {"name": "agent-triage-13"},
    ],
)
def test_invalid_triage_markers_cannot_cap_or_debounce(edited, changed, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "debounce"))
    if not edited:
        case["payload"]["action"] = "opened"
    case["unfiltered_artifacts"] = True
    case["artifacts"] = [dict(case["artifacts"][0], **changed)] * 3
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and not result.capped and result.reason == "ready"


@pytest.mark.parametrize(
    "count,overflow", [(301, False), (999, False), (1000, False), (1001, True), (2000, True)]
)
def test_triage_marker_paging_is_bounded(count, overflow, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    foreign = dict(case["artifacts"][0], workflow_run={"repository_id": 7, "head_repository_id": 8})
    case["artifacts"] = [dict(foreign, id=i) for i in range(count)]
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.run is not overflow and not result.capped
    assert result.reason == ("counter-overflow" if overflow else "ready")
    queries = [
        q
        for p, q in transport.requests
        if p == "actions/artifacts" and q.get("name") == ["agent-triage-12"]
    ]
    assert len(queries) == min((count + 99) // 100, 10)
    assert [q["page"] for q in queries] == [[str(i + 1)] for i in range(len(queries))]
    assert len(transport.requests) <= 30
    assert not any(p.startswith("actions/runs/") for p, _ in transport.requests)


@pytest.mark.parametrize("page", [1, 2, 3])
def test_triage_marker_page_errors_fail_closed(page, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    case["artifacts"] = [
        dict(case["artifacts"][0], workflow_run={"repository_id": 7, "head_repository_id": 8})
    ] * 300
    case["fail_artifact_page"] = page
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and not result.tier2_allowed and not result.capped
    assert result.reason == "api-error"
    assert sum(p == "actions/artifacts" for p, _ in transport.requests) == page


@pytest.mark.parametrize("stage", ["comments", "marker", "global", "author", "permission", "pull"])
def test_shared_request_budget_fails_closed(stage, tmp_path):
    case_id = {
        "marker": "cycle-cap",
        "permission": "authorized-triage",
        "pull": "authorized-review",
    }
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id.get(stage, "content-edit")))
    comment = {"user": {"login": "contributor"}, "body": "context"}
    case["comments"] = [comment] * {
        "comments": 3000,
        "marker": 2599,
        "permission": 2799,
        "pull": 2799,
    }.get(stage, 0)
    if stage in {"global", "author"}:
        name = "tier2-20261014" + ("-contributor" if stage == "author" else "")
        case["artifacts"] = [
            {"name": name, "workflow_run": {"repository_id": 7, "head_repository_id": 8}}
        ] * 3100
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and not result.tier2_allowed and not result.capped
    assert result.reason == "request-budget"
    assert len(transport.requests) == 30
    if stage == "marker":
        assert sum(p.startswith("actions/runs/") for p, _ in transport.requests) == 2
    if stage == "permission":
        assert transport.requests[-1][0] == "/repos/your-org/project"
    if stage == "pull":
        assert transport.requests[0][0] == "collaborators/maintainer/permission"
        assert transport.requests[1][0] == "pulls/24"
        assert transport.requests[-1][0] == "issues/24/comments"


def test_marker_provenance_lookups_are_bounded(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "marker-other-workflow"))
    case["artifacts"] = [case["artifacts"][0]] * 100
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and not result.capped and not result.tier2_allowed
    assert result.reason == "request-budget"
    assert len(transport.requests) == 30
    assert sum(p.startswith("actions/runs/") for p, _ in transport.requests) == 27


@pytest.mark.parametrize(
    "workflow_ref,reason",
    [
        ("your-org/project/.github/workflows/other.yml@refs/heads/main", "ready"),
        ("your-org/project/.github/workflows/agent-triage.yml@refs/heads/topic@v2", "cycle-cap"),
        ("", "invalid-metadata"),
        ("your-org/foreign/.github/workflows/agent-triage.yml@refs/heads/main", "invalid-metadata"),
    ],
)
def test_marker_provenance_uses_invoking_workflow(workflow_ref, reason, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", workflow_ref)
    case = next(c for c in CASES if c["id"] == "cycle-cap")
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.reason == reason
    assert result.run is (reason == "ready")
    assert result.capped is (reason == "cycle-cap")


@pytest.mark.parametrize("case_id", ["content-edit", "reporter-needs-info", "authorized-triage"])
@pytest.mark.parametrize("unrelated", [0, 1000, 3000])
def test_no_marker_never_caps_or_debounces(case_id, unrelated, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id))
    marker = next(c for c in CASES if c["id"] == "cycle-cap")["artifacts"][0]
    case["artifacts"] = [dict(marker, name=f"ci-coverage-{i}") for i in range(unrelated)]
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.run and not result.capped and result.reason == "ready"
    assert not any("/runs" in p or p.endswith("/jobs") for p, _ in transport.requests)
    assert sum(q.get("name") == ["agent-triage-12"] for _, q in transport.requests) == 1
    assert len(transport.requests) == 5 + (case_id == "authorized-triage")


def test_review_command_on_issue_skips_before_api(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-triage"))
    case["workflow"] = "review"
    case["payload"]["comment"]["body"] = "/review"
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "unsupported-event"
    assert transport.requests == []


def test_authorized_review_overrides_first_time_cost_gate(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    case["payload"]["issue"]["author_association"] = "FIRST_TIME_CONTRIBUTOR"
    pull = json.loads(Path("tests/fixtures/events/pull_request_target.opened.json").read_text())
    case["pull"] = dict(pull["pull_request"], author_association="FIRST_TIME_CONTRIBUTOR")
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.reason == "ready"


def test_pull_request_review_gate_captures_event_head(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "known-review"))

    result, event, _, _ = evaluate_case(case, tmp_path)

    assert result.head_sha == event.head_sha


@pytest.mark.parametrize(
    "case_id",
    [
        "disabled",
        "self",
        "other-bot",
        "skip-label",
        "no-op-edit",
        "non-content-edit",
        "third-party-comment",
        "reporter-without-needs-info",
        "unauthorized-triage",
        "unauthorized-fix",
        "no-fix-trigger",
        "first-time-review",
        "human-draft",
    ],
)
def test_cheap_skips_never_read_counters(case_id, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id))
    case["artifacts"] = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap")["artifacts"])
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    if case_id.startswith("unauthorized"):
        assert [p for p, _ in transport.requests] == ["collaborators/maintainer/permission"]
    else:
        assert transport.requests == []


@pytest.mark.parametrize(
    "provenance",
    [
        {"repository_id": 7, "head_repository_id": 8},
        {"repository_id": 8, "head_repository_id": 8},
        {"repository_id": 8, "head_repository_id": 7},
    ],
)
@pytest.mark.parametrize("counter", ["author-cap", "global-cap"])
def test_foreign_artifacts_cannot_exhaust_tier2(provenance, counter, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == counter))
    case["artifacts"] = [dict(a, workflow_run=provenance) for a in case["artifacts"]]
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.tier2_allowed


def test_unfiltered_artifact_names_do_not_exhaust_tier2(tmp_path):
    case = dict(
        CASES[0],
        unfiltered_artifacts=True,
        artifacts=[
            {
                "name": "tier2-20261013",
                "workflow_run": {"repository_id": 7, "head_repository_id": 7},
            }
        ]
        * 25,
    )
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.tier2_allowed


@pytest.mark.parametrize("counter", ["cycle-cap", "author-cap", "global-cap"])
def test_counter_stops_paging_at_cap(counter, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == counter))
    case["artifacts"] *= 101
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    assert not any(query.get("page") == ["2"] for _, query in transport.requests)
    if counter == "global-cap":
        assert not any(
            q.get("name") == ["tier2-20261014-contributor"] for _, q in transport.requests
        )


@pytest.mark.parametrize(
    "path",
    [
        "/repos/your-org/project",
        "actions/artifacts",
        "actions/runs/101",
    ],
)
def test_counter_api_errors_fail_closed(path, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-boundary"))
    case["fail_path"] = path
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and not result.tier2_allowed and result.reason == "api-error"


@pytest.mark.parametrize("failure", ["http", "transport", "malformed"])
def test_api_errors_fail_closed_without_credentials(failure, tmp_path, capsys):
    case = dict(CASES[0], failure=failure)
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert not result.tier2_allowed
    assert result.reason in {"api-error", "invalid-metadata"}
    assert TOKEN not in result.reason + str(capsys.readouterr())


def test_canary_is_fresh_and_32_hex_characters(tmp_path):
    from agent_repo.gate import evaluate

    first, event, ctx, _ = evaluate_case(CASES[0], tmp_path)
    second = evaluate(event, ctx)
    assert re.fullmatch(r"[0-9a-f]{32}", first.canary)
    assert re.fullmatch(r"[0-9a-f]{32}", second.canary)
    assert first.canary != second.canary


@pytest.mark.parametrize("counter", ["cycle-cap", "author-cap", "global-cap"])
def test_counters_include_paginated_metadata(counter, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == counter))
    other = dict(case["artifacts"][0], workflow_run={"repository_id": 7, "head_repository_id": 8})
    case["artifacts"] = [other] * 100 + case["artifacts"]
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    assert any(query.get("page") == ["2"] for _, query in transport.requests)


@pytest.mark.parametrize("author", ["contributor", "CoNtRiBuToR"])
def test_tier2_artifact_filters_use_utc_day_and_author(author, tmp_path):
    from agent_repo.gate import evaluate

    _, event, ctx, transport = evaluate_case(CASES[0], tmp_path)
    transport.requests.clear()
    ctx = replace(ctx, now=datetime.fromisoformat("2026-10-13T23:30:00-01:00"))
    assert evaluate(replace(event, author=author), ctx).tier2_allowed
    names = [query.get("name") for path, query in transport.requests if path == "actions/artifacts"]
    assert names == [["agent-triage-12"], ["tier2-20261014"], ["tier2-20261014-contributor"]]


def test_command_cannot_cross_workflows(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-fix"))
    case["workflow"] = "triage"
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert result.reason == "wrong-command"


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_only_created_comments_can_trigger_commands(action, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-fix"))
    case["payload"]["action"] = action
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert result.reason == "unsupported-event"


def test_first_time_human_draft_is_skipped_as_draft(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "human-draft"))
    case["payload"]["pull_request"]["author_association"] = "FIRST_TIME_CONTRIBUTOR"
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert result.reason == "draft"


def test_triage_skips_bot_authors_even_for_a_human_sender(tmp_path):
    case = copy.deepcopy(CASES[0])
    case["payload"]["issue"]["user"]["login"] = "other[bot]"
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert result.reason == "bot"


@pytest.mark.parametrize("permission", [[], {}, None, "WRITE", "maintain"])
def test_malformed_effective_permission_fails_closed(permission):
    from agent_repo.gate import authorize

    data = {"role_name": "write", "permission": permission}
    gh = GitHub(TOKEN, "your-org/project", transport=FakeTransport({"permission": data}))
    assert authorize(gh, "human", frozenset()) is False


@pytest.mark.parametrize(
    "permission,reason",
    [
        ({"role_name": "release-writer", "permission": "write"}, "ready"),
        ({"role_name": "maintain", "permission": "write"}, "ready"),
        ({"role_name": "triage", "permission": "read"}, "unauthorized"),
        ({"role_name": "custom-triager", "permission": "read"}, "unauthorized"),
    ],
)
@pytest.mark.parametrize("case_id", ["authorized-triage", "authorized-fix", "authorized-label"])
def test_gate_writers_use_effective_permission(case_id, permission, reason, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id))
    case["permission"] = permission
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.reason == reason and result.run is (reason == "ready")


def off_marker_comment(user):
    return {"user": user, "body": "Agent automation is off.\n\n<!-- agent:off run=123 -->"}


@pytest.mark.parametrize(
    "user,honored",
    [
        ({"login": "helper[bot]", "type": "Bot"}, True),
        ({"login": "HELPER[BOT]", "type": "Bot"}, True),
        ({"login": "helper", "type": "User"}, False),
        ({"login": "Helper", "type": "User"}, False),
        ({"login": "helper[bot]", "type": "User"}, False),
        ({"login": "helper[bot]", "type": None}, False),
        ({"login": "other[bot]", "type": "Bot"}, False),
        ({"login": "helper.bot", "type": "Bot"}, False),
    ],
)
@pytest.mark.parametrize("case_id", ["content-edit", "authorized-review", "fix-agent-draft"])
def test_off_marker_needs_the_canonical_bot_account(case_id, user, honored, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == case_id))
    if case_id.startswith("fix"):
        case["pull"]["body"] = "Fixes #3"
    case["comments"] = [off_marker_comment(user)]
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.reason == ("off" if honored else "ready")
    assert result.run is not honored


@pytest.mark.parametrize("workflow", ["review", "triage", "fix"])
@pytest.mark.parametrize("authorized", [True, False])
def test_agent_off_on_a_pr_routes_to_the_review_workflow(workflow, authorized, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    case["workflow"] = workflow
    case["payload"]["comment"]["body"] = "/agent off"
    case["payload"]["issue"]["draft"] = True
    pull = json.loads(Path("tests/fixtures/events/pull_request_target.opened.json").read_text())
    case["pull"] = dict(pull["pull_request"], draft=True)
    if not authorized:
        case["permission"] = {"role_name": "triage", "permission": "read"}
    result, _, _, _ = evaluate_case(case, tmp_path)
    expected = {
        "review": "agent-off" if authorized else "unauthorized",
        "triage": "unsupported-event",
        "fix": "wrong-command",
    }[workflow]
    assert result.reason == expected
    assert result.run is (expected == "agent-off")
    assert not result.tier2_allowed
    if expected == "agent-off":
        assert result.head_sha == pull["pull_request"]["head"]["sha"]


def test_agent_off_on_an_issue_is_not_a_review_command(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-agent-off"))
    case["workflow"] = "review"
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "unsupported-event"
    assert transport.requests == []


def test_agent_off_on_a_pr_is_posted_by_apply_and_skips_review_and_fix(tmp_path, monkeypatch):
    from agent_repo.charter import parse_charter
    from agent_repo.commands import review_apply

    off_case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    off_case["payload"]["comment"]["body"] = "/agent off"
    gate_result, _, _, _ = evaluate_case(off_case, tmp_path)
    assert gate_result.run and gate_result.reason == "agent-off"

    posted = []

    def apply_transport(request, timeout):
        path = urlparse(request.full_url).path.removeprefix("/repos/your-org/project/")
        if request.method == "POST":
            # The App token's comment is recorded as the bot account, as GitHub does.
            body = json.loads(request.data)["body"]
            posted.append({"user": {"login": "helper[bot]", "type": "Bot"}, "body": body})
            return 201, {}, b"{}"
        if path == "contents/MAINTAINERS":
            return 404, {}, b"{}"
        if path.endswith("/permission"):
            return 200, {}, json.dumps({"role_name": "write", "permission": "write"}).encode()
        if path == "issues/24/comments":
            return 200, {}, json.dumps(posted).encode()
        return 200, {}, json.dumps({"labels": [], "assignees": [], "state": "open"}).encode()

    event_path = tmp_path / "comment.json"
    event_path.write_text(json.dumps(off_case["payload"]))
    config = RepoConfig({"mode": {"review": "live"}}, (), ("needs-maintainer",))
    charter = parse_charter(Path("tests/fixtures/charter/example.md").read_text())
    monkeypatch.setattr(review_apply, "_policy", lambda gh, sha, directory: (sha, config, charter))
    monkeypatch.setattr(
        review_apply, "GitHub", lambda token, repo: GitHub(token, repo, transport=apply_transport)
    )
    for key, value in {
        "AGENT_OFF_AUTHORIZED": "true",
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_SHA": EVENT_SHA,
        "GITHUB_RUN_ID": "99",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GH_TOKEN": TOKEN,
        "EXPECTED_HEAD_SHA": gate_result.head_sha,
        "AGENT_SCAN_KEYS": "scan-key",
        "AGENT_CANARY": gate_result.canary,
        "AGENT_BOT_LOGIN": "helper[bot]",
    }.items():
        monkeypatch.setenv(key, value)
    assert review_apply.main([str(tmp_path / "missing-review.json")]) == 0
    assert len(posted) == 1 and "<!-- agent:off run=99 -->" in posted[0]["body"]

    review_case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    fix_case = feedback_case()
    fix_case["payload"]["comment"]["body"] = "/fix address the review feedback"
    for case in (review_case, fix_case):
        case["comments"] = posted
        result, _, _, _ = evaluate_case(case, tmp_path)
        assert not result.run and result.reason == "off"
        assert fix_outputs(result) == ("", "", "", "")


def fix_outputs(result):
    return (result.issue, result.base_sha, result.head_ref, result.head_sha)


def feedback_case(**pull_changes):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "fix-agent-draft"))
    case["pull"]["body"] = "Follow-up for the parser.\n\nFixes #3"
    case["pull"].update(pull_changes)
    return case


def test_new_fix_outputs_issue_branch_and_event_base(tmp_path):
    for case_id, issue in (("authorized-label", "31"), ("authorized-fix", "12")):
        case = next(c for c in CASES if c["id"] == case_id)
        result, _, _, _ = evaluate_case(case, tmp_path)
        assert result.run and result.reason == "ready"
        assert (result.issue, result.base_sha, result.head_ref, result.head_sha) == (
            issue,
            EVENT_SHA,
            f"agent/fix-{issue}",
            "",
        )


@pytest.mark.parametrize("event_sha", ["", "C" * 40, "c" * 39, "main"])
def test_new_fix_requires_a_commit_event_sha(event_sha, tmp_path):
    case = dict(next(c for c in CASES if c["id"] == "authorized-label"), event_sha=event_sha)
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "invalid-metadata"
    assert fix_outputs(result) == ("", "", "", "")


def test_feedback_fix_outputs_pr_head_and_agent_base(tmp_path):
    result, _, _, transport = evaluate_case(feedback_case(), tmp_path)
    head = "a" * 40
    assert result.run and result.reason == "ready"
    assert (result.issue, result.base_sha, result.head_ref, result.head_sha) == (
        "3",
        AGENT_BASE,
        "agent/fix-3",
        head,
    )
    assert f"git/commits/{head}" in [path for path, _ in transport.requests]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"draft": False}, "not-agent-draft"),
        ({"user": {"login": "helper", "type": "User"}}, "not-agent-draft"),
        ({"user": {"login": "helper[bot]", "type": "User"}}, "not-agent-draft"),
        ({"user": {"login": "other[bot]", "type": "Bot"}}, "not-agent-draft"),
        (
            {"head": {"ref": "agent/fix-3", "sha": "a" * 40, "repo": {"full_name": "x/fork"}}},
            "not-agent-draft",
        ),
        (
            {
                "head": {
                    "ref": "agent/fix-3-retry",
                    "sha": "a" * 40,
                    "repo": {"full_name": "your-org/project"},
                }
            },
            "not-agent-draft",
        ),
        (
            {
                "head": {
                    "ref": "agent/fix-03",
                    "sha": "a" * 40,
                    "repo": {"full_name": "your-org/project"},
                }
            },
            "not-agent-draft",
        ),
        (
            {
                "head": {
                    "ref": "agent/review-3",
                    "sha": "a" * 40,
                    "repo": {"full_name": "your-org/project"},
                }
            },
            "not-agent-draft",
        ),
        ({"body": "Updates the parser"}, "unlinked-issue"),
        ({"body": "Fixes #31"}, "unlinked-issue"),
        ({"body": "Prefixes #3"}, "unlinked-issue"),
        ({"body": "See #3"}, "unlinked-issue"),
        ({"body": None}, "unlinked-issue"),
        ({"body": "closes #3."}, "ready"),
        ({"body": "Resolved: #3"}, "ready"),
    ],
)
def test_feedback_fix_refuses_untrusted_pull_requests(changes, reason, tmp_path):
    result, _, _, transport = evaluate_case(feedback_case(**changes), tmp_path)
    assert result.reason == reason and result.run is (reason == "ready")
    if reason != "ready":
        assert fix_outputs(result) == ("", "", "", "")
        assert not any(path.startswith("git/commits/") for path, _ in transport.requests)


@pytest.mark.parametrize(
    "message",
    [
        "Fix the parser",
        f"Fix\n\nAgent-Base: {AGENT_BASE}\nAgent-Base: {AGENT_BASE}",
        f"Fix\n\nAgent-Base: {AGENT_BASE}\nagent-base: {'e' * 40}",
        f"Fix\n\nAgent-Base: {AGENT_BASE[:-1]}",
        f"Fix\n\nAgent-Base: {AGENT_BASE.upper()}",
        "Fix\n\nAgent-Base: main",
        f"Agent-Base: {AGENT_BASE}",
        f"Fix\n\nAgent-Base: {AGENT_BASE}\n\nThe parser now handles empty input.",
        f"Fix\n\nAgent-Base: {AGENT_BASE}\nfollow-up for the parser",
        None,
    ],
    ids=[
        "none",
        "two",
        "two-casefolded",
        "short",
        "uppercase",
        "ref-name",
        "subject-only",
        "body-only",
        "mixed-final-paragraph",
        "missing-message",
    ],
)
def test_feedback_fix_needs_exactly_one_agent_base_trailer(message, tmp_path):
    case = feedback_case()
    case["commit"] = {"sha": "a" * 40, "message": message}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run
    assert result.reason == ("invalid-metadata" if message is None else "invalid-agent-base")
    assert fix_outputs(result) == ("", "", "", "")


def test_feedback_fix_reads_agent_base_from_the_trailer_block_only(tmp_path):
    case = feedback_case()
    quoted = f"Agent-Base: {'e' * 40} was the first attempt."
    message = f"Fix the parser\n\n{quoted}\n\nAgent-Run: 41\nAgent-Base: {AGENT_BASE}\n"
    case["commit"] = {"sha": "a" * 40, "message": message}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.base_sha == AGENT_BASE


def test_feedback_fix_api_error_emits_no_fix_outputs(tmp_path):
    case = feedback_case()
    case["fail_path"], case["fail_status"] = f"git/commits/{'a' * 40}", 403
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "api-error-403"
    assert fix_outputs(result) == ("", "", "", "")


def test_feedback_fix_rejects_a_commit_for_another_head(tmp_path):
    case = feedback_case()
    case["commit"] = {"sha": "f" * 40, "message": f"Fix\n\nAgent-Base: {AGENT_BASE}"}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "invalid-agent-base"


def prepare_cli(tmp_path, monkeypatch, enabled="true"):
    checkout = tmp_path / "agent-gate-default"
    shutil.copytree("tests/fixtures/agents", checkout / ".agents")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(CASES[0]["payload"]))
    output = tmp_path / "github-output"
    for key, value in {
        "AGENTS_ENABLED": enabled,
        "AGENT_BOT_LOGIN": "helper[bot]",
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_RUN_ID": "99",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_TOKEN": TOKEN,
        "GITHUB_SHA": EVENT_SHA,
    }.items():
        monkeypatch.setenv(key, value)
    return checkout, event_path, output


def read_outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.mark.parametrize("enabled", ["", "false", "TRUE", "1", " true"])
def test_cli_requires_literal_true_and_emits_all_outputs(enabled, tmp_path, monkeypatch):
    from agent_repo.commands.gate import main

    checkout, _, output = prepare_cli(tmp_path, monkeypatch, enabled)
    assert main(["triage", str(checkout)]) == 0
    values = read_outputs(output)
    assert values == {
        "run": "false",
        "reason": "disabled",
        "tier2_allowed": "false",
        "capped": "false",
        "command": "",
        "command_args": "",
        "canary": values["canary"],
        "head_sha": "",
        "issue": "",
        "base_sha": "",
        "head_ref": "",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", values["canary"])
    assert TOKEN not in output.read_text()


def test_cli_disabled_ignores_malformed_config(tmp_path, monkeypatch, capsys):
    from agent_repo.commands.gate import main

    checkout, _, output = prepare_cli(tmp_path, monkeypatch, "false")
    (checkout / ".agents/config.yml").write_text("invalid: [")
    assert main(["triage", str(checkout)]) == 0
    values = read_outputs(output)
    assert values["reason"] == "disabled"
    assert values["run"] == values["tier2_allowed"] == values["capped"] == "false"
    assert re.fullmatch(r"[0-9a-f]{32}", values["canary"])
    assert capsys.readouterr().err == ""


def test_cli_reads_maintainers_from_default_checkout_and_preserves_args(tmp_path, monkeypatch):
    from agent_repo.commands import gate

    checkout, event_path, output = prepare_cli(tmp_path, monkeypatch)
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-fix"))
    case["payload"]["comment"]["body"] += "\nrun=false"
    event_path.write_text(json.dumps(case["payload"]))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    (checkout / "MAINTAINERS").write_text("# Trusted maintainers\n\nMAINTAINER # owner\n")
    (tmp_path / "MAINTAINERS").write_text("untrusted\n")
    gh = GitHub(TOKEN, "your-org/project", transport=FakeTransport({}))
    monkeypatch.setattr(gate, "GitHub", lambda *args, **kwargs: gh)
    assert gate.main(["fix", str(checkout)]) == 0
    values = read_outputs(output)
    assert values["run"] == "true"
    assert values["tier2_allowed"] == "true"
    assert values["command"] == "fix"
    assert values["command_args"] == "handle the None case"
    assert (values["issue"], values["base_sha"], values["head_ref"]) == (
        "12",
        EVENT_SHA,
        "agent/fix-12",
    )
    assert TOKEN not in output.read_text()


@pytest.mark.parametrize(
    "bot_login,reason",
    [("helper", "ready"), ("helper[bot]", "ready"), ("bad login", "invalid-metadata")],
)
def test_cli_canonicalizes_the_bot_login(bot_login, reason, tmp_path, monkeypatch):
    from agent_repo.commands import gate

    checkout, event_path, output = prepare_cli(tmp_path, monkeypatch)
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "content-edit"))
    case["comments"] = [off_marker_comment({"login": "helper", "type": "User"})]
    event_path.write_text(json.dumps(case["payload"]))
    monkeypatch.setenv("AGENT_BOT_LOGIN", bot_login)
    gh = GitHub(TOKEN, "your-org/project", transport=FakeTransport(case))
    monkeypatch.setattr(gate, "GitHub", lambda *args, **kwargs: gh)
    assert gate.main(["triage", str(checkout)]) == 0
    assert read_outputs(output)["reason"] == reason


@pytest.mark.parametrize(
    "base_ref,reason",
    [("main", "ready"), ("release", "non-default-base"), (None, "non-default-base")],
)
def test_cli_pull_request_target_requires_default_branch_base(
    base_ref, reason, tmp_path, monkeypatch
):
    from agent_repo.commands import gate

    checkout, event_path, output = prepare_cli(tmp_path, monkeypatch)
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "known-review"))
    if base_ref is not None:
        case["payload"]["pull_request"]["base"]["ref"] = base_ref
    event_path.write_text(json.dumps(case["payload"]))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    transport = FakeTransport(case)
    monkeypatch.setattr(
        gate,
        "GitHub",
        lambda *args, **kwargs: GitHub(TOKEN, "your-org/project", transport=transport),
    )
    assert gate.main(["review", str(checkout)]) == 0
    values = read_outputs(output)
    assert values["reason"] == reason and values["run"] == str(reason == "ready").lower()
    if reason != "ready":
        assert transport.requests == []


def test_gate_policy_checkout_is_the_event_commit_not_the_moving_branch():
    action = load_yaml(Path("actions/gate/action.yml"))
    checkout = next(step for step in action["runs"]["steps"] if "uses" in step)
    # A default-branch push after the event must not change the policy the gate reads.
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert "default_branch" not in json.dumps(action)


def test_review_gate_outputs_pr_head_captured_for_issue_comment(tmp_path, monkeypatch):
    from agent_repo.commands import gate

    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    checkout, event_path, output = prepare_cli(tmp_path, monkeypatch)
    event_path.write_text(json.dumps(case["payload"]))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issue_comment")
    gh = GitHub(TOKEN, "your-org/project", transport=FakeTransport(case))
    monkeypatch.setattr(gate, "GitHub", lambda *args, **kwargs: gh)

    assert gate.main(["review", str(checkout)]) == 0

    values = read_outputs(output)
    assert values["run"] == "true"
    assert (
        values["head_sha"]
        == case.get(
            "pull",
            json.loads(Path("tests/fixtures/events/pull_request_target.opened.json").read_text())[
                "pull_request"
            ],
        )["head"]["sha"]
    )


@pytest.mark.parametrize("failure", ["config", "event", "maintainers"])
def test_cli_errors_emit_closed_outputs_without_credentials(failure, tmp_path, monkeypatch, capsys):
    from agent_repo.commands.gate import main

    checkout, event_path, output = prepare_cli(tmp_path, monkeypatch)
    if failure == "config":
        (checkout / ".agents/config.yml").write_text(TOKEN)
    elif failure == "event":
        event_path.write_text(TOKEN)
    else:
        (checkout / "MAINTAINERS").mkdir()
    assert main(["triage", str(checkout)]) == 1
    values = read_outputs(output)
    assert values["run"] == "false" and values["tier2_allowed"] == "false"
    assert TOKEN not in output.read_text() + str(capsys.readouterr())


@pytest.mark.parametrize("args", [[], ["unknown", "."], ["triage"]])
def test_cli_rejects_invalid_arguments(args, capsys):
    from agent_repo.commands.gate import main

    assert main(args) == 2
    assert "usage:" in capsys.readouterr().err


def test_composite_action_input_defaults_use_runner_contexts():
    action = load_yaml(Path("actions/gate/action.yml"))
    # actions/runner src/Runner.Worker/action_yaml.json defines input-default-context.
    allowed = {"github", "strategy", "matrix", "job", "runner", "hashFiles"}
    for name, definition in action["inputs"].items():
        for expression in re.findall(r"\$\{\{(.*?)\}\}", str(definition.get("default", "")), re.S):
            expression = re.sub(r"'(?:[^']|'')*'", "", expression)
            identifiers = set(re.findall(r"(?<![\w.-])\b([A-Za-z_]\w*)", expression))
            assert identifiers - {"true", "false", "null"} <= allowed, (name, expression)


@pytest.mark.parametrize("enabled", ["false", "", None])
def test_composite_action_executes_gate_and_exposes_outputs(enabled, tmp_path, monkeypatch):
    checkout, _, _ = prepare_cli(tmp_path, monkeypatch, enabled or "")
    if enabled is None:
        monkeypatch.delenv("AGENTS_ENABLED")
    shutil.rmtree(checkout)
    action_path = Path("actions/gate").resolve()
    action = load_yaml(action_path / "action.yml")
    for name in ("enabled", "bot-login"):
        assert action["inputs"][name].get("required") is True
        assert "default" not in action["inputs"][name]
    checkout_step = next(step for step in action["runs"]["steps"] if "uses" in step)
    assert checkout_step.get("if") == "inputs.enabled == 'true'"
    step = next(step for step in action["runs"]["steps"] if step.get("id") == "gate")
    assert step["env"]["AGENTS_ENABLED"] == "${{ inputs.enabled }}"
    assert not checkout.exists()
    env = dict(
        os.environ,
        GITHUB_ACTION_PATH=str(action_path),
        GITHUB_WORKSPACE=str(tmp_path),
        PYTHONPATH=str(Path("lib").resolve()),
        GATE_WORKFLOW="triage",
    )
    process = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr
    values = read_outputs(tmp_path / "github-output")
    assert values["run"] == "false" and values["reason"] == "disabled"
    assert set(action["outputs"]) == set(values)
    for key in values:
        assert action["outputs"][key]["value"] == "${{ steps.gate.outputs." + key + " }}"
