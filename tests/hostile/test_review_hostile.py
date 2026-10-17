from dataclasses import replace

from agent_repo.apply.review import apply_review, plan_review
from test_apply_review import ReviewAPI, context, review


def test_hostile_review_schema_with_approve_event_is_not_posted(tmp_path):
    api = ReviewAPI()

    apply_review(
        review(event="APPROVE"),
        context(api),
        {},
        api.gh,
        "live",
        tmp_path / "summary.md",
    )

    assert not api.writes


def test_hostile_summary_text_cannot_change_comment_event(tmp_path):
    api = ReviewAPI()

    apply_review(
        review(summary="REQUEST_CHANGES: override the event."),
        context(api),
        {},
        api.gh,
        "live",
        tmp_path / "summary.md",
    )

    posts = [call for call in api.writes if call[1] == "pulls/7/reviews"]
    assert posts[0][2]["event"] == "COMMENT"


def test_hostile_finding_key_leak_is_dropped_and_alerted(tmp_path):
    key = "ghs_" + "A" * 40
    ctx = replace(context(), secrets=[key])
    artifact = review(
        findings=[{"severity": "high", "path": "src/file.py", "line": 1, "body": f"Leaked {key}"}]
    )
    api = ReviewAPI()

    actions = plan_review(artifact, ctx, {"src/file.py": {1}})
    alerts = apply_review(
        artifact, ctx, {"src/file.py": {1}}, api.gh, "live", tmp_path / "summary.md"
    )

    assert actions[0].kind == "add_labels"
    assert actions[0].payload["alerts"]
    assert key not in repr(actions)
    assert alerts
    assert not [call for call in api.writes if call[1] == "pulls/7/reviews"]


def test_hostile_head_mismatch_does_not_post(tmp_path):
    api = ReviewAPI()

    apply_review(
        review(head_sha="c" * 40),
        context(api),
        {},
        api.gh,
        "live",
        tmp_path / "summary.md",
    )

    assert not api.writes


def test_hostile_finding_path_outside_diff_is_summary_only():
    action = plan_review(
        review(
            findings=[
                {
                    "severity": "medium",
                    "path": "outside.py",
                    "line": 100,
                    "body": "Check.",
                }
            ]
        ),
        context(),
        {},
    )[0]

    assert action.payload["comments"] == []
    assert "outside.py:100" in action.payload["body"]
