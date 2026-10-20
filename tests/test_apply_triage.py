import json
import math
import os
import re
import secrets
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest
from agent_repo.charter import DocRef, parse_charter
from agent_repo.config import Area, RepoConfig
from agent_repo.event import load_event
from agent_repo.github import GitHub


class API:
    def __init__(self, *, target_open=True, labels=(), assignees=(), comments=(), fail=None):
        self.calls = []
        self.target_open = target_open
        self.labels = list(labels)
        self.assignees = list(assignees)
        self.comments = list(comments)
        self.fail = fail
        self.issue_state = "open"
        self.closed_by = None
        self.gh = GitHub("test-token", "your-org/project", transport=self.transport)

    def transport(self, request, timeout):
        path = urlparse(request.full_url).path.removeprefix("/repos/your-org/project/")
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, body))
        if self.fail and self.fail in path:
            return 500, {}, b"{}"
        if request.method != "GET":
            if path.endswith("/labels"):
                self.labels = list(dict.fromkeys([*self.labels, *body["labels"]]))
            elif path.endswith("/assignees"):
                self.assignees = list(dict.fromkeys([*self.assignees, *body["assignees"]]))
            elif path.endswith("/comments"):
                self.comments.append(
                    {"body": body["body"], "user": {"login": "agent-apply[bot]", "type": "Bot"}}
                )
            elif request.method == "PATCH" and path == "issues/7":
                self.issue_state = body["state"]
                self.closed_by = {"login": "agent-apply[bot]", "type": "Bot"}
            return 200, {}, b"{}"
        if path.endswith("/comments"):
            value = self.comments
        elif path == "contents/MAINTAINERS":
            return 404, {}, b"{}"
        elif path.endswith("/permission"):
            value = {"role_name": "write"}
        else:
            value = {
                "number": int(path.split("/")[-1]) if path.startswith("issues/") else 7,
                "state": self.issue_state
                if path == "issues/7"
                else ("open" if self.target_open else "closed"),
                "labels": [{"name": label} for label in self.labels],
                "assignees": [{"login": owner} for owner in self.assignees],
                "closed_by": self.closed_by,
            }
        return 200, {}, json.dumps(value).encode()

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def context(api=None, *, enabled=False, capped=False, off=False):
    from agent_repo.apply.actions import ApplyContext

    event = replace(
        load_event("issues", Path("tests/fixtures/events/issues.opened.json")), number=7
    )
    config = RepoConfig(
        {
            "thresholds": {"apply_min": 0.8, "close_min": 0.92, "close_lexical_min": 0.45},
            "system_one": {"provider": "jev", "model": "model"},
            "close": {"enabled": enabled},
            "mode": {"triage": "live"},
        },
        (Area("api", "API", ("src/**",), ("octocat",), False),),
        (
            "type:bug",
            "area:api",
            "P0",
            "P1",
            "P2",
            "P3",
            "needs-info",
            "needs-maintainer",
            "agent:closed",
            "agent:fix",
            "agent:triage-capped",
        ),
    )
    charter = replace(
        parse_charter(Path("tests/fixtures/charter/example.md").read_text()),
        docs_index=(DocRef("Guide", "https://example.org/guide"),),
    )
    return ApplyContext(
        event,
        config,
        charter,
        "run-1",
        "a" * 40,
        [secrets.token_urlsafe(32)],
        secrets.token_hex(20),
        capped,
        github=api.gh if api else None,
        off_authorized=off,
        bot_login="agent-apply[bot]",
    )


def decision():
    return {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "tier": 2,
        "answers": {
            "type": {"value": "type:bug", "p": 0.9},
            "area": {"value": "api", "p": 0.9},
            "priority": {"value": "P2", "p": 0.9},
            "needs_info": {"value": False, "p": 0.9},
        },
        "labels": ["type:bug", "area:api", "P2"],
        "suggestions": {"priority": None, "labels": []},
        "needs_info": None,
        "rca": None,
        "close": None,
        "escalate": None,
        "security": False,
        "notes": [],
    }


def complete_llm_answers(*, priority="P2", duplicate="none"):
    return {
        "type": {"value": "bug", "p": 0.99},
        "area": {"value": "api", "p": 0.99},
        "priority": {"value": priority, "p": 0.99},
        "needs_info": {"value": False, "p": 0.99},
        "out_of_scope": {"value": False, "p": 0.99},
        "docs_ref": {"value": "none", "p": 0.99},
        "duplicate_of": {"value": duplicate, "p": 0.99},
        "security_sensitive": {"value": False, "p": 0.99},
    }


def closing(kind="duplicate"):
    return {
        "kind": kind,
        "target": 99 if kind == "duplicate" else None,
        "docs_ref": "https://example.org/guide" if kind == "out_of_scope" else None,
        "p": 0.97,
        "lexical": 0.8,
        "tier2_agrees": True,
    }


def plan(data, ctx):
    from agent_repo.apply.triage import plan_triage

    return plan_triage(data, ctx)


def labels(actions):
    return {
        label
        for action in actions
        if action.kind == "add_labels"
        for label in action.payload["labels"]
    }


def bodies(actions):
    return "\n".join(action.payload["body"] for action in actions if action.kind == "comment")


def run(data, ctx, api, tmp_path, mode="live"):
    from agent_repo.apply.actions import execute

    summary = tmp_path / "summary.md"
    alerts = execute(plan(data, ctx), api.gh, mode, summary)
    return alerts, summary.read_text()


@pytest.mark.parametrize("maximum", [False, True], ids=["baseline", "schema-maximum"])
def test_clean_production_secret_set_posts_comment(maximum, tmp_path, record_property):
    from time import perf_counter

    from agent_repo.apply.actions import check_identity

    api = API()
    keys = ["sk-ant-api03-" + secrets.token_urlsafe(32) for _ in range(3)]
    keys.append("ghs_" + secrets.token_urlsafe(32))
    ctx = replace(context(api), secrets=keys)
    data = decision()
    prose = "Please provide the application version and steps to reproduce. " * 70
    data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    data["needs_info"] = {"text": prose[:2000]}
    if maximum:
        data["rca"] = {
            "hypothesis": prose[:4000],
            "locations": [],
            "confidence": "low",
            "confirm_with": prose[:1000],
        }
        data["notes"] = [prose[:500]] * 20
    assert not check_identity(data, ctx, "decision"), "test artifact must be valid"
    started = perf_counter()
    alerts, _ = run(data, ctx, api, tmp_path)
    elapsed = perf_counter() - started
    record_property("apply_seconds", elapsed)
    assert not alerts, "ordinary request raised an outbound scan alert"
    assert len(api.comments) == 1, "ordinary request comment was dropped"
    assert data["needs_info"]["text"] in api.comments[0]["body"]
    assert elapsed < 2, "ordinary request exceeded bounded runtime"


@pytest.mark.parametrize(
    "answer,values,label",
    [
        ("type", ("bug", "type:bug"), "type:bug"),
        ("area", ("api", "area:api"), "area:api"),
        ("priority", ("p2", "priority: P2"), "P2"),
        ("needs_info", (True,), "needs-info"),
    ],
)
@pytest.mark.parametrize("probability,allowed", [(0.8, True), (0.79, False)])
def test_labels_require_answer_threshold(answer, values, label, probability, allowed):
    for value in values:
        data = decision()
        data["answers"][answer] = {"value": value, "p": probability}
        actions = plan(data, context())
        assert not any(action.payload.get("alerts") for action in actions)
        assert (label in labels(actions)) is allowed
        assert ("needs-maintainer" in labels(actions)) is (not allowed)


