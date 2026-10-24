import json
import os
import re
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from agent_repo.apply.actions import ApplyContext, canonical_bot_login
from agent_repo.apply.fix import Violation, _sha, apply_fix, branch_for
from agent_repo.commands.apply import _policy
from agent_repo.commands.preflight import _load
from agent_repo.event import load_event
from agent_repo.github import GitHub
from agent_repo.preflight import FIX_RULE, PreflightContext, analyze


def _preflight(gh, repo: Path, event, run_id: str, base: str) -> dict:
    workflows, tree = _load(repo / ".github/workflows", gh, event.default_branch)
    context = PreflightContext(
        event.default_branch,
        token_readonly=os.environ.get("AGENT_CI_DEFAULT_TOKEN_READONLY") == "true",
        safe_environments=frozenset(os.environ.get("AGENT_SAFE_ENVIRONMENTS", "").splitlines()),
        template_ref=os.environ["AGENT_TEMPLATE_REF"],
    )
    exposures = analyze(workflows, context)
    return {
        "schema_version": "1",
        "run_id": run_id,
        "base_sha": base,
        "workflow_tree_sha": tree,
        "pass": not any(item.rule != FIX_RULE for item in exposures),
        "exposures": [
            {**asdict(item), "secrets": list(item.secrets)}
            for item in exposures
            if item.rule != FIX_RULE
        ],
        "accepted_template_exposures": [
            {"rule": FIX_RULE, "job": item.job, "secret": "AGENT_FIX_KEY"}
            for item in exposures
            if item.rule == FIX_RULE
        ],
    }


def _read(path: Path, limit: int = 1_000_000) -> dict:
    if path.is_symlink() or path.stat().st_size > limit:
        raise Violation("Fix refused: invalid input file.")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: apply_fix <fix.json> <fix.patch> <work-path> <canary.json>", file=sys.stderr)
        return 2
    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "fix-summary.md"))
    try:
        token = os.environ["GH_TOKEN"]
        scan_keys = [
            value.strip() for value in os.environ["AGENT_SCAN_KEYS"].splitlines() if value.strip()
        ]
        bot = canonical_bot_login(os.environ["AGENT_BOT_LOGIN"])
        if not token or not scan_keys or not bot:
            raise Violation("Fix refused: scan configuration missing.")
        event = load_event(os.environ["GITHUB_EVENT_NAME"], Path(os.environ["GITHUB_EVENT_PATH"]))
        if event.name not in {"issues", "issue_comment"}:
            raise Violation("Fix refused: invalid event.")
        base = _sha(os.environ["FIX_BASE"])
        issue = int(os.environ["FIX_ISSUE"])
        if os.environ["FIX_REF"] != branch_for(issue):
            raise Violation("Fix refused: invalid branch.")
        gh = GitHub(token, event.repo)
        with tempfile.TemporaryDirectory(prefix="agent-fix-policy-") as directory:
            _, config, charter = _policy(gh, os.environ["GITHUB_SHA"], Path(directory))
        config.mode_for("fix")
        context = _read(Path(argv[3]), 4096)
        if (
            context.get("schema_version") != "1"
            or context.get("run_id") != os.environ["GITHUB_RUN_ID"]
            or context.get("base_sha") != base
            or re.fullmatch(r"[0-9a-f]{32}", context.get("canary", "")) is None
        ):
            raise Violation("Fix refused: trusted canary missing.")
        ctx = ApplyContext(
            event,
            config,
            charter,
            os.environ["GITHUB_RUN_ID"],
            base,
            [*scan_keys, token],
            context["canary"],
            False,
            github=gh,
            bot_login=bot,
        )
        artifact = _read(Path(argv[0]))
        patch_path = Path(argv[1])
        patch = (
            patch_path.read_bytes().decode("utf-8", "surrogateescape")
            if patch_path.exists()
            else None
        )
        preflight = _preflight(gh, Path(argv[2]), event, ctx.run_id, base)
        (Path(argv[0]).parent / "preflight.json").write_text(json.dumps(preflight) + "\n")
        result = apply_fix(
            artifact,
            patch,
            Path(argv[2]),
            ctx,
            preflight,
            issue=issue,
            feedback_head=os.environ.get("FIX_HEAD", ""),
            feedback_ref=os.environ["FIX_REF"] if os.environ.get("FIX_HEAD") else "",
        )
        with summary.open("a") as stream:
            stream.write(result + "\n")
        print(result)
        return 0
    except Exception:
        with summary.open("a") as stream:
            stream.write(
                "Fix refused: trusted inputs or validation unavailable; no further writes.\n"
            )
        print("Fix refused: trusted inputs or validation unavailable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
