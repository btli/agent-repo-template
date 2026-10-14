import json
from pathlib import Path

import pytest
from agent_repo.event import load_event

EVENTS = Path("tests/fixtures/events")


@pytest.mark.parametrize(
    ("filename", "name", "action", "is_pr"),
    [
        ("issues.opened.json", "issues", "opened", False),
        ("issue_comment.created.issue.json", "issue_comment", "created", False),
        ("issue_comment.created.pr.json", "issue_comment", "created", True),
        ("issues.labeled.json", "issues", "labeled", False),
        ("pull_request_target.opened.json", "pull_request_target", "opened", True),
    ],
)
def test_fixtures_map_expected_fields(filename: str, name: str, action: str, is_pr: bool):
    event = load_event(name, EVENTS / filename)
    payload = json.loads((EVENTS / filename).read_text())
    number = payload["issue"]["number"] if "issue" in payload else payload["pull_request"]["number"]
    assert event.name == name
    assert event.action == action
    assert event.repo == payload["repository"]["full_name"]
    assert event.number == number
    assert event.is_pr is is_pr
    assert event.sender == payload["sender"]["login"]
    assert event.default_branch == "main"
    if filename == "issues.opened.json":
        assert (event.author, event.author_association, event.title, event.body, event.labels) == (
            "contributor",
            "CONTRIBUTOR",
            "Validation fails for empty input",
            "Steps to reproduce",
            ("type:bug",),
        )
    elif filename.startswith("issue_comment"):
        expected_comment = (
            ("I can reproduce this", "reporter")
            if filename.endswith("issue.json")
            else ("/review", "maintainer")
        )
        assert (event.comment_body, event.comment_author) == expected_comment
    elif filename == "issues.labeled.json":
        assert event.label == "agent:fix"
        assert event.labels == ("agent:fix",)
    else:
        assert (event.head_ref, event.head_repo, event.base_sha, event.head_sha, event.draft) == (
            "feature/messages",
            "your-org/project",
            "b" * 40,
            "a" * 40,
            False,
        )


@pytest.mark.parametrize(
    "sender_type,login,expected",
    [("Bot", "helper", True), ("User", "helper[bot]", True), ("User", "human", False)],
)
def test_sender_is_bot_for_bot_type_or_suffix(
    tmp_path: Path, sender_type: str, login: str, expected: bool
):
    payload = json.loads((EVENTS / "issues.opened.json").read_text())
    payload["sender"] = {"type": sender_type, "login": login}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    assert load_event("issues", path).sender_is_bot is expected