@pytest.mark.parametrize("priority", ["P0", "P1"])
def test_high_priority_is_suggested(priority):
    data = decision()
    data["answers"]["priority"]["value"] = priority
    data["labels"].append(priority)
    actions = plan(data, context())
    assert priority not in labels(actions)
    assert "needs-maintainer" in labels(actions)
    assert priority in bodies(actions)


@pytest.mark.parametrize("priority", ["P0", "P1"])
def test_high_priority_only_in_suggested_labels_is_suggested(priority):
    from agent_repo.apply.actions import check_identity

    data = decision()
    data["suggestions"]["labels"] = [priority]
    ctx = context()
    assert not check_identity(data, ctx, "decision"), "test artifact must be valid"
    actions = plan(data, ctx)
    assert priority not in labels(actions)
    assert "needs-maintainer" in labels(actions)
    assert f"Suggested priority: {priority}." in bodies(actions)


@pytest.mark.parametrize("probability,posted", [(0.8, True), (0.79, False)])
def test_needs_info_threshold(probability, posted):
    data = decision()
    data["answers"]["needs_info"] = {"value": True, "p": probability}
    data["needs_info"] = {"text": "Please provide versions."}
    assert ("Please provide versions." in bodies(plan(data, context()))) is posted


def test_needs_info_with_unknown_priority_still_posts_request(tmp_path):
    api = API()
    data = decision()
    del data["answers"]["priority"]
    data["answers"]["needs_info"] = {"value": True, "p": 0.95}
    data["needs_info"] = {"text": "Please provide the version and a reproducer."}
    alerts, _ = run(data, context(api), api, tmp_path)
    assert not alerts
    assert set(api.labels) == {"type:bug", "area:api", "needs-info", "needs-maintainer"}
    assert len(api.comments) == 1
    assert data["needs_info"]["text"] in api.comments[0]["body"]


def test_needs_info_uses_charter_checklist():
    data = decision()
    data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    assert "Steps to reproduce" in bodies(plan(data, context()))


@pytest.mark.parametrize("prose", ["small", "maximum", "backticks"])
@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
def test_close_with_all_guards(kind, prose, tmp_path):
    from agent_repo.apply.actions import check_identity

    api = API()
    ctx = context(api, enabled=True)
    data = decision()
    data["close"] = closing(kind)
    data["close"]["p"] = ctx.config.raw["thresholds"]["close_min"]
    data["answers"].update(
        {
            "out_of_scope": {"value": kind == "out_of_scope", "p": 0.9},
            "docs_ref": {
                "value": "none" if prose == "small" else "https://example.org/guide",
                "p": 0.9,
            },
            "duplicate_of": {"value": "#99" if kind == "duplicate" else "none", "p": 0.9},
            "security_sensitive": {"value": False, "p": 0.9},
        }
    )
    if prose != "small":
        data["answers"]["needs_info"]["value"] = True
        data["needs_info"] = {"text": ("Please provide your version. " * 100)[:2000]}
        data["rca"] = {
            "hypothesis": ("The observed behavior suggests a failure. " * 200)[:4000],
            "locations": [
                {"path": "src/" + "component/" * 32 + f"handler{i}.py", "line": 1}
                for i in range(10)
            ],
            "confidence": "high",
            "confirm_with": ("Inspect the implementation and confirm. " * 100)[:1000],
        }
        if prose == "backticks":
            data["rca"]["hypothesis"] = "`" * 4000
    assert not check_identity(data, ctx, "decision"), "test artifact must be valid"
    alerts, _ = run(data, ctx, api, tmp_path)
    assert not alerts
    assert len(api.comments) == 1
    body = api.comments[0]["body"]
    assert len(body) <= 10000
    reference = "Duplicate of #99" if kind == "duplicate" else "https://example.org/guide"
    link = (
        "https://github.com/your-org/project/issues/99"
        if kind == "duplicate"
        else "https://example.org/guide"
    )
    assert (
        f"Closing: {reference}. If this is incorrect, ask a maintainer to reopen this issue."
    ) in body
    assert f"Reference: <{link}>" in body
    assert body.endswith("<!-- agent:triage run=run-1 -->")
    assert api.writes[-2] == (
        "PATCH",
        "issues/7",
        {"state": "closed", "state_reason": "not_planned"},
    )
    assert api.writes[-1] == ("POST", "issues/7/labels", {"labels": ["agent:closed"]})
    assert all("agent:closed" not in body.get("labels", []) for _, _, body in api.writes[:-1])
    assert any("reopen" in (body or {}).get("body", "").lower() for _, _, body in api.writes)


@pytest.mark.parametrize(
    "guard",
    [
        "disabled",
        "confidence",
        "lexical",
        "tier2",
        "closed_target",
        "no_api",
        "self",
        "docs",
        "target_pr",
        "missing_answer",
        "none_answer",
        "different_answer",
    ],
)
def test_close_rejects_missing_guard(guard, tmp_path):
    api = API(target_open=guard != "closed_target")
    ctx = context(None if guard == "no_api" else api, enabled=guard != "disabled")
    data = decision()
    data["close"] = closing("out_of_scope" if guard == "docs" else "duplicate")
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    if guard == "confidence":
        data["close"]["p"] = 0.91
    if guard == "lexical":
        data["close"]["lexical"] = 0.44
    if guard == "tier2":
        data["close"]["tier2_agrees"] = False
    if guard == "self":
        data["close"]["target"] = 7
        data["answers"]["duplicate_of"]["value"] = "#7"
    if guard == "missing_answer":
        del data["answers"]["duplicate_of"]
    if guard == "none_answer":
        data["answers"]["duplicate_of"]["value"] = "none"
    if guard == "different_answer":
        data["answers"]["duplicate_of"]["value"] = "#100"
    if guard == "docs":
        data["close"]["docs_ref"] = "https://example.org/unlisted"
    if guard == "target_pr":
        original = api.transport

        def transport(request, timeout):
            status, headers, body = original(request, timeout)
            value = json.loads(body)
            if request.full_url.endswith("issues/99"):
                value["pull_request"] = {}
            return status, headers, json.dumps(value).encode()

        api.gh = GitHub("test-token", "your-org/project", transport=transport)
        ctx = replace(ctx, github=api.gh)
    actions = plan(data, ctx)
    assert not any(action.kind == "close" for action in actions)
    assert "agent:closed" not in labels(actions)
    assert "needs-maintainer" in labels(actions)
    assert "suggest" in bodies(actions).lower()
    alerts, _ = run(data, ctx, api, tmp_path)
    assert not alerts
    assert api.issue_state == "open"
    assert api.comments and "Suggested closure" in api.comments[0]["body"]
    assert "needs-maintainer" in api.labels and "agent:closed" not in api.labels


def test_dry_run_has_no_writes_and_lists_actions(tmp_path):
    api = API()
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    _, summary = run(data, context(api, enabled=True), api, tmp_path, "dry-run")
    assert api.writes == []
    assert all(kind in summary for kind in ("add_labels", "comment", "close"))


def test_live_order_labels_comment_close(tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, context(api, enabled=True))
    shuffled = [
        next(action for action in actions if action.kind == kind)
        for kind in ("close", "comment", "add_labels")
    ]
    assert not execute(shuffled, api.gh, "live", tmp_path / "summary.md")
    assert [path.rsplit("/", 1)[-1] for _, path, _ in api.writes] == [
        "labels",
        "comments",
        "7",
        "labels",
    ]
    assert "agent:closed" not in api.writes[0][2]["labels"]
    assert api.writes[-1][2] == {"labels": ["agent:closed"]}


