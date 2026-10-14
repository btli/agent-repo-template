import json
from dataclasses import dataclass
from pathlib import Path


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


def load_event(name: str, path: Path) -> Event:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid event payload: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    sender_data = payload.get("sender") or {}
    sender = str(sender_data.get("login", ""))
    sender_type = sender_data.get("type", "")
    comment = payload.get("comment") or {}
    is_comment = name == "issue_comment"
    issue = payload.get("issue")
    pull = payload.get("pull_request")
    if is_comment:
        is_pr = isinstance(issue, dict) and "pull_request" in issue
        item = issue or {}
        pull = item.get("pull_request") if is_pr else None
    else:
        is_pr = name == "pull_request_target"
        item = pull if is_pr else issue or {}
    user = item.get("user") or {}
    repo_data = payload.get("repository") or {}
    head = item.get("head") or {}
    head_repo = head.get("repo") or {}
    base = item.get("base") or {}
    label_data = payload.get("label") or {}
    labels_data = item.get("labels") or []
    return Event(
        name=name,
        action=str(payload.get("action", "")),
        repo=str(repo_data.get("full_name", "")),
        number=int(item.get("number", 0)),
        is_pr=is_pr,
        sender=sender,
        sender_is_bot=sender_type == "Bot" or sender.endswith("[bot]"),
        author=str(user.get("login", "")),
        author_association=str(item.get("author_association", "")),
        comment_body=comment.get("body") if is_comment else None,
        comment_author=(comment.get("user") or {}).get("login") if is_comment else None,
        label=label_data.get("name"),
        head_ref=head.get("ref"),
        head_repo=head_repo.get("full_name"),
        base_sha=base.get("sha"),
        head_sha=head.get("sha"),
        default_branch=str(repo_data.get("default_branch", "")),
        title=str(item.get("title", "")),
        body=str(item.get("body", "")),
        labels=tuple(label.get("name", "") for label in labels_data),
        draft=bool(item.get("draft", False)),
    )
