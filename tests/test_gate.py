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
            return 503, {}, b"{}"
        if path.endswith("/permission"):
            status = self.case.get("permission_status", 200)
            data = self.case.get("permission", {"role_name": "read", "permission": "read"})
        elif path.endswith("/comments"):
            status, data = 200, self.case.get("comments", [])
        elif path == "actions/runs":
            status, data = 200, {"workflow_runs": self.case.get("runs", [])}
        elif path == "actions/runs/99":
            status, data = (
                200,
                {
                    "id": 99,
                    "workflow_id": 77,
                    "path": ".github/workflows/agent-triage.yml",
                    "repository": {"id": 7, "full_name": "your-org/project"},
                },
            )
        elif path == "issues/12":
            status, data = 200, {"created_at": "2026-10-01T00:00:00Z"}
        elif path == "actions/workflows/77/runs":
            runs = self.case.get("runs", [])
            if not self.case.get("unfiltered_runs"):
                runs = [r for r in runs if r["event"] == query["event"][0]]
            status, data = 200, {"workflow_runs": runs}
        elif path.endswith("/jobs"):
            run_id = path.split("/")[-2]
            jobs = self.case.get("jobs", {}).get(
                run_id,
                [
                    {"name": "propose", "status": "completed", "conclusion": "success"},
                ],
            )
            status, data = 200, {"jobs": jobs}
        elif path == "actions/artifacts":
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
        elif path.startswith("pulls/"):
            default = json.loads(
                Path("tests/fixtures/events/pull_request_target.opened.json").read_text()
            )
            status, data = 200, self.case.get("pull", default["pull_request"])
        else:
            raise AssertionError("unexpected API endpoint")
        if self.case.get("failure") == "malformed":
            data = None
        headers = {}
        if isinstance(data, dict) and any(
            k in data for k in ("workflow_runs", "artifacts", "jobs")
        ):
            key = next(k for k in ("workflow_runs", "artifacts", "jobs") if k in data)
            items = data[key]
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * 100
            data = {key: items[start : start + 100]}
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
        config=RepoConfig({"limits": LIMITS}, (), ()),
        gh=GitHub(TOKEN, event.repo, transport=transport),
        now=NOW,
        maintainers=frozenset(),
        run_id="99",
    )
    return evaluate(event, ctx), event, ctx, transport


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_gate_event_rules(case, tmp_path):
    result, _, _, _ = evaluate_case(case, tmp_path)
    observed = {key: getattr(result, key) for key in case["expected"]}
    assert observed == case["expected"]
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
        (None, "admin", 200, "human", (), False),
        ("custom", "admin", 200, "human", (), False),
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
def test_authorize_uses_role_name_and_maintainers(
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


@pytest.mark.parametrize("status", [403, 429, 503])
def test_permission_outages_report_api_error(status, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    case["permission_status"] = status
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and not result.tier2_allowed
    assert result.reason == "api-error"


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "synchronize"])
def test_bot_gets_agent_pr_reviews_only(action, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "agent-draft"))
    case["payload"]["action"] = action
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run
    case["payload"]["pull_request"]["head"]["repo"]["full_name"] = "your-org/fork"
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "self"


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_other_self_bot_events_stay_self(action, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-triage"))
    case["payload"]["action"] = action
    case["payload"]["sender"] = {"login": "helper[bot]", "type": "Bot"}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert not result.run and result.reason == "self"


def test_authorized_review_overrides_first_time_cost_gate(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "authorized-review"))
    case["payload"]["issue"]["author_association"] = "FIRST_TIME_CONTRIBUTOR"
    pull = json.loads(Path("tests/fixtures/events/pull_request_target.opened.json").read_text())
    case["pull"] = dict(pull["pull_request"], author_association="FIRST_TIME_CONTRIBUTOR")
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.reason == "ready"


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
    case["runs"] = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap")["runs"])
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    assert not any(path.startswith("actions/") for path, _ in transport.requests)