def test_missing_closure_explanation_blocks_all_normal_writes(tmp_path):
    from agent_repo.apply.actions import execute

    api = API(
        comments=[
            {"body": "Earlier comment.", "user": {"login": "agent-apply[bot]", "type": "Bot"}}
        ]
    )
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = [
        replace(action, payload={**action.payload, "comment_body": "Missing explanation."})
        if action.kind == "close"
        else action
        for action in plan(data, context(api, enabled=True))
    ]
    assert execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]


@pytest.mark.parametrize("route", ["normal", "priority", "security", "leak", "invalid"])
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("capped", [True, False])
def test_cap_label_is_trusted(route, allowed, capped):
    ctx = context(capped=capped)
    if not allowed:
        ctx = replace(
            ctx,
            config=replace(
                ctx.config,
                labels=tuple(
                    label for label in ctx.config.labels if label != "agent:triage-capped"
                ),
            ),
        )
    data = decision()
    data["labels"].append("agent:triage-capped")
    if route == "priority":
        data["answers"]["priority"]["value"] = "P0"
    elif route == "security":
        data["security"] = True
    elif route == "leak":
        data["notes"] = [ctx.canary]
    elif route == "invalid":
        data = None
    assert ("agent:triage-capped" in labels(plan(data, ctx))) is (
        capped and allowed and route != "invalid"
    )


@pytest.mark.parametrize("area,assigned", [("api", True), ("unknown", False)])
def test_maintainer_assigns_area_owners(area, assigned, tmp_path):
    data = decision()
    data["answers"]["area"]["value"] = area
    data["escalate"] = {"to": "maintainer", "reason": "uncertain"}
    api = API()
    run(data, context(api), api, tmp_path)
    assert any(body == {"assignees": ["octocat"]} for _, _, body in api.writes) is assigned


@pytest.mark.parametrize("route", ["priority", "needs-info"])
@pytest.mark.parametrize(
    "owners",
    [("@your-org/maintainers",), ("@your-org/maintainers", "@octocat")],
    ids=["team", "mixed"],
)
def test_team_owners_and_assignment_failure_preserve_comment(route, owners, tmp_path):
    api = API(fail="assignees")
    ctx = context(api)
    ctx = replace(
        ctx, config=replace(ctx.config, areas=(replace(ctx.config.areas[0], owners=owners),))
    )
    data = decision()
    if route == "priority":
        data["suggestions"]["priority"] = "P0"
        expected = "Suggested priority: P0."
    else:
        data["answers"]["needs_info"] = {"value": True, "p": 0.95}
        data["needs_info"] = {"text": "Please provide versions."}
        data["escalate"] = {"to": "maintainer", "reason": "uncertain"}
        expected = data["needs_info"]["text"]
    actions = plan(data, ctx)
    assignees = [
        owner
        for action in actions
        if action.kind == "assign"
        for owner in action.payload["assignees"]
    ]
    assert assignees == (["octocat"] if "@octocat" in owners else [])
    run(data, ctx, api, tmp_path)
    assert len(api.comments) == 1
    assert expected in api.comments[0]["body"]
    if assignees:
        paths = [path for _, path, _ in api.writes]
        assert paths.index("issues/7/comments") < paths.index("issues/7/assignees")
        assert sum("needs-maintainer" in body.get("labels", []) for _, _, body in api.writes) == 1


def test_area_label_requires_configured_area():
    data = decision()
    data["answers"]["area"] = {"value": "legacy", "p": 0.95}
    data["labels"].append("area:legacy")
    ctx = context()
    ctx = replace(ctx, config=replace(ctx.config, labels=(*ctx.config.labels, "area:legacy")))
    actions = plan(data, ctx)
    assert "area:legacy" not in labels(actions)
    assert labels(actions) == {"needs-maintainer"}
    assert any(
        "Artifact rejected: invalid schema." in action.payload.get("alerts", [])
        for action in actions
    )


@pytest.mark.parametrize("authorized", [True, False])
def test_off_requires_gate_authorization(authorized, tmp_path):
    api = API()
    ctx = context(api, off=authorized)
    ctx = replace(
        ctx,
        event=replace(ctx.event, name="issue_comment", comment_body="/agent off", sender="octocat"),
    )
    run(decision(), ctx, api, tmp_path)
    if authorized:
        assert len(api.writes) == 1
        assert api.writes[0][1] == "issues/7/comments"
        assert "<!-- agent:off run=run-1 -->" in api.writes[0][2]["body"]
    else:
        assert "agent:off" not in str(api.writes)


@pytest.mark.parametrize(
    "body,sender,bot,listed,expected",
    [
        ("/agent off\nthanks", "octocat", False, False, True),
        ("\n /agent\toff \nthanks", "octocat", False, False, True),
        ("/agent off", "OctoCat", False, True, True),
        ("thanks\n/agent off", "octocat", False, False, False),
        ("/agent off", "automation[BOT]", False, True, False),
        ("/agent off", "octocat", True, True, False),
        ("/agent off", "outsider", False, False, False),
    ],
    ids=[
        "trailing-text",
        "first-nonblank",
        "maintainer",
        "not-first",
        "bot-login",
        "bot-type",
        "outsider",
    ],
)
def test_off_rechecks_first_line_and_trusted_maintainers(
    body, sender, bot, listed, expected, tmp_path
):
    import base64

    api = API()
    original = api.transport
    refs = []

    def transport(request, timeout):
        path = urlparse(request.full_url).path
        if path.endswith("/contents/MAINTAINERS"):
            refs.append(urlparse(request.full_url).query)
            content = (
                f"# Trusted operators\n {sender.swapcase()} # on call\n"
                if listed
                else "someone-else\n"
            )
            value = {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
            return 200, {}, json.dumps(value).encode()
        if path.endswith("/permission") and (listed or sender == "outsider"):
            return 404, {}, b"{}"
        return original(request, timeout)

    api.gh = GitHub("test-token", "your-org/project", transport=transport)
    ctx = context(api, off=True)
    ctx = replace(
        ctx,
        event=replace(
            ctx.event, name="issue_comment", comment_body=body, sender=sender, sender_is_bot=bot
        ),
    )
    run(decision(), ctx, api, tmp_path)
    if expected:
        assert len(api.writes) == 1 and api.writes[0][1] == "issues/7/comments"
        assert "<!-- agent:off run=run-1 -->" in api.writes[0][2]["body"]
        if listed:
            assert refs == ["ref=" + ctx.base_sha]
    else:
        assert not api.comments
        assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]
    if refs:
        assert all(ref == "ref=" + ctx.base_sha for ref in refs)


def test_live_comments_have_trusted_marker(tmp_path):
    api = API()
    data = decision()
    data["rca"] = {
        "hypothesis": "Possible parsing bug",
        "locations": [{"path": "src/a.py", "line": 3}],
        "confidence": "low",
        "confirm_with": "Run repro",
    }
    run(data, context(api), api, tmp_path)
    comments = [body["body"] for _, path, body in api.writes if path.endswith("comments")]
    assert comments and all("<!-- agent:triage run=run-1 -->" in body for body in comments)
    assert "src/a.py:3" in comments[0]


