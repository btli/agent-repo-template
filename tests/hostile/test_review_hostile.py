import json
import shlex
from dataclasses import replace

from agent_repo.apply.review import apply_review, plan_review
from test_apply_review import ReviewAPI, context, review
from test_review_agent import _prepare_review_run


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


def test_hostile_key_split_by_html_comments_is_dropped_and_alerted(tmp_path):
    key = "ghs_" + "abcdefghijklmnopqrstuvwxyz" + str(1234567890)
    split_key = "<!--z-->".join(key[index : index + 6] for index in range(0, len(key), 6))
    ctx = replace(context(), secrets=[key])
    artifact = review(
        findings=[
            {
                "severity": "high",
                "path": "src/file.py",
                "line": 1,
                "body": f"Leaked {split_key}",
            }
        ]
    )
    api = ReviewAPI()

    actions = plan_review(artifact, ctx, {"src/file.py": {1}})
    alerts = apply_review(
        artifact, ctx, {"src/file.py": {1}}, api.gh, "live", tmp_path / "summary.md"
    )

    assert actions[0].kind == "add_labels"
    assert "rendered review dropped" in actions[0].payload["alerts"][0]
    assert alerts
    assert not [call for call in api.writes if call[1] == "pulls/7/reviews"]


def test_hostile_canary_split_by_html_comments_is_never_posted(tmp_path, monkeypatch):
    canary = "gate-canary-0123456789abcdef"
    split_canary = "<!--z-->".join(canary[index : index + 6] for index in range(0, len(canary), 6))
    artifact = review(
        findings=[
            {
                "severity": "high",
                "path": "src/file.py",
                "line": 1,
                "body": f"Leaked {split_canary}",
            }
        ]
    )
    response = "```json\n" + json.dumps(artifact) + "\n```"
    prompt_path = tmp_path / "prompt.txt"
    fake_body = (
        "printf '%s\\n' \"$*\" > "
        + shlex.quote(str(prompt_path))
        + "\nprintf '%s\\n' "
        + shlex.quote(response)
    )
    module, output = _prepare_review_run(tmp_path, monkeypatch, fake_body)

    assert module.main() == 0
    assert canary in prompt_path.read_text()
    api = ReviewAPI()
    ctx = replace(context(api), canary=canary)
    proposed = json.loads(output.read_text())
    alerts = apply_review(
        proposed, ctx, {"src/file.py": {1}}, api.gh, "live", tmp_path / "summary.md"
    )

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
