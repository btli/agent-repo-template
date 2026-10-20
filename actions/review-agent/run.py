import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_repo.policies import agent_environment
from agent_repo.prompting import extract_json, render_prompt
from agent_repo.scan import find_leaks
from agent_repo.schema import load_schema, validate


def _write_review(output: Path, value: dict) -> None:
    encoded = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8")


def _summary_review(run_id: str, base_sha: str, head_sha: str, reason: str) -> dict:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "summary": f"Automated review skipped. {reason}",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": {
            "to": "maintainer",
            "reason": reason[:500] or "Review needs a maintainer.",
        },
    }


def main() -> int:
    output = Path(os.environ.get("OUTPUT_FILE", "review.json"))
    key_env = os.environ.get("API_KEY_ENV", "LLM_API_KEY")
    canary = secrets.token_hex(24)
    try:
        run_id = os.environ["RUN_ID"]
        base_sha = os.environ["BASE_SHA"]
        head_sha = os.environ["HEAD_SHA"]
        if (
            not run_id
            or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None
            or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None
        ):
            raise ValueError
        schema = load_schema("review")
        reason = os.environ.get("SUMMARY_REASON", "")[:4000]
        secrets_to_scan = [
            value
            for value in [
                os.environ.get(key_env, ""),
                *os.environ.get("AGENT_SCAN_KEYS", "").splitlines(),
            ]
            if value
        ]
        if os.environ.get("SUMMARY_ONLY") == "true":
            result = _summary_review(run_id, base_sha, head_sha, reason)
            if validate(result, schema) or find_leaks(
                json.dumps(result, ensure_ascii=False), secrets_to_scan, canary
            ):
                raise ValueError
            _write_review(output, result)
            print("Summary-only review artifact written.")
            return 0

        files_text = Path(os.environ["FILES_FILE"]).read_text(encoding="utf-8")
        files = json.loads(files_text)
        if not isinstance(files, list) or len(files_text.encode()) > 120 * 1024:
            raise ValueError
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env) is None:
            raise ValueError
        key = os.environ[key_env]
        if not key:
            raise ValueError
        blocks = {"files": files_text, "schema": json.dumps(schema, sort_keys=True)}
        prompt = render_prompt(
            (Path(os.environ["REVIEW_AGENT"]) / "prompt.md").read_text(encoding="utf-8")
            + "\nPrivate canary: {{canary}}\nRun: {{context:run_id}}\n"
            + "Base: {{context:base_sha}}\nHead: {{context:head_sha}}\n"
            + "{{block:schema}}\n{{block:files}}",
            canary=canary,
            blocks=blocks,
            context={"run_id": run_id, "base_sha": base_sha, "head_sha": head_sha},
        )
        if len(prompt.encode()) > 160 * 1024:
            raise ValueError
        with tempfile.TemporaryDirectory(prefix="agent-review-") as directory:
            budget = Path(directory) / "budget"
            budget.write_text("0\n", encoding="utf-8")
            budget.chmod(0o600)
            child_env = agent_environment(
                dict(os.environ, LLM_API_KEY=key, AGENT_REPO_BUDGET_FILE=str(budget))
            )
            result = subprocess.run(
                [
                    "timeout",
                    "-k",
                    "15",
                    "900",
                    "omnigent",
                    "run",
                    os.environ["REVIEW_AGENT"],
                    "--no-session",
                    "--no-log",
                    "-p",
                    prompt,
                ],
                env=child_env,
                capture_output=True,
                timeout=930,
                check=False,
            )
        secrets_to_scan = [*secrets_to_scan, key]
        output_text = result.stdout.decode("utf-8", errors="replace")
        combined = output_text + result.stderr.decode("utf-8", errors="replace")
        output_leaks = find_leaks(combined, secrets_to_scan, canary)
        if result.returncode != 0 or (output_leaks and output_leaks != ["scan_budget"]):
            raise ValueError
        artifact = extract_json(output_text)
        if (
            validate(artifact, schema)
            or artifact["run_id"] != run_id
            or artifact["base_sha"] != base_sha
            or artifact["head_sha"] != head_sha
        ):
            raise ValueError
        artifact_leaks = find_leaks(
            json.dumps(artifact, ensure_ascii=False), secrets_to_scan, canary
        )
        if artifact_leaks and artifact_leaks != ["scan_budget"]:
            raise ValueError
        if output_leaks == ["scan_budget"] or artifact_leaks == ["scan_budget"]:
            reason = "Review response exceeded the outbound scan budget; needs-maintainer."
            handoff = _summary_review(run_id, base_sha, head_sha, reason)
            if validate(handoff, schema) or find_leaks(
                json.dumps(handoff, ensure_ascii=False), secrets_to_scan, canary
            ):
                raise ValueError
            _write_review(output, handoff)
            print("Summary-only review artifact written.")
            return 0
        _write_review(output, artifact)
        print("Review artifact validated.")
        return 0
    except (KeyError, OSError, ValueError, TypeError, subprocess.SubprocessError, RecursionError):
        output.unlink(missing_ok=True)
        print("Review agent failed safely; no artifact was produced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
