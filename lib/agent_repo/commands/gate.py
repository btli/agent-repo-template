import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_repo.apply.actions import canonical_bot_login
from agent_repo.commands.fetch_diff import _event_base_ref
from agent_repo.config import ConfigError, load_config
from agent_repo.event import load_event
from agent_repo.gate import GateContext, GateResult, evaluate
from agent_repo.github import GitHub


def _maintainers(checkout: Path) -> frozenset[str]:
    path = checkout / "MAINTAINERS"
    if not path.exists():
        return frozenset()
    return frozenset(
        login
        for line in path.read_text(encoding="utf-8").splitlines()
        if (login := line.split("#", 1)[0].strip())
    )


def _write_outputs(result: GateResult) -> None:
    values = {
        "run": str(result.run).lower(),
        "reason": result.reason,
        "tier2_allowed": str(result.tier2_allowed).lower(),
        "capped": str(result.capped).lower(),
        "command": result.command.name if result.command else "",
        "command_args": result.command.args if result.command else "",
        "canary": result.canary,
        "head_sha": result.head_sha,
        "issue": result.issue,
        "base_sha": result.base_sha,
        "head_ref": result.head_ref,
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def _fix_context(directory: Path) -> int:
    try:
        issue = os.environ["GATE_FIX_ISSUE"]
        base = os.environ["GATE_FIX_BASE"]
        canary = os.environ["GATE_FIX_CANARY"]
        run_id = os.environ["GITHUB_RUN_ID"]
        if (
            os.environ["GATE_FIX_AUTHORIZED"] != "true"
            or re.fullmatch(r"[1-9][0-9]*", issue) is None
            or re.fullmatch(r"[0-9a-f]{40}", base) is None
            or re.fullmatch(r"[0-9a-f]{32}", canary) is None
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", run_id) is None
        ):
            raise ValueError
        event = load_event(os.environ["GITHUB_EVENT_NAME"], Path(os.environ["GITHUB_EVENT_PATH"]))
        token = os.environ["GITHUB_TOKEN"]
        if event.is_pr:
            item = GitHub(token, event.repo).request("GET", f"issues/{issue}")
            if "pull_request" in item:
                raise ValueError
            title, body = item["title"], item.get("body") or ""
        else:
            if str(event.number) != issue:
                raise ValueError
            title, body = event.title, event.body
        text = f"{title}\n\n{body}\n\n{event.comment_body or ''}"[:100000]
        if token and token in text:
            raise ValueError
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or any(
            (directory / name).is_symlink() for name in ("issue.txt", "canary.json")
        ):
            raise ValueError
        (directory / "issue.txt").write_text(text, encoding="utf-8")
        (directory / "canary.json").write_text(
            json.dumps(
                {"schema_version": "1", "run_id": run_id, "base_sha": base, "canary": canary}
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception:
        print("Fix context unavailable.", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "fix-context":
        return _fix_context(Path(argv[1]))
    if len(argv) != 2 or argv[0] not in {"triage", "review", "fix"}:
        print("usage: gate <triage|review|fix> <default-branch-checkout>", file=sys.stderr)
        return 2
    status = 0
    try:
        if os.environ.get("AGENTS_ENABLED") != "true":
            result = GateResult(False, "disabled", False, False, None, secrets.token_hex(16))
        else:
            checkout = Path(argv[1])
            event_name = os.environ["GITHUB_EVENT_NAME"]
            event_path = Path(os.environ["GITHUB_EVENT_PATH"])
            event = load_event(event_name, event_path)
            ctx = GateContext(
                workflow=argv[0],
                enabled=True,
                bot_login=canonical_bot_login(os.environ["AGENT_BOT_LOGIN"]),
                config=load_config(checkout / ".agents"),
                gh=GitHub(os.environ["GITHUB_TOKEN"], event.repo),
                now=datetime.now(UTC),
                maintainers=_maintainers(checkout),
                run_id=os.environ["GITHUB_RUN_ID"],
                event_sha=os.environ.get("GITHUB_SHA", ""),
            )
            # GITHUB_SHA is the default-branch policy only when the PR targets that branch.
            if (
                event.name == "pull_request_target"
                and _event_base_ref(event_name, event_path) != event.default_branch
            ):
                result = GateResult(
                    False, "non-default-base", False, False, None, secrets.token_hex(16)
                )
            else:
                result = evaluate(event, ctx)
    except (ConfigError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Gate input could not be loaded.", file=sys.stderr)
        result = GateResult(False, "gate-error", False, False, None, secrets.token_hex(16))
        status = 1
    try:
        _write_outputs(result)
    except (OSError, KeyError):
        print("Gate outputs could not be written.", file=sys.stderr)
        return 1
    return status