def test_idempotent_live_state_makes_no_writes(tmp_path):
    data = decision()
    data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    data["needs_info"] = {"text": "Please supply versions."}
    api = API(labels=("type:bug", "area:api", "P2", "needs-info"))
    ctx = context(api)
    body = next(action.payload["body"] for action in plan(data, ctx) if action.kind == "comment")
    api.comments.append({"body": body, "user": {"login": "agent-apply[bot]", "type": "Bot"}})
    run(data, ctx, api, tmp_path)
    assert api.writes == []


@pytest.mark.parametrize("bad", [None, [], {"extra": "field"}, "identity", "answer"])
@pytest.mark.parametrize("capped", [False, True])
def test_invalid_artifact_only_maintainer(bad, capped, tmp_path):
    api = API()
    if bad == "identity":
        bad = decision()
        bad["run_id"] = "another-run"
    elif bad == "answer":
        bad = decision()
        bad["answers"]["type"]["value"] = True
    alerts, summary = run(bad, context(api, capped=capped), api, tmp_path)
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]
    assert alerts and "reject" in summary.lower()
    assert "agent:triage-capped" not in summary


@pytest.mark.parametrize(
    "question,value,with_close",
    [
        (question, value, True)
        for question in ("type", "area", "priority", "docs_ref", "duplicate_of")
        for value in (True, 1, None, "unlisted")
    ]
    + [
        (question, value, True)
        for question in ("needs_info", "out_of_scope", "security_sensitive")
        for value in ("yes", 1, None)
    ]
    + [("priority", "", True)]
    + [("duplicate_of", value, True) for value in (7, "7", "#0")]
    + [("duplicate_of", value, False) for value in ("99", "#0", "#x", "")],
)
def test_malformed_answer_cannot_authorize_close(question, value, with_close, tmp_path):
    from agent_repo.apply.actions import check_identity

    api = API()
    ctx = context(api, enabled=True)
    data = decision()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    data["answers"][question] = {"value": value, "p": 0.9}
    data["close"] = closing() if with_close else None
    assert not check_identity(data, ctx, "decision"), "generic schema should accept this value"
    alerts, summary = run(data, ctx, api, tmp_path)
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]
    assert alerts and "Artifact rejected: invalid schema." in summary
    assert not api.comments and api.issue_state == "open"


def test_nonfinite_probability_rejected(tmp_path):
    api = API()
    data = decision()
    data["answers"]["priority"]["p"] = math.nan
    run(data, context(api), api, tmp_path)
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]


@pytest.mark.parametrize("security", [True, False])
def test_security_routes_to_maintainer(security):
    data = decision()
    data["security"] = security
    assert ("needs-maintainer" in labels(plan(data, context()))) is security


def test_read_failure_stops_normal_writes(tmp_path):
    api = API(fail="issues/7")
    alerts, summary = run(decision(), context(api), api, tmp_path)
    assert all(body == {"labels": ["needs-maintainer"]} for _, _, body in api.writes)
    assert alerts and "fail" in summary.lower()


def test_invalid_mode_never_writes(tmp_path):
    api = API()
    alerts, _ = run(decision(), context(api), api, tmp_path, "LIVE")
    assert not api.writes
    assert alerts


def test_mixed_targets_never_write(tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, context(api, enabled=True))
    actions = [
        replace(action, payload={**action.payload, "number": 8})
        if action.kind == "close"
        else action
        for action in actions
    ]
    alerts = execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not api.writes
    assert any(alert.startswith("Apply failed") for alert in alerts)


