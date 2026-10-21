import json
import os
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from agent_repo.apply.actions import ApplyContext, canonical_bot_login, execute
from agent_repo.apply.review import apply_review, summary_only_review
from agent_repo.apply.triage import _off
from agent_repo.commands.apply import _policy
from agent_repo.commands.fetch_diff import _changed_files, _event_base_ref, _pull
from agent_repo.config import ConfigError
from agent_repo.diff import diff_index, fetch_files, summary_only
from agent_repo.event import load_event
from agent_repo.github import GitHub, GitHubError


def _scan_keys() -> list[str]:
    scan_keys = [
        value.strip()
        for value in os.environ.get("AGENT_SCAN_KEYS", "").splitlines()
        if value.strip()
    ]
    if not scan_keys:
        raise ValueError
    return scan_keys


def _apply_off(gh: GitHub, event, bot_login: str, summary: Path) -> int:
    # An authorized /agent off on a pull request posts the bot off marker; no review artifact.
    if event.name != "issue_comment" or not event.is_pr:
        raise ValueError("off requires a pull request comment")
    base_sha = os.environ["GITHUB_SHA"]
    with tempfile.TemporaryDirectory(prefix="agent-review-policy-") as directory:
        _, config, charter = _policy(gh, base_sha, Path(directory))
    ctx = ApplyContext(
        event,
        config,
        charter,
        os.environ["GITHUB_RUN_ID"],
        base_sha,
        [*_scan_keys(), os.environ["GH_TOKEN"]],
        os.environ["AGENT_CANARY"],
        False,
        github=gh,
        off_authorized=True,
        bot_login=bot_login,
    )
    alerts = execute(_off(ctx), gh, config.mode_for("review"), summary)
    return 1 if any(alert.startswith("Apply failed") for alert in alerts) else 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: review_apply <review.json>", file=sys.stderr)
        return 2
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "review-apply-summary.md"))
    try:
        bot_login = canonical_bot_login(os.environ["AGENT_BOT_LOGIN"])
        if not bot_login:
            raise ValueError("trusted bot identity required")
        if os.environ.get("AGENT_OFF_AUTHORIZED") == "true":
            event = load_event(
                os.environ["GITHUB_EVENT_NAME"], Path(os.environ["GITHUB_EVENT_PATH"])
            )
            return _apply_off(GitHub(os.environ["GH_TOKEN"], event.repo), event, bot_login, summary)
        review_path = Path(argv[0])
        if review_path.stat().st_size > 1_000_000:
            raise ValueError
        review = json.loads(review_path.read_text(encoding="utf-8"))
        event_name = os.environ["GITHUB_EVENT_NAME"]
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        event = load_event(event_name, event_path)
        if (
            event.name == "pull_request_target"
            and _event_base_ref(event_name, event_path) != event.default_branch
        ):
            raise ValueError("event pull request is not based on the default branch")
        gh = GitHub(os.environ["GH_TOKEN"], event.repo)
        expected_head_sha = os.environ["EXPECTED_HEAD_SHA"]
        if re.fullmatch(r"[0-9a-f]{40}", expected_head_sha) is None:
            raise ValueError
        try:
            pull = _pull(gh, event)
        except ValueError as error:
            if str(error) == "pull request head changed since event":
                summary.write_text("No review posted: pull request head moved.\n", encoding="utf-8")
                return 0
            raise
        base_sha = os.environ["GITHUB_SHA"]
        if (
            pull["head"]["sha"] != expected_head_sha
            or (event.head_sha is not None and event.head_sha != expected_head_sha)
            or review.get("head_sha") != expected_head_sha
        ):
            summary.write_text("No review posted: pull request head moved.\n", encoding="utf-8")
            return 0
        changed_files = _changed_files(pull)
        files_invalid = False
        files_incomplete = changed_files is None or changed_files > 3000
        if files_incomplete:
            files = []
        else:
            try:
                files = fetch_files(gh, event.number)
            except ValueError:
                files = []
                files_invalid = True
        current_pull = _pull(gh, event)
        if current_pull["head"]["sha"] != expected_head_sha:
            summary.write_text("No review posted: pull request head moved.\n", encoding="utf-8")
            return 0
        if review.get("base_sha") != base_sha:
            summary.write_text(
                "No review posted: policy base identity mismatch.\n", encoding="utf-8"
            )
            return 0
        with tempfile.TemporaryDirectory(prefix="agent-review-policy-") as directory:
            _, config, charter = _policy(gh, base_sha, Path(directory))
        files_incomplete = files_incomplete or files_invalid or len(files) != changed_files
        limited, reason = (
            summary_only(files, config)
            if not files_incomplete
            else (
                True,
                "Pull request file list is incomplete; needs-maintainer.",
            )
        )
        if limited:
            review = summary_only_review(
                review["run_id"], review["base_sha"], review["head_sha"], reason
            )
        if event.head_sha is None:
            event = replace(event, head_sha=review["head_sha"])
        ctx = ApplyContext(
            event,
            config,
            charter,
            os.environ["GITHUB_RUN_ID"],
            base_sha,
            [*_scan_keys(), os.environ["GH_TOKEN"]],
            os.environ["AGENT_CANARY"],
            False,
            github=gh,
            bot_login=bot_login,
        )
        mode = config.mode_for("review")
        alerts = apply_review(
            review,
            ctx,
            {} if limited else diff_index(files),
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
