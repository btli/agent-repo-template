import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from agent_repo.apply.actions import Action, ApplyContext, canonical_bot_login, execute
from agent_repo.apply.triage import plan_triage
from agent_repo.charter import parse_charter
from agent_repo.config import load_config
from agent_repo.event import load_event
from agent_repo.github import GitHub


def _policy(gh: GitHub, sha: str, directory: Path):
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("invalid workflow SHA")
    for name in ("config.yml", "labels.yml", "areas.yml", "charter.md"):
        value = gh.request("GET", f"contents/.agents/{name}", params={"ref": sha})
        if value["encoding"] != "base64":
            raise ValueError("invalid policy encoding")
        content = base64.b64decode(value["content"], validate=False)
        (directory / name).write_bytes(content)
    config = load_config(directory)
    charter = parse_charter((directory / "charter.md").read_text(encoding="utf-8"))
    return sha, config, charter


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: apply <decision.json>", file=sys.stderr)
        return 2
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "apply-summary.md"))
    gh = None
    event = None
    mode = "dry-run"
    try:
        bot_login = canonical_bot_login(os.environ["AGENT_BOT_LOGIN"])
        if not bot_login:
            raise ValueError("trusted bot identity required")
        canary = os.environ["AGENT_CANARY"]
        scan_keys = [
            key.strip() for key in os.environ["AGENT_SCAN_KEYS"].splitlines() if key.strip()
        ]
        if not canary or not scan_keys:
            raise ValueError("scan configuration required")
        event = load_event(os.environ["GITHUB_EVENT_NAME"], Path(os.environ["GITHUB_EVENT_PATH"]))
        if (
            event.name not in {"issues", "issue_comment"}
            or event.is_pr
            or event.number < 1
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", event.repo) is None
        ):
            raise ValueError("triage requires an issue event")
        token = os.environ["GH_TOKEN"]
        gh = GitHub(token, event.repo)
        with tempfile.TemporaryDirectory(prefix="agent-apply-") as directory:
            base_sha, config, charter = _policy(gh, os.environ["GITHUB_SHA"], Path(directory))
        mode = config.mode_for("triage")
        ctx = ApplyContext(
            event,
            config,
            charter,
            os.environ["GITHUB_RUN_ID"],
            base_sha,
            [*scan_keys, token],
            canary,
            os.environ.get("AGENT_CAPPED") == "true",
            github=gh,
            off_authorized=os.environ.get("AGENT_OFF_AUTHORIZED") == "true",
            bot_login=bot_login,
        )
        try:
            artifact = Path(argv[0])
            if artifact.stat().st_size > 1_000_000:
                raise ValueError("artifact too large")
            decision = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError, MemoryError):
            decision = None
        alerts = execute(plan_triage(decision, ctx), gh, mode, summary)
        return 1 if any(alert.startswith("Apply failed") for alert in alerts) else 0
    except Exception:
        # Environment, file and API errors may contain credentials; publish fixed text.
        actions = []
        if event is not None and event.number > 0 and not event.is_pr:
            actions = [
                Action(
                    "add_labels",
                    {
                        "number": event.number,
                        "bot_login": bot_login,
                        "labels": ["needs-maintainer"],
                        "alerts": ["Apply failed: trusted inputs unavailable."],
                    },
                )
            ]
        execute(actions, gh, mode, summary)
        if not actions:
            with summary.open("a", encoding="utf-8") as stream:
                stream.write("- ALERT: Apply failed: trusted inputs unavailable.\n")
        print("Apply failed: trusted inputs unavailable.", file=sys.stderr)
        return 1