@pytest.mark.parametrize(
    "scenario",
    [
        "scan-key",
        "token-literal",
        "token-base64",
        "moved-head",
        "mismatched-sha",
        "deep-json",
        "memory-json",
        "oversized-json",
        "planner-error",
        "labels-error",
        "empty-scan-keys",
        "whitespace-scan-keys",
        "empty-canary",
        "event-pull_request",
        "event-pull_request_target",
        "event-workflow_run",
        "event-push",
        "event-pull_request_review",
        "event-comment-pr",
    ],
)
def test_cli_reads_default_branch_policy_and_scan_keys(scenario, monkeypatch, tmp_path):
    import base64

    from agent_repo.commands.apply import main

    api = API(fail="labels" if scenario == "labels-error" else None)
    key = secrets.token_hex(20)
    token = secrets.token_urlsafe(32)
    data = decision()
    data["answers"]["area"]["value"] = "core"
    data["labels"] = ["area:core" if label == "area:api" else label for label in data["labels"]]
    data["answers"]["needs_info"] = {"value": True, "p": 0.99}
    text = {
        "scan-key": base64.b64encode(key.encode()).decode(),
        "token-literal": token,
        "token-base64": base64.b64encode(token.encode()).decode(),
        "moved-head": "Please supply versions.",
        "mismatched-sha": "Please supply versions.",
    }.get(scenario, "Please supply versions.")
    data["needs_info"] = {"text": text}
    if scenario == "mismatched-sha":
        data["base_sha"] = "b" * 40
    artifact = tmp_path / "decision.json"
    artifact.write_text(
        "[" * 200_000 + "]" * 200_000 if scenario == "deep-json" else json.dumps(data)
    )
    if scenario == "oversized-json":
        artifact.write_text(json.dumps(data) + " " * 1_000_001)
    if scenario in {"memory-json", "oversized-json"}:
        original_loads = json.loads
        artifact_text = artifact.read_text()
        artifact_parse_calls = []

        def loads(text, *args, **kwargs):
            if text == artifact_text:
                artifact_parse_calls.append(True)
                if scenario == "memory-json":
                    raise MemoryError
            return original_loads(text, *args, **kwargs)

        monkeypatch.setattr("agent_repo.commands.apply.json.loads", loads)
    event_name = "issues"
    event_path = Path("tests/fixtures/events/issues.opened.json").resolve()
    if scenario.startswith("event-"):
        event_name = scenario.removeprefix("event-")
        payload = json.loads(event_path.read_text())
        payload["pull_request"] = payload["issue"].copy()
        if event_name == "comment-pr":
            event_name = "issue_comment"
            payload["issue"]["pull_request"] = {}
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(payload))
    summary = tmp_path / "summary.md"
    original = api.transport
    policy_refs = []

    def transport(request, timeout):
        path = urlparse(request.full_url).path
        if "/contents/.agents/" in path:
            policy_refs.append(urlparse(request.full_url).query)
            name = path.rsplit("/", 1)[-1]
            source = (
                Path("tests/fixtures/charter/example.md")
                if name == "charter.md"
                else Path("tests/fixtures/agents") / name
            )
            content = source.read_text()
            if name == "config.yml":
                content = content.replace("triage: dry-run", "triage: live")
            value = {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
        else:
            return original(request, timeout)
        return 200, {}, json.dumps(value).encode()

    api.gh = GitHub(token, "your-org/project", transport=transport)
    monkeypatch.setattr("agent_repo.commands.apply.GitHub", lambda token, repo: api.gh)
    for name, value in {
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_RUN_ID": "run-1",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": token,
        "AGENT_BOT_LOGIN": "agent-apply[bot]",
        "AGENT_SCAN_KEYS": {
            "empty-scan-keys": "",
            "whitespace-scan-keys": "\n  \n",
        }.get(scenario, "\n" + key + "\n"),
        "AGENT_CANARY": "" if scenario == "empty-canary" else secrets.token_hex(20),
    }.items():
        monkeypatch.setenv(name, value)
    if scenario in {"empty-scan-keys", "whitespace-scan-keys", "empty-canary"}:
        assert main([str(artifact)]) == 1
        assert not api.calls, "empty scan configuration reached the GitHub API"
        assert "Apply failed" in summary.read_text()
        assert key not in summary.read_text() and token not in summary.read_text()
        return
    if scenario == "planner-error":

        def fail_plan(*args):
            raise RuntimeError("planner unavailable")

        monkeypatch.setattr("agent_repo.commands.apply.plan_triage", fail_plan)
    if scenario.startswith("event-"):
        assert main([str(artifact)]) == 1
        assert not api.calls, "unsupported event reached the GitHub API"
        return
    fails_apply = scenario in {
        "planner-error",
        "labels-error",
        "scan-key",
        "token-literal",
        "token-base64",
    }
    assert main([str(artifact)]) == int(fails_apply)
    if scenario == "oversized-json":
        assert not artifact_parse_calls, "oversized artifact reached JSON parsing"
    if scenario == "planner-error":
        assert api.writes == [("POST", "issues/12/labels", {"labels": ["needs-maintainer"]})]
        assert "Apply failed: trusted inputs unavailable." in summary.read_text()
    elif scenario == "labels-error":
        assert api.writes and all(path.endswith("/labels") for _, path, _ in api.writes)
        assert "Apply failed: GitHub action stopped" in summary.read_text()
    elif scenario == "moved-head":
        assert len(api.comments) == 1, "workflow-pinned artifact was rejected after a push"
        assert "ALERT" not in summary.read_text()
        boundary = len(api.calls)
        assert main([str(artifact)]) == 0
        assert not [call for call in api.calls[boundary:] if call[0] != "GET"]
    else:
        assert not api.comments, "rejected proposal posted a comment"
        assert all(
            method == "POST" and body == {"labels": ["needs-maintainer"]}
            for method, _, body in api.writes
        )
        assert api.writes, "maintainer fallback was omitted"
        expected_alert = (
            "invalid schema"
            if scenario in {"deep-json", "memory-json", "oversized-json"}
            else "identity mismatch"
            if scenario == "mismatched-sha"
            else "Outbound scan"
        )
        assert expected_alert in summary.read_text()
    assert policy_refs and all(query == "ref=" + "a" * 40 for query in policy_refs)
    assert key not in summary.read_text(), "credential echoed"
    assert token not in summary.read_text(), "installation token echoed"
    if scenario in {"moved-head", "planner-error", "labels-error"}:
        return
    if scenario in {"memory-json", "oversized-json"}:
        monkeypatch.setattr("agent_repo.commands.apply.json.loads", original_loads)
    data["base_sha"] = "a" * 40
    data["needs_info"]["text"] = "Please supply versions."
    artifact.write_text(json.dumps(data))
    assert main([str(artifact)]) == 0
    assert len(api.comments) == 1
    boundary = len(api.calls)
    assert main([str(artifact)]) == 0
    assert not [call for call in api.calls[boundary:] if call[0] != "GET"]


def test_cli_missing_canary_fails_without_writes(monkeypatch, tmp_path, capsys):
    from agent_repo.commands.apply import main

    monkeypatch.delenv("AGENT_CANARY", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert main([str(tmp_path / "missing.json")]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_cli_outbound_scan_alert_escalates_and_fails(tmp_path, monkeypatch):
    from agent_repo.commands.apply import main

    api = API()
    ctx = context(api)
    data = decision()
    data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    data["needs_info"] = {"text": ctx.canary}
    artifact = tmp_path / "decision.json"
    artifact.write_text(json.dumps(data))
    summary = tmp_path / "summary.md"

    monkeypatch.setattr("agent_repo.commands.apply.GitHub", lambda token, repo: api.gh)
    monkeypatch.setattr(
        "agent_repo.commands.apply._policy",
        lambda gh, sha, directory: (ctx.base_sha, ctx.config, ctx.charter),
    )
    for name, value in {
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(Path("tests/fixtures/events/issues.opened.json").resolve()),
        "GITHUB_RUN_ID": ctx.run_id,
        "GITHUB_SHA": ctx.base_sha,
        "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": "test-token",
        "AGENT_BOT_LOGIN": "agent-apply[bot]",
        "AGENT_SCAN_KEYS": "\n".join(ctx.secrets),
        "AGENT_CANARY": ctx.canary,
    }.items():
        monkeypatch.setenv(name, value)

    exit_code = main([str(artifact)])

    assert any(
        path.endswith("/labels") and body == {"labels": ["needs-maintainer"]}
        for _, path, body in api.writes
    )
    assert "Outbound scan alert" in summary.read_text()
    assert exit_code == 1


@pytest.mark.parametrize(
    "modules",
    [("json.py",), ("agent_repo_cli.py",), ("json.py", "agent_repo_cli.py")],
    ids=["stdlib", "entrypoint", "both"],
)
def test_apply_action_ignores_workspace_python_modules(modules, tmp_path):
    from agent_repo.yamljson import load_yaml

    action_path = Path(__file__).resolve().parents[1] / "actions/apply"
    step = load_yaml(action_path / "action.yml")["runs"]["steps"][0]
    sentinel = tmp_path / "shadow-imported"
    for module in modules:
        (tmp_path / module).write_text(
            'from pathlib import Path\nPath("shadow-imported").touch()\nraise SystemExit(0)\n'
        )
    artifact = tmp_path / "decision.json"
    artifact.write_text(json.dumps(decision()))
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": step["env"]["PYTHONPATH"].replace(
                "${{ github.action_path }}", str(action_path)
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GITHUB_ACTION_PATH": str(action_path),
            "AGENT_ARTIFACT": str(artifact),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert not sentinel.exists(), "workspace Python module executed"
    # Missing trusted configuration reaches apply's fail-closed path without any API call.
    assert result.returncode == 1
    assert result.stderr.strip() == "Apply failed: trusted inputs unavailable."
    assert "Apply failed: trusted inputs unavailable." in summary.read_text()


def _python_uses_script(run):
    run = run.replace("${{ github.action_path }}", "$GITHUB_ACTION_PATH")
    for line in run.replace("\\\n", "").splitlines():
        words = shlex.split(line, comments=True)
        for index, word in enumerate(words):
            if not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(word).name):
                continue
            if index and words[index - 1] in {"-x", "--python"}:
                continue
            options = iter(words[index + 1 :])
            for option in options:
                if option == "--":
                    option = next(options, "")
                if not option.startswith("-") or option == "-":
                    if not option.startswith(("$GITHUB_ACTION_PATH/", "${GITHUB_ACTION_PATH}/")):
                        return False
                    break
                if option == "--check-hash-based-pycs":
                    next(options, None)
                    continue
                if option.startswith("--"):
                    continue
                for position, flag in enumerate(option[1:], 1):
                    if flag in {"m", "c"}:
                        return False
                    if flag in {"X", "W"}:
                        # Interpreter option values may be attached or in the next word.
                        if position == len(option) - 1:
                            next(options, None)
                        break
            else:
                return False
    return True


@pytest.mark.parametrize(
    "command,expected",
    [
        (None, True),
        ("python3 -X dev -m agent_repo_cli", False),
        ("python3 -W ignore -m agent_repo_cli", False),
        ("python3 -X dev -Bm agent_repo_cli", False),
        ('python3 "$GITHUB_WORKSPACE/configure.py"', False),
        ("python3 configure.py", False),
        ("python3 - <<EOF", False),
        ("python3 <<EOF", False),
        ("python3", False),
        ('python3 "${{ github.action_path }}/run.py"', True),
        ('python3 -P "$GITHUB_ACTION_PATH/run.py"', True),
        ('python3 "$GITHUB_ACTION_PATH/run.py" \\\n  --flag value', True),
        ("python3 \\\n  -m agent_repo.cli", False),
    ],
    ids=[
        "real-actions",
        "x-option",
        "warnings-option",
        "clustered",
        "workspace-script",
        "relative-script",
        "stdin-script",
        "heredoc",
        "no-script",
        "action-path-expression",
        "safe-path-option",
        "continued-script",
        "continued-module",
    ],
)
def test_composite_actions_do_not_use_python_module_or_command_mode(command, expected):
    from agent_repo.yamljson import load_yaml

    actions = Path(__file__).resolve().parents[1] / "actions"
    for path in sorted(actions.rglob("action.y*ml")):
        runs = load_yaml(path)["runs"]
        if runs["using"] != "composite":
            continue
        for step in runs["steps"]:
            run = step.get("run", "") if command is None else command
            for name, value in step.get("env", {}).items():
                if value == "${{ github.action_path }}":
                    run = run.replace(f"${name}/", "$GITHUB_ACTION_PATH/")
            assert _python_uses_script(run) is expected, (
                f"{path.relative_to(actions)}: unexpected Python invocation policy result"
            )


@pytest.mark.parametrize("omitted", [("area:api", "P2"), ("agent:closed",)])
def test_label_absent_from_allowlist_is_dropped(omitted):
    api = API()
    ctx = context(api, enabled=True)
    ctx = replace(
        ctx,
        config=replace(
            ctx.config, labels=tuple(label for label in ctx.config.labels if label not in omitted)
        ),
    )
    data = decision()
    if "agent:closed" in omitted:
        data["close"] = closing()
        data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, ctx)
    assert not any(action.payload.get("alerts") for action in actions)
    assert not labels(actions) & set(omitted)
    assert not any(action.kind == "close" for action in actions)
    if data["close"] is None:
        assert labels(actions) == {"type:bug"}
    else:
        assert "needs-maintainer" in labels(actions)
        assert "Suggested closure" in bodies(actions)


def test_labels_not_requested_are_not_added():
    data = decision()
    data["labels"] = []
    assert not labels(plan(data, context())) & {"type:bug", "area:api", "P2"}


@pytest.mark.parametrize("target", ["closed", "pull_request"])
def test_duplicate_state_rechecked_before_any_write(target, tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, context(api, enabled=True))
    if target == "closed":
        api.target_open = False
    else:
        original = api.transport

        def transport(request, timeout):
            status, headers, body = original(request, timeout)
            if request.full_url.endswith("/issues/99"):
                value = json.loads(body)
                value["pull_request"] = {}
                body = json.dumps(value).encode()
            return status, headers, body

        api.gh = GitHub("test-token", "your-org/project", transport=transport)
    alerts = execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]
    assert alerts


