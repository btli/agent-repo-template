import json
import secrets
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from agent_repo.apply.actions import ApplyContext
from agent_repo.apply.review import apply_review, plan_review
from agent_repo.charter import parse_charter
from agent_repo.config import Area, RepoConfig
from agent_repo.event import load_event
from agent_repo.github import GitHub


class ReviewAPI:
    def __init__(self, *, labels=(), comments=(), head_sha="b" * 40, head_reads=None):
        self.calls = []
        self.labels = list(labels)
        self.comments = list(comments)
        self.head_sha = head_sha
        self.head_reads = iter(head_reads or ())
        self.gh = GitHub("test-token", "your-org/project", transport=self.transport)

    def transport(self, request, timeout):
        path = urlparse(request.full_url).path.removeprefix("/repos/your-org/project/")
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, body, request.get_header("Accept")))
        if request.method == "POST" and path.endswith("/reviews"):
            return 200, {}, b"{}"
        if request.method == "GET" and path == "pulls/7":
            self.head_sha = next(self.head_reads, self.head_sha)
            return 200, {}, json.dumps({"head": {"sha": self.head_sha}}).encode()
        if request.method == "GET" and path == "issues/7":
            return (
                200,
                {},
                json.dumps(
                    {"labels": [{"name": label} for label in self.labels], "assignees": []}
                ).encode(),
            )
        if request.method == "GET" and path == "issues/7/comments":
            return 200, {}, json.dumps(self.comments).encode()
        if request.method == "GET" and path == "issues/7/comments":
            return 200, {}, json.dumps(self.comments).encode()
        if request.method == "POST" and path == "issues/7/labels":
            self.labels.extend(body["labels"])
            return 200, {}, b"{}"
        return 200, {}, b"[]" if request.method == "GET" else b"{}"

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def context(api=None):
    event = replace(
        load_event(
            "pull_request_target",
            Path("tests/fixtures/events/pull_request_target.opened.json"),
        ),
        number=7,
        head_sha="b" * 40,
        repo="your-org/project",
        default_branch="main",
    )
    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}, "mode": {"review": "live"}},
        (Area("core", "Core", ("src/**",), (), False),),
        ("needs-maintainer",),
    )
    charter = parse_charter(Path("tests/fixtures/charter/example.md").read_text())
    return ApplyContext(
        event,
        config,
        charter,
        "run-1",
        "a" * 40,
        ["ghs_" + secrets.token_urlsafe(32)],
        "canary-1",
        False,
        github=api.gh if api else None,
        bot_login="agent-review[bot]",
    )


def review(**overrides):
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "Looks good overall.",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
    }
    artifact.update(overrides)
    return artifact


def test_review_is_planned_as_comment_event():
    actions = plan_review(review(), context(), {})

    assert len(actions) == 1
    assert actions[0].kind == "review"
    assert actions[0].payload["event"] == "COMMENT"


def test_finding_outside_diff_moves_to_summary():
    actions = plan_review(
        review(findings=[{"severity": "high", "path": "src/file.py", "line": 9, "body": "Bug."}]),
        context(),
        {"src/file.py": {8}},
    )

    assert actions[0].payload["comments"] == []
    assert "src/file.py:9" in actions[0].payload["body"]


def test_schema_rejects_more_than_thirty_findings():
    findings = [
        {"severity": "low", "path": "src/file.py", "line": 1, "body": f"Finding {index}."}
        for index in range(31)
    ]

    assert plan_review(review(findings=findings), context(), {"src/file.py": {1}}) == []


def test_schema_rejects_approve_event_without_posting():
    artifact = review(event="APPROVE")

    assert plan_review(artifact, context(), {}) == []


def test_request_changes_text_does_not_change_comment_event():
    actions = plan_review(
        review(summary="REQUEST_CHANGES: please review this note."), context(), {}
    )

    assert actions[0].payload["event"] == "COMMENT"


def test_finding_key_leak_is_dropped_and_alert_raised():
    key = "ghs_" + "A" * 40
    ctx = replace(context(), secrets=[key])
    artifact = review(
        findings=[{"severity": "high", "path": "src/file.py", "line": 1, "body": f"Leaked {key}"}]
    )

    actions = plan_review(artifact, ctx, {"src/file.py": {1}})

    assert actions[0].kind == "add_labels"
    assert "needs-maintainer" in actions[0].payload["labels"]
    assert actions[0].payload["alerts"]
    assert key not in repr(actions)


def test_escalated_review_plans_needs_maintainer_label():
    actions = plan_review(
        review(escalate={"to": "maintainer", "reason": "Sensitive changes."}), context(), {}
    )

    assert any(
        action.kind == "add_labels" and "needs-maintainer" in action.payload["labels"]
        for action in actions
    )


def test_head_sha_mismatch_does_not_plan_a_review():
    assert plan_review(review(head_sha="c" * 40), context(), {}) == []


def test_apply_posts_review_with_comment_event(tmp_path):
    api = ReviewAPI()
    summary = tmp_path / "summary.md"
    artifact = review(
        findings=[{"severity": "low", "path": "src/file.py", "line": 1, "body": "Looks good."}]
    )

    alerts = apply_review(artifact, context(api), {"src/file.py": {1}}, api.gh, "live", summary)

    posts = [call for call in api.writes if call[1] == "pulls/7/reviews"]
    assert not alerts
    assert len(posts) == 1
    assert posts[0][2]["event"] == "COMMENT"
    assert posts[0][2]["commit_id"] == "b" * 40
    assert posts[0][2]["comments"][0]["side"] == "RIGHT"


def test_apply_refuses_to_post_when_pr_head_moved(tmp_path):
    api = ReviewAPI(head_sha="c" * 40)

    apply_review(review(), context(api), {}, api.gh, "live", tmp_path / "summary.md")

    assert not api.writes


def test_apply_skip_label_recheck_prevents_posting(tmp_path):
    api = ReviewAPI(labels=["agent:skip"])

    alerts = apply_review(review(), context(api), {}, api.gh, "live", tmp_path / "summary.md")

    assert alerts == ["Apply skipped: automation disabled for this issue."]
    assert not api.writes


def test_apply_bot_off_marker_recheck_prevents_posting(tmp_path):
    from agent_repo.markers import marker

    api = ReviewAPI(
        comments=[
            {
                "body": marker("off", "old-run"),
                "user": {"login": "agent-review[bot]", "type": "Bot"},
            }
        ]
    )

    alerts = apply_review(review(), context(api), {}, api.gh, "live", tmp_path / "summary.md")

    assert alerts == ["Apply skipped: automation disabled for this issue."]
    assert not api.writes


def test_moved_head_prevents_escalation_label_write(tmp_path):
    api = ReviewAPI(head_sha="c" * 40)

    apply_review(
        review(escalate={"to": "maintainer", "reason": "Review requested."}),
        context(api),
        {},
        api.gh,
        "live",
        tmp_path / "summary.md",
    )

    assert not api.writes


def test_head_change_after_enabled_check_prevents_label_write(tmp_path):
    api = ReviewAPI(head_reads=["b" * 40, "c" * 40])

    apply_review(
        review(escalate={"to": "maintainer", "reason": "Review requested."}),
        context(api),
        {},
        api.gh,
        "live",
        tmp_path / "summary.md",
    )

    assert not api.writes


def test_second_head_check_catches_move_after_enabled_check(tmp_path):
    api = ReviewAPI(head_reads=["b" * 40, "b" * 40, "b" * 40, "c" * 40])

    apply_review(review(), context(api), {}, api.gh, "live", tmp_path / "summary.md")

    assert not [call for call in api.writes if call[1] == "pulls/7/reviews"]
