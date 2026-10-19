import json
import os
import re
import sys
from pathlib import Path

from agent_repo.config import load_config
from agent_repo.diff import fetch_files, render_files, summary_only
from agent_repo.event import Event, load_event
from agent_repo.github import GitHub, GitHubError


def _pull(gh: GitHub, event: Event) -> dict:
    value = gh.request("GET", f"pulls/{event.number}")
    if not isinstance(value, dict) or not isinstance(value.get("head"), dict):
        raise ValueError("invalid pull request metadata")
    base = value.get("base")
    if not isinstance(base, dict) or base.get("ref") != event.default_branch:
        raise ValueError("pull request is not based on the default branch")
    if event.name == "pull_request_target":
        if event.base_sha != base.get("sha"):
            raise ValueError("pull request base changed since event")
    head_sha = value["head"].get("sha")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise ValueError("invalid pull request head")
    if event.name == "pull_request_target" and event.head_sha != head_sha:
        raise ValueError("pull request head changed since event")
    return value


def _event_base_ref(event_name: str, event_path: Path) -> str | None:
    if event_name != "pull_request_target":
        return None
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    pull = payload.get("pull_request") if isinstance(payload, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    if isinstance(base, dict):
        value = base.get("ref")
        return value if isinstance(value, str) else None
    return None


def _changed_files(pull: dict) -> int | None:
    count = pull.get("changed_files")
    return count if type(count) is int and count >= 0 else None


def fetch(files_path: Path, config_dir: Path) -> tuple[str, str, bool, str]:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = load_event(event_name, event_path)
    if (
        event.name not in {"pull_request_target", "issue_comment"}
        or not event.is_pr
        or event.number < 1
        or not event.default_branch
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", event.repo) is None
    ):
        raise ValueError("pull request event required")
    base_sha = os.environ["GITHUB_SHA"]
    if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
        raise ValueError("invalid policy SHA")
    if event.name == "pull_request_target" and event.base_sha != base_sha:
        raise ValueError("event base does not match policy SHA")
    if _event_base_ref(event_name, event_path) not in (None, event.default_branch):
        raise ValueError("event pull request is not based on the default branch")
    expected_head_sha = os.environ["EXPECTED_HEAD_SHA"]
    if re.fullmatch(r"[0-9a-f]{40}", expected_head_sha) is None:
        raise ValueError("invalid gate head SHA")
    gh = GitHub(os.environ["GH_TOKEN"], event.repo)
    pull = _pull(gh, event)
    head_sha = pull["head"]["sha"]
    if head_sha != expected_head_sha:
        raise ValueError("pull request head changed since gate")
    files_invalid = False
    try:
        files = fetch_files(gh, event.number)
    except ValueError:
        files = []
        files_invalid = True
    current_pull = _pull(gh, event)
    if current_pull["head"]["sha"] != expected_head_sha:
        raise ValueError("pull request head changed while fetching diff")
    changed_files = _changed_files(pull)
    config = load_config(config_dir)
    if files_invalid:
        limited, reason = True, "Pull request file list is malformed; needs-maintainer."
    elif changed_files is None or changed_files > 3000 or len(files) != changed_files:
        limited, reason = True, "Pull request file list is incomplete; needs-maintainer."
    else:
        limited, reason = summary_only(files, config)
    reason = reason.replace("\r", " ").replace("\n", " ")[:4000]
    files_path.parent.mkdir(parents=True, exist_ok=True)
    files_path.write_text(render_files(files), encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"base_sha={base_sha}\n")
        output.write(f"head_sha={head_sha}\n")
        output.write(f"summary_only={str(limited).lower()}\n")
        output.write(f"summary_reason={reason}\n")
    return base_sha, head_sha, limited, reason


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fetch_diff <files-file> <trusted-config-dir>", file=sys.stderr)
        return 2
    try:
        fetch(Path(argv[0]), Path(argv[1]))
    except (GitHubError, KeyError, OSError, TypeError, ValueError):
        print("Pull request diff could not be fetched safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