@pytest.mark.parametrize("failed_read", ["issues/7", "comments"])
def test_execution_read_error_cannot_bypass_kill_switch(failed_read, tmp_path):
    api = API(fail=failed_read)
    alerts, _ = run(decision(), context(api), api, tmp_path)
    assert not api.writes
    assert alerts


@pytest.mark.parametrize(
    "security,sensitive", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("probability", [0, 0.99])
def test_security_cannot_close_or_apply_normal_labels(security, sensitive, probability, tmp_path):
    api = API()
    data = decision()
    data["security"] = security
    data["answers"]["security_sensitive"] = {"value": sensitive, "p": probability}
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, context(api, enabled=True))
    signal = security or sensitive
    if signal:
        assert labels(actions) == {"needs-maintainer"}
        assert "SECURITY.md" in bodies(actions)
    else:
        assert labels(actions) == {"type:bug", "area:api", "P2", "agent:closed"}
        assert "SECURITY.md" not in bodies(actions)
    assert any(action.kind == "close" for action in actions) is (not signal)
    alerts, _ = run(data, context(api, enabled=True), api, tmp_path)
    assert not alerts
    assert (api.issue_state == "closed") is (not signal)
    assert ("agent:closed" in api.labels) is (not signal)


@pytest.mark.parametrize("status", [404, 410])
def test_missing_duplicate_target_preserves_labels_and_suggestion(status, tmp_path):
    api = API()

    def transport(request, timeout):
        response = api.transport(request, timeout)
        if request.method == "GET" and request.full_url.endswith("/issues/99"):
            return status, {}, b"{}"
        return response

    api.gh = GitHub("test-token", "your-org/project", transport=transport)
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    alerts, _ = run(data, context(api, enabled=True), api, tmp_path)
    assert not alerts
    assert set(api.labels) == {"type:bug", "area:api", "P2", "needs-maintainer"}
    assert api.assignees == ["octocat"]
    assert len(api.comments) == 1
    assert "Suggested closure requires maintainer confirmation." in api.comments[0]["body"]
    assert api.issue_state == "open"
    assert all(method == "POST" and path.startswith("issues/7/") for method, path, _ in api.writes)


def test_duplicate_lookup_error_fails_closed(tmp_path):
    api = API(fail="issues/99")
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    alerts, _ = run(data, context(api, enabled=True), api, tmp_path)
    assert api.writes == [
        ("POST", "issues/7/labels", {"labels": ["needs-maintainer"]}),
        ("POST", "issues/7/assignees", {"assignees": ["octocat"]}),
    ]
    assert alerts


def test_cli_missing_scan_configuration_alerts_summary(monkeypatch, tmp_path):
    from agent_repo.commands.apply import main

    monkeypatch.delenv("AGENT_SCAN_KEYS", raising=False)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main([str(tmp_path / "missing.json")]) == 1
    assert "ALERT" in summary.read_text()