@pytest.mark.parametrize(
    "jobs",
    [
        [
            {"name": "gate", "status": "completed", "conclusion": "success"},
            {"name": "propose", "status": "completed", "conclusion": "skipped"},
        ],
        [{"name": "propose", "status": "queued", "conclusion": None}],
        [],
    ],
)
@pytest.mark.parametrize("edited", [False, True])
def test_skipped_propose_cannot_cap_or_debounce(jobs, edited, tmp_path):
    case = copy.deepcopy(
        next(c for c in CASES if c["id"] == "debounce")
        if edited
        else next(c for c in CASES if c["id"] == "cycle-cap")
    )
    case["jobs"] = {str(run["id"]): jobs for run in case["runs"]}
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and result.reason == "ready" and not result.capped


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("completed", "success"),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("in_progress", None),
    ],
)
def test_started_propose_runs_still_count(status, conclusion, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    case["jobs"] = {
        str(r["id"]): [
            {"name": "propose", "status": status, "conclusion": conclusion},
        ]
        for r in case["runs"]
    }
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert not result.run and result.capped
    assert any(path.endswith("/jobs") for path, _ in transport.requests)


def test_counter_query_is_workflow_event_and_issue_scoped(tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    case["runs"][0]["event"] = "issue_comment"
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.capped
    queries = [query for path, query in transport.requests if path == "actions/workflows/77/runs"]
    assert {q["event"][0] for q in queries} == {"issues", "issue_comment"}
    assert all(q["created"] == [">=2026-10-01T00:00:00Z"] for q in queries)
    assert not any(path == "actions/runs" for path, _ in transport.requests)


@pytest.mark.parametrize(
    "changed",
    [
        {"head_repository": {"id": 8}},
        {"repository": {"id": 8}},
        {"workflow_id": 78},
        {"path": ".github/workflows/untrusted.yml"},
        {"event": "pull_request"},
        {"created_at": "2026-09-30T23:59:59Z"},
    ],
)
def test_foreign_runs_cannot_cap_issue(changed, tmp_path):
    case = copy.deepcopy(next(c for c in CASES if c["id"] == "cycle-cap"))
    case["unfiltered_runs"] = True
    case["runs"] = [dict(run, **changed) for run in case["runs"]]
    result, _, _, _ = evaluate_case(case, tmp_path)
    assert result.run and not result.capped and result.tier2_allowed


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
    if counter == "cycle-cap":
        run = case["runs"][0]
        case["runs"] = [dict(run, id=1000 + i) for i in range(201)]
    else:
        case["artifacts"] *= 101
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    assert not any(query.get("page") == ["2"] for _, query in transport.requests)
    if counter == "cycle-cap":
        assert sum(path.endswith("/jobs") for path, _ in transport.requests) == 3
    if counter == "global-cap":
        assert not any(
            q.get("name") == ["tier2-20261014-contributor"] for _, q in transport.requests
        )


@pytest.mark.parametrize(
    "path",
    [
        "actions/runs/99",
        "issues/12",
        "actions/workflows/77/runs",
        "actions/runs/1/jobs",
        "actions/artifacts",
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
    if counter == "cycle-cap":
        other = dict(case["runs"][0], display_title="agent-triage #999")
        case["runs"] = [dict(other, id=1000 + i) for i in range(100)] + case["runs"]
    else:
        other = dict(
            case["artifacts"][0], workflow_run={"repository_id": 7, "head_repository_id": 8}
        )
        case["artifacts"] = [other] * 100 + case["artifacts"]
    result, _, _, transport = evaluate_case(case, tmp_path)
    assert result.reason == case["expected"]["reason"]
    assert any(query.get("page") == ["2"] for _, query in transport.requests)


def test_tier2_artifact_filters_use_utc_day_and_author(tmp_path):
    from agent_repo.gate import evaluate

    _, event, ctx, transport = evaluate_case(CASES[0], tmp_path)
    transport.requests.clear()
    ctx = replace(ctx, now=datetime.fromisoformat("2026-10-13T23:30:00-01:00"))
    assert evaluate(event, ctx).tier2_allowed
    names = [query.get("name") for path, query in transport.requests if path == "actions/artifacts"]
    assert names == [["tier2-20261014"], ["tier2-20261014-contributor"]]


@pytest.mark.parametrize(
    "field", ["triage_runs_per_issue", "tier2_per_author_per_day", "tier2_per_day"]
)
def test_configured_counter_limits_are_enforced(field, tmp_path):
    from agent_repo.gate import evaluate

    _, event, ctx, _ = evaluate_case(CASES[0], tmp_path)
    limits = dict(LIMITS, **{field: 0})
    result = evaluate(event, replace(ctx, config=RepoConfig({"limits": limits}, (), ())))
    if field == "triage_runs_per_issue":
        assert not result.run and result.capped
    else:
        assert result.run and not result.tier2_allowed


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


@pytest.mark.parametrize("role", [[], {}])
def test_malformed_permission_role_fails_closed(role):
    from agent_repo.gate import authorize

    gh = GitHub(
        TOKEN, "your-org/project", transport=FakeTransport({"permission": {"role_name": role}})
    )
    assert authorize(gh, "human", frozenset()) is False


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
    }
    assert re.fullmatch(r"[0-9a-f]{32}", values["canary"])
    assert TOKEN not in output.read_text()


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
    assert values["command"] == "fix"
    assert values["command_args"] == "handle the None case"
    assert TOKEN not in output.read_text()


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


def test_composite_action_executes_gate_and_exposes_outputs(tmp_path, monkeypatch):
    prepare_cli(tmp_path, monkeypatch, "false")
    action_path = Path("actions/gate").resolve()
    action = load_yaml(action_path / "action.yml")
    step = next(step for step in action["runs"]["steps"] if step.get("id") == "gate")
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
