import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from agent_repo.apply.actions import ApplyContext
from agent_repo.apply.review import apply_review
from agent_repo.commands.apply import _policy
from agent_repo.commands.fetch_diff import _pull
from agent_repo.config import ConfigError
from agent_repo.diff import diff_index
from agent_repo.event import load_event
from agent_repo.github import GitHub, GitHubError


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: review_apply <review.json> <diff.patch>", file=sys.stderr)
        return 2
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "review-apply-summary.md"))
    try:
        review_path, diff_path = map(Path, argv)
        if review_path.stat().st_size > 1_000_000 or diff_path.stat().st_size > 2_000_000:
            raise ValueError
        review = json.loads(review_path.read_text(encoding="utf-8"))
        event = load_event(os.environ["GITHUB_EVENT_NAME"], Path(os.environ["GITHUB_EVENT_PATH"]))
        gh = GitHub(os.environ["GH_TOKEN"], event.repo)
        pull = _pull(gh, event)
        base_sha = os.environ["GITHUB_SHA"]
        if event.name == "pull_request_target" and event.base_sha != base_sha:
            raise ValueError
        if review.get("head_sha") != pull["head"]["sha"]:
            summary.write_text("No review posted: pull request head moved.\n", encoding="utf-8")
            return 0
        if review.get("base_sha") != base_sha:
            summary.write_text(
                "No review posted: policy base identity mismatch.\n", encoding="utf-8"
            )
            return 0
        with tempfile.TemporaryDirectory(prefix="agent-review-policy-") as directory:
            _, config, charter = _policy(gh, base_sha, Path(directory))
        if event.head_sha is None:
            event = replace(event, head_sha=review["head_sha"])
        token = os.environ["GH_TOKEN"]
        scan_keys = [
            value.strip()
            for value in os.environ.get("AGENT_SCAN_KEYS", "").splitlines()
            if value.strip()
        ]
        if not scan_keys:
            raise ValueError
        ctx = ApplyContext(
            event,
            config,
            charter,
            os.environ["GITHUB_RUN_ID"],
            base_sha,
            [*scan_keys, token],
            os.environ["AGENT_CANARY"],
            False,
            github=gh,
            bot_login=os.environ["AGENT_BOT_LOGIN"],
        )
        mode = config.mode_for("review")
        alerts = apply_review(
            review,
            ctx,
            diff_index(diff_path.read_text(encoding="utf-8")),
            gh,
            mode,
            summary,
        )
        return 1 if any(alert.startswith("Apply failed") for alert in alerts) else 0
    except (
        ConfigError,
        GitHubError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        summary.write_text(
            "No review posted: trusted review inputs unavailable.\n", encoding="utf-8"
        )
        print("Review apply failed safely.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