def test_duplicate_comment_contains_clickable_trusted_link():
    api = API()
    ctx = context(api, enabled=True)
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    text = bodies(plan(data, ctx))
    assert "<https://github.com/your-org/project/issues/99>" in text
    assert text.index("<https://") > text.rindex("```")


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
def test_summary_describes_actions_without_model_text(mode, kind, tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    data = decision()
    data["close"] = closing(kind)
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    data["escalate"] = {"to": "maintainer", "reason": "uncertain"}
    data["rca"] = {
        "hypothesis": "MODEL_DIAGNOSTIC",
        "locations": [],
        "confidence": "low",
        "confirm_with": "MODEL_CONFIRMATION",
    }
    actions = plan(data, context(api, enabled=True))
    summary_path = tmp_path / "summary.md"
    assert execute(actions, api.gh, mode, summary_path) == []
    summary = summary_path.read_text()
    assert '"labels": ["type:bug", "area:api", "P2", "agent:closed"]' in summary
    assert '"labels": ["needs-maintainer"]' in summary
    assert '"assignees": ["octocat"]' in summary
    assert f'"kind": "{kind}"' in summary
    assert '"target": 7' in next(line for line in summary.splitlines() if "Planned: close" in line)
    if kind == "duplicate":
        assert '"duplicate_target": 99' in summary
    comment = next(action.payload["body"] for action in actions if action.kind == "comment")
    assert f'"body_chars": {len(comment)}' in summary
    assert "MODEL_DIAGNOSTIC" not in summary and "MODEL_CONFIRMATION" not in summary
    assert "<!--" not in summary
    if mode == "dry-run":
        assert api.writes == []


@pytest.mark.parametrize("route", ["escalation", "security"])
@pytest.mark.parametrize("apply_min", [0.8, 0.95])
def test_maintainer_owner_assignment_requires_decided_area(route, apply_min):
    ctx = context()
    ctx.config.raw["thresholds"]["apply_min"] = apply_min
    data = decision()
    if route == "security":
        data["security"] = True
    else:
        data["escalate"] = {"to": "maintainer", "reason": "uncertain"}
    data["answers"]["area"]["p"] = apply_min - 0.01
    actions = plan(data, ctx)
    assert "needs-maintainer" in labels(actions)
    assert not any(action.kind == "assign" for action in actions)

    data["answers"]["area"]["p"] = apply_min
    actions = plan(data, ctx)
    assert "needs-maintainer" in labels(actions)
    assert [action.payload for action in actions if action.kind == "assign"] == [
        {"number": 7, "assignees": ["octocat"]}
    ]


@pytest.mark.parametrize("failure", ["close", "closed-label"])
def test_retry_after_comment_then_failed_close_does_not_repost(failure, tmp_path):
    api = API()
    original = api.transport
    failed = False

    def transport(request, timeout):
        nonlocal failed
        body = json.loads(request.data) if request.data else None
        is_close = request.method == "PATCH"
        is_closed_label = request.method == "POST" and body == {"labels": ["agent:closed"]}
        if not failed and (is_close if failure == "close" else is_closed_label):
            failed = True
            api.calls.append(
                (request.method, urlparse(request.full_url).path.split("/project/")[1], body)
            )
            return 500, {}, b"{}"
        return original(request, timeout)

    api.gh = GitHub("test-token", "your-org/project", transport=transport)
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    ctx = context(api, enabled=True)
    alerts, _ = run(data, ctx, api, tmp_path)
    assert alerts and api.issue_state == ("open" if failure == "close" else "closed")
    assert "needs-maintainer" in api.labels and "agent:closed" not in api.labels
    if failure == "close":
        assert not any("agent:closed" in body.get("labels", []) for _, _, body in api.writes)
    assert [path.rsplit("/", 1)[-1] for _, path, _ in api.writes[:3]] == ["labels", "comments", "7"]
    boundary = len(api.calls)
    alerts, _ = run(data, ctx, api, tmp_path)
    assert not alerts and api.issue_state == "closed"
    expected = [("POST", "issues/7/labels", {"labels": ["agent:closed"]})]
    if failure == "close":
        expected.insert(
            0, ("PATCH", "issues/7", {"state": "closed", "state_reason": "not_planned"})
        )
    assert [call for call in api.calls[boundary:] if call[0] != "GET"] == expected
    assert "agent:closed" in api.labels
    assert len(api.comments) == 1
    boundary = len(api.calls)
    run(data, ctx, api, tmp_path)
    assert not [call for call in api.calls[boundary:] if call[0] != "GET"]


@pytest.mark.parametrize(
    "closed_by,trusted",
    [
        ("open", True),
        ({"login": "human", "type": "User"}, False),
        ("missing", False),
        (None, False),
        ({"login": "agent-apply[bot]", "type": "Bot"}, True),
        ({"login": "AGENT-APPLY[bot]", "type": "Bot"}, True),
        ({"login": "agent-apply", "type": "User"}, False),
        ({"login": "agent-apply[bot]"}, False),
    ],
    ids=[
        "open",
        "human",
        "missing",
        "null",
        "agent-apply[bot]",
        "AGENT-APPLY[bot]",
        "lookalike-user",
        "missing-type",
    ],
)
@pytest.mark.parametrize("kind", ["duplicate", "out_of_scope"])
@pytest.mark.parametrize("closed_at", [1, 2], ids=["already-closed", "closed-before-write"])
def test_close_uses_existing_trusted_explanation(closed_by, trusted, kind, closed_at, tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    data = decision()
    data["close"] = closing(kind)
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, context(api, enabled=True))
    body = next(action.payload["body"] for action in actions if action.kind == "comment")
    if trusted or closed_by == {"login": "agent-apply", "type": "User"}:
        api.comments.append({"body": body, "user": {"login": "agent-apply[bot]", "type": "Bot"}})
        actions = [action for action in actions if action.kind != "comment"]
    original = api.transport
    issue_reads = 0

    def transport(request, timeout):
        nonlocal issue_reads
        if request.method == "GET" and request.full_url.endswith("/issues/7"):
            issue_reads += 1
            if issue_reads >= closed_at and closed_by != "open":
                api.issue_state = "closed"
                api.closed_by = None if closed_by == "missing" else closed_by
            status, headers, response = original(request, timeout)
            value = json.loads(response)
            if closed_by == "missing":
                value.pop("closed_by", None)
            return status, headers, json.dumps(value).encode()
        return original(request, timeout)

    api.gh = GitHub("test-token", "your-org/project", transport=transport)
    alerts = execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not alerts
    assert api.issue_state == "closed"
    assert ("agent:closed" in api.labels) is trusted
    assert ("needs-maintainer" in api.labels) is (not trusted)
    assert {"type:bug", "area:api", "P2"} <= set(api.labels)
    assert any(method == "PATCH" for method, _, _ in api.writes) is (closed_by == "open")
    posted = [payload["body"] for _, path, payload in api.writes if path.endswith("/comments")]
    if trusted:
        assert not posted
    else:
        assert len(posted) == 1
        assert "Suggested closure requires maintainer confirmation." in posted[0]
        assert "Closing:" not in posted[0] and "Reference:" not in posted[0]
        assert posted[0].endswith("<!-- agent:triage run=run-1 -->")
        assert api.assignees == ["octocat"]
    boundary = len(api.calls)
    assert not execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not [call for call in api.calls[boundary:] if call[0] != "GET"]


@pytest.mark.parametrize("role", ["read", "triage", "none", None])
def test_off_rejects_nonwrite_roles(role, tmp_path):
    api = API()
    original = api.transport

    def transport(request, timeout):
        if request.full_url.endswith("/permission"):
            return 200, {}, json.dumps({"role_name": role} if role is not None else {}).encode()
        return original(request, timeout)

    api.gh = GitHub("test-token", "your-org/project", transport=transport)
    ctx = context(api, off=True)
    ctx = replace(ctx, event=replace(ctx.event, name="issue_comment", comment_body="/agent off"))
    run(decision(), ctx, api, tmp_path)
    assert api.writes == [("POST", "issues/7/labels", {"labels": ["needs-maintainer"]})]


@pytest.mark.parametrize("source", ["charter", "security_template"])
def test_rendered_only_leak_is_rejected(source, tmp_path):
    api = API()
    ctx = context(api)
    data = decision()
    if source == "charter":
        ctx = replace(ctx, charter=replace(ctx.charter, good_report=(ctx.canary,)))
        data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    else:
        ctx = replace(ctx, canary="Agent triage")
        data["security"] = True
    alerts, summary = run(data, ctx, api, tmp_path)
    assert alerts and "alert" in summary.lower()
    assert not any(path.endswith("/comments") for _, path, _ in api.writes)
    assert ctx.canary not in summary, "canary echoed"


@pytest.mark.parametrize("login", ["AGENT-APPLY[bot]", "agent-apply", "Agent-Apply"])
@pytest.mark.parametrize("close_only", [False, True])
def test_bot_identity_is_canonical_for_retry_and_close(login, close_only, tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    ctx = replace(context(api, enabled=True), bot_login=login)
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, ctx)
    body = next(action.payload["body"] for action in actions if action.kind == "comment")
    api.comments.append({"body": body, "user": {"login": "agent-apply[bot]", "type": "Bot"}})
    api.labels = list(labels(actions))
    if close_only:
        actions = [action for action in actions if action.kind != "comment"]
    assert not execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert api.issue_state == "closed"
    assert not any(path.endswith("/comments") for _, path, _ in api.writes)
    boundary = len(api.calls)
    assert not execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not [call for call in api.calls[boundary:] if call[0] != "GET"]


@pytest.mark.parametrize("existing", [False, True])
def test_empty_bot_identity_cannot_explain_close(existing, tmp_path):
    from agent_repo.apply.actions import execute

    api = API()
    ctx = replace(context(api, enabled=True), bot_login="")
    data = decision()
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}
    actions = plan(data, ctx)
    if existing:
        body = next(action.payload["body"] for action in actions if action.kind == "comment")
        api.comments.append({"body": body, "user": {"login": "agent-apply[bot]", "type": "Bot"}})
    assert execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not any(
        path.endswith("/comments") or method == "PATCH" for method, path, _ in api.writes
    )


