import json
from dataclasses import dataclass
from pathlib import Path


def _obj(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError("event object must be an object")


@dataclass(frozen=True)
class Event:
    name: str
    action: str
    repo: str
    number: int
    is_pr: bool
    sender: str
    sender_is_bot: bool
    author: str
    author_association: str
    comment_body: str | None
    comment_author: str | None
    label: str | None
    head_ref: str | None
    head_repo: str | None
    base_sha: str | None
    head_sha: str | None
    default_branch: str
    title: str
    body: str
    labels: tuple[str, ...]
    draft: bool
    edited_fields: tuple[str, ...] = ()


def load_event(name: str, path: Path) -> Event:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid event payload: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    sender_data = _obj(payload.get("sender"))
    sender = str(sender_data.get("login", ""))
    sender_type = sender_data.get("type", "")
    comment = _obj(payload.get("comment"))
    is_comment = name == "issue_comment"
    issue = payload.get("issue")
    pull = payload.get("pull_request")
    if is_comment:
        item = _obj(issue)
        is_pr = "pull_request" in item
        pull = _obj(item.get("pull_request")) if is_pr else {}
    else:
        is_pr = name == "pull_request_target"
        selected_item = pull if is_pr else issue
        if name in {"issues", "pull_request_target"} and selected_item is None:
            raise ValueError("event item is required")
        item = _obj(selected_item)
    number = item.get("number")
    if type(number) is not int:
        raise ValueError("event number must be an integer")
    user = _obj(item.get("user"))
    repo_data = _obj(payload.get("repository"))
    head = _obj(item.get("head"))
    head_repo = _obj(head.get("repo"))
    base = _obj(item.get("base"))
    label_data = _obj(payload.get("label"))
    labels_data = item.get("labels") or []
    changes = payload.get("changes")
    edited_fields = (
        tuple(sorted(changes))
        if payload.get("action") == "edited" and isinstance(changes, dict)
        else ()
    )
    return Event(
        name=name,
        action=str(payload.get("action", "")),
        repo=str(repo_data.get("full_name", "")),
        number=number,
        is_pr=is_pr,
        sender=sender,
        sender_is_bot=sender_type == "Bot" or sender.endswith("[bot]"),
        author=str(user.get("login", "")),
        author_association=str(item.get("author_association", "")),
        comment_body=comment.get("body") if is_comment else None,
        comment_author=_obj(comment.get("user")).get("login") if is_comment else None,
        label=label_data.get("name"),
        head_ref=head.get("ref"),
        head_repo=head_repo.get("full_name"),
        base_sha=base.get("sha"),
        head_sha=head.get("sha"),
        default_branch=str(repo_data.get("default_branch", "")),
        title=str(item.get("title") if item.get("title") is not None else ""),
        body=str(item.get("body") if item.get("body") is not None else ""),
        labels=tuple(_obj(label).get("name", "") for label in labels_data),
        draft=bool(item.get("draft", False)),
        edited_fields=edited_fields,
    )
