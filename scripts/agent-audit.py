"""List every agent marker comment and agent:* item in a repository, with links.

Usage: agent-audit.py --repo OWNER/REPO --bot-login LOGIN

Maintainers use the list to revert agent labels and reopen agent closes. Data comes
from `gh api`; only comments posted by the bot account count as markers.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_repo.apply.actions import canonical_bot_login  # noqa: E402
from agent_repo.markers import find_markers  # noqa: E402


class GhError(Exception):
    """Raised when a `gh api` call fails; the message names only the endpoint."""


def gh_items(endpoint: str) -> list:
    try:
        result = subprocess.run(
            ["gh", "api", "--paginate", "--jq", ".[]", endpoint],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh api failed: {endpoint}") from exc
    if result.returncode != 0:
        raise GhError(f"gh api failed: {endpoint}")
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as exc:
        raise GhError(f"gh api returned invalid JSON: {endpoint}") from exc


def marker_comments(comments: list, bot_login: str) -> list[str]:
    lines = []
    for comment in comments:
        user = comment.get("user") or {}
        if canonical_bot_login(user.get("login", "")) != bot_login:
            continue
        for found in find_markers(comment.get("body") or ""):
            number = comment.get("issue_url", "").rsplit("/", 1)[-1]
            lines.append(
                f"- #{number} {found.role} run {found.run_id}: {comment.get('html_url', '')}"
            )
    return lines


def agent_items(items: list) -> list[str]:
    lines = []
    for item in items:
        labels = sorted(
            label["name"] for label in item.get("labels", []) if label["name"].startswith("agent:")
        )
        if not labels:
            continue
        hint = (
            "reopen if the close was wrong"
            if "agent:closed" in labels and item.get("state") == "closed"
            else "remove the label to revert"
        )
        kind = "pull request" if "pull_request" in item else "issue"
        lines.append(
            f"- #{item['number']} {kind} ({item.get('state')}), {', '.join(labels)}; "
            f"{hint}: {item.get('html_url', '')}"
        )
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="List agent markers and agent:* items.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--bot-login", required=True)
    args = parser.parse_args(argv)
    bot_login = canonical_bot_login(args.bot_login)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) is None or not bot_login:
        print("--repo must be OWNER/REPO and --bot-login a GitHub login.", file=sys.stderr)
        return 2
    try:
        comments = gh_items(f"repos/{args.repo}/issues/comments?per_page=100")
        items = gh_items(f"repos/{args.repo}/issues?state=all&per_page=100")
    except GhError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    markers = marker_comments(comments, bot_login)
    labelled = agent_items(items)
    print(f"# Agent audit for {args.repo}")
    print(f"\n## Marker comments by {bot_login} ({len(markers)})\n")
    print("\n".join(markers) or "- None.")
    print(f"\n## Items with agent:* labels ({len(labelled)})\n")
    print("\n".join(labelled) or "- None.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
