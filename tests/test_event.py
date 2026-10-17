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


def test_non_edit_event_ignores_nonempty_changes(tmp_path: Path):
    payload = json.loads((EVENTS / "issues.opened.json").read_text())
    payload["changes"] = {"body": {"from": "previous body"}}
    path = tmp_path / "opened.json"
    path.write_text(json.dumps(payload))
    assert load_event("issues", path).edited_fields == ()


@pytest.mark.parametrize("field", ["title", "body"])
def test_null_issue_text_fields_normalize_to_empty_string(tmp_path: Path, field: str):
    payload = json.loads((EVENTS / "issues.opened.json").read_text())
    payload["issue"][field] = None
    path = tmp_path / "null-text.json"
    path.write_text(json.dumps(payload))
    event = load_event("issues", path)
    assert getattr(event, field) == ""


def test_pull_request_without_payload_raises_value_error(tmp_path: Path):
    payload = json.loads((EVENTS / "pull_request_target.opened.json").read_text())
    payload.pop("pull_request")
    path = tmp_path / "missing-pr.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="event item is required"):
        load_event("pull_request_target", path)


def test_pull_request_draft_state_is_preserved(tmp_path: Path):
    payload = json.loads((EVENTS / "pull_request_target.opened.json").read_text())
    payload["pull_request"]["draft"] = True
    path = tmp_path / "draft-pr.json"
    path.write_text(json.dumps(payload))
    assert load_event("pull_request_target", path).draft is True


def test_issue_without_payload_raises_value_error(tmp_path: Path):
    payload = json.loads((EVENTS / "issues.opened.json").read_text())
    payload.pop("issue")
    path = tmp_path / "missing-issue.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="event item is required"):
        load_event("issues", path)


def test_non_mapping_pull_request_payload_raises_value_error(tmp_path: Path):
    payload = json.loads((EVENTS / "pull_request_target.opened.json").read_text())
    payload["pull_request"] = []
    path = tmp_path / "invalid-pr.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="event object must be an object"):
        load_event("pull_request_target", path)


@pytest.mark.parametrize(
    ("filename", "object_path"),
    [
        ("issues.opened.json", ("sender",)),
        ("issue_comment.created.issue.json", ("comment",)),
        ("issue_comment.created.issue.json", ("comment", "user")),
        ("issues.opened.json", ("issue", "user")),
        ("issues.opened.json", ("repository",)),
        ("pull_request_target.opened.json", ("pull_request", "head")),
        ("pull_request_target.opened.json", ("pull_request", "head", "repo")),
        ("pull_request_target.opened.json", ("pull_request", "base")),
        ("issues.labeled.json", ("label",)),
        ("issues.opened.json", ("issue", "labels")),
    ],
)
def test_invalid_nested_event_objects_raise_value_error(
    tmp_path: Path, filename: str, object_path: tuple[str, ...]
):
    payload = json.loads((EVENTS / filename).read_text())
    target = payload
    for key in object_path[:-1]:
        target = target[key]
    target[object_path[-1]] = "not an object"
    path = tmp_path / "invalid-object.json"
    path.write_text(json.dumps(payload))
    event_name = "issue_comment" if filename.startswith("issue_comment") else filename.split(".")[0]
    with pytest.raises(ValueError, match="event object must be an object"):
        load_event(event_name, path)


@pytest.mark.parametrize("number", [None, "12", True, 12.0])
def test_non_integer_event_number_raises_value_error(tmp_path: Path, number: object):
    payload = json.loads((EVENTS / "issues.opened.json").read_text())
    payload["issue"]["number"] = number
    path = tmp_path / "invalid-number.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="event number must be an integer"):
        load_event("issues", path)


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
