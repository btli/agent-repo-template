import json
from pathlib import Path

import pytest
from agent_repo.event import Event, load_event

EVENTS = Path("tests/fixtures/events")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "issues.opened.json",
            Event(
                name="issues",
                action="opened",
                repo="your-org/project",
                number=12,
                is_pr=False,
                sender="contributor",
                sender_is_bot=False,
                author="contributor",
                author_association="CONTRIBUTOR",
                comment_body=None,
                comment_author=None,
                label=None,
                head_ref=None,
                head_repo=None,
                base_sha=None,
                head_sha=None,
                default_branch="main",
                title="Validation fails for empty input",
                body="Steps to reproduce",
                labels=("type:bug",),
                draft=False,
            ),
        ),
        (
            "issues.edited.json",
            Event(
                name="issues",
                action="edited",
                repo="your-org/project",
                number=12,
                is_pr=False,
                sender="contributor",
                sender_is_bot=False,
                author="contributor",
                author_association="CONTRIBUTOR",
                comment_body=None,
                comment_author=None,
                label=None,
                head_ref=None,
                head_repo=None,
                base_sha=None,
                head_sha=None,
                default_branch="main",
                title="Validation fails for empty input",
                body="Steps to reproduce with another case",
                labels=("type:bug",),
                draft=False,
                edited_fields=("body",),
            ),
        ),
        (
            "issue_comment.created.issue.json",
            Event(
                name="issue_comment",
                action="created",
                repo="your-org/project",
                number=12,
                is_pr=False,
                sender="reporter",
                sender_is_bot=False,
                author="contributor",
                author_association="CONTRIBUTOR",
                comment_body="I can reproduce this",
                comment_author="reporter",
                label=None,
                head_ref=None,
                head_repo=None,
                base_sha=None,
                head_sha=None,
                default_branch="main",
                title="Validation fails for empty input",
                body="Steps to reproduce",
                labels=(),
                draft=False,
            ),
        ),
        (
            "issue_comment.created.pr.json",
            Event(
                name="issue_comment",
                action="created",
                repo="your-org/project",
                number=24,
                is_pr=True,
                sender="maintainer",
                sender_is_bot=False,
                author="contributor",
                author_association="MEMBER",
                comment_body="/review",
                comment_author="maintainer",
                label=None,
                head_ref=None,
                head_repo=None,
                base_sha=None,
                head_sha=None,
                default_branch="main",
                title="Improve validation messages",
                body="PR discussion",
                labels=(),
                draft=False,
            ),
        ),
        (
            "issues.labeled.json",
            Event(
                name="issues",
                action="labeled",
                repo="your-org/project",
                number=31,
                is_pr=False,
                sender="maintainer",
                sender_is_bot=False,
                author="contributor",
                author_association="CONTRIBUTOR",
                comment_body=None,
                comment_author=None,
                label="agent:fix",
                head_ref=None,
                head_repo=None,
                base_sha=None,
                head_sha=None,
                default_branch="main",
                title="Fix parser edge case",
                body="Parser mishandles this input",
                labels=("agent:fix",),
                draft=False,
            ),
        ),
        (
            "pull_request_target.opened.json",
            Event(
                name="pull_request_target",
                action="opened",
                repo="your-org/project",
                number=24,
                is_pr=True,
                sender="contributor",
                sender_is_bot=False,
                author="contributor",
                author_association="CONTRIBUTOR",
                comment_body=None,
                comment_author=None,
                label=None,
                head_ref="feature/messages",
                head_repo="your-org/project",
                base_sha="b" * 40,
                head_sha="a" * 40,
                default_branch="main",
                title="Improve validation messages",
                body="Updates the parser",
                labels=(),
                draft=False,
            ),
        ),
    ],
)
def test_fixtures_map_expected_fields(filename: str, expected: Event):
    event = load_event(expected.name, EVENTS / filename)
    assert event == expected
    expected_edited_fields = ("body",) if filename == "issues.edited.json" else ()
    assert event.edited_fields == expected_edited_fields


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, ()),
        (None, ()),
        ({"title": {"from": "Old title"}, "body": {"from": "Old body"}}, ("body", "title")),
    ],
)
def test_edited_fields_are_sorted_or_empty(
    tmp_path: Path, changes: object, expected: tuple[str, ...]
):
    payload = json.loads((EVENTS / "issues.edited.json").read_text())
    if changes is None:
        payload.pop("changes")
    else:
        payload["changes"] = changes
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(payload))

    assert load_event("issues", path).edited_fields == expected


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