def test_empty_bot_identity_cannot_ignore_off_marker(tmp_path):
    from agent_repo.apply.actions import Action, execute

    api = API(
        comments=[
            {
                "body": "<!-- agent:off run=prior -->",
                "user": {"login": "agent-apply[bot]", "type": "Bot"},
            }
        ]
    )
    actions = [Action("add_labels", {"number": 7, "labels": ["type:bug"], "bot_login": ""})]
    alerts = execute(actions, api.gh, "live", tmp_path / "summary.md")
    assert not api.writes
    assert alerts


@pytest.mark.parametrize("login", ["", "   ", "wrong/login"])
def test_cli_rejects_missing_or_invalid_bot_identity(login, monkeypatch, tmp_path, capsys):
    from agent_repo.commands.apply import main

    api = API()
    ctx = context(api, enabled=True)
    monkeypatch.setattr("agent_repo.commands.apply.GitHub", lambda token, repo: api.gh)
    monkeypatch.setattr(
        "agent_repo.commands.apply._policy", lambda *args: (ctx.base_sha, ctx.config, ctx.charter)
    )
    data = decision()
    data["answers"]["needs_info"] = {"value": True, "p": 0.9}
    data["needs_info"] = {"text": "Please supply versions."}
    artifact = tmp_path / "decision.json"
    artifact.write_text(json.dumps(data))
    for name, value in {
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_EVENT_PATH": str(Path("tests/fixtures/events/issues.opened.json").resolve()),
        "GITHUB_RUN_ID": "run-1",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GH_TOKEN": "test-token",
        "AGENT_CANARY": ctx.canary,
        "AGENT_SCAN_KEYS": "\n".join(ctx.secrets),
        "AGENT_BOT_LOGIN": login,
    }.items():
        monkeypatch.setenv(name, value)
    assert main([str(artifact)]) == 1
    assert not api.writes
    assert "Traceback" not in capsys.readouterr().err


def test_llm_only_suppresses_close_even_when_enabled(tmp_path):
    api = API()
    ctx = context(api, enabled=True)
    config_raw = dict(ctx.config.raw)
    config_raw.pop("system_one")
    ctx = replace(ctx, config=replace(ctx.config, raw=config_raw))
    data = decision()
    data["mode"] = "llm-only"
    data["close"] = closing()
    data["answers"]["duplicate_of"] = {"value": "#99", "p": 0.9}

    actions = plan(data, ctx)
    assert "needs-maintainer" in labels(actions)
    assert "Suggested closure requires maintainer confirmation." in bodies(actions)
    assert not any(action.kind == "close" for action in actions)

    run(data, ctx, api, tmp_path)
    assert api.issue_state == "open"
    assert not any(method == "PATCH" for method, _, _ in api.writes)


def test_llm_only_p1_remains_suggestion_only():
    from agent_repo.commands.triage_merge import merge

    ctx = context(enabled=True)
    config_raw = dict(ctx.config.raw)
    config_raw["system_one"] = None
    ctx = replace(ctx, config=replace(ctx.config, raw=config_raw))
    tier1 = decision()
    tier1.update(mode="llm-only", answers={}, labels=[], close=None)
    tier2 = decision()
    tier2.update(
        tier=2,
        mode="llm-only",
        answers=complete_llm_answers(priority="P1"),
        labels=["type:bug", "area:api", "P1"],
        suggestions={"priority": "P1", "labels": []},
    )
    data = merge(tier1, tier2, None, allowed_labels={"type:bug", "area:api"})

    actions = plan(data, ctx)

    assert labels(actions) == {"needs-maintainer"}
    assert "Suggested priority: P1." in bodies(actions)

    partial = dict(tier2)
    partial["answers"] = dict(tier2["answers"])
    partial["answers"].pop("security_sensitive")
    failed = merge(tier1, partial, None, allowed_labels={"type:bug", "area:api"})
    assert failed["escalate"]["to"] == "maintainer"
    assert failed["answers"] == {}
    assert failed["suggestions"] == tier1["suggestions"]


def test_llm_only_merged_close_cannot_close_with_system_one_configured():
    from agent_repo.commands.triage_merge import merge

    api = API()
    tier1 = decision()
    tier1.update(mode="llm-only", answers={}, labels=[], close=None)
    tier2 = decision()
    tier2.update(
        tier=2,
        answers=complete_llm_answers(duplicate="#99"),
        labels=["type:bug", "area:api", "P2"],
        close=closing(),
    )
    merged = merge(
        tier1,
        tier2,
        None,
        allowed_labels={"type:bug", "area:api", "area:unknown"},
    )
    ctx = context(api, enabled=True)

    assert ctx.config.raw["system_one"] is not None
    assert merged["close"]["tier2_agrees"] is False
    actions = plan(merged, ctx)

    assert not any(action.kind == "close" for action in actions)


@pytest.mark.parametrize("failure", ["incomplete", "contradiction"], ids=["p1", "p2"])
def test_llm_only_escalation_cannot_close_with_system_one_configured(failure, tmp_path):
    from agent_repo.commands.triage_merge import merge

    api = API()
    tier1 = decision()
    tier1.update(
        mode="llm-only",
        answers={} if failure == "incomplete" else tier1["answers"],
        labels=["type:bug", "area:api", "P2"],
        close=closing("out_of_scope"),
    )
    tier1["close"]["tier2_agrees"] = True
    tier2 = decision()
    tier2.update(
        tier=2,
        mode="llm-only",
        answers=(
            {"type": {"value": "bug", "p": 0.99}}
            if failure == "incomplete"
            else complete_llm_answers()
        ),
        labels=["type:bug", "area:api", "P2"],
        close=closing("out_of_scope"),
    )
    merged = merge(
        tier1,
        tier2,
        None,
        allowed_labels={"type:bug", "area:api"},
    )
    ctx = context(api, enabled=True)

    assert ctx.config.raw["system_one"] is not None
    assert merged["escalate"]["to"] == "maintainer"
    assert merged["close"]["tier2_agrees"] is False
    assert not any(action.kind == "close" for action in plan(merged, ctx))

    run(merged, ctx, api, tmp_path)

    assert api.issue_state == "open"
    assert not any(method == "PATCH" for method, _, _ in api.writes)
