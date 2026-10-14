import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from agent_repo.commands.triage_merge import merge
from agent_repo.prompting import extract_json, render_prompt
from agent_repo.schema import load_schema, validate


def main() -> int:
    decision = None
    tier2 = None
    error = None
    try:
        decision = json.loads(Path(os.environ["DECISION_FILE"]).read_text())
        schema = load_schema("decision")
        if validate(decision, schema):
            raise ValueError("invalid input")
        context = json.loads(Path(os.environ["CONTEXT_FILE"]).read_text())
        required = {"charter", "areas", "labels", "contributing", "docs_index"}
        if (
            not isinstance(context, dict)
            or set(context) != required
            or any(not isinstance(v, str) for v in context.values())
        ):
            raise ValueError("invalid context")
        agent = Path(os.environ["TRIAGE_AGENT"])
        instructions = (agent / "prompt.md").read_text()
        blocks = dict(
            context,
            issue=Path(os.environ["ISSUE_FILE"]).read_text(),
            tier_one=json.dumps(decision),
            schema=json.dumps(schema),
        )
        template = instructions + "\nPrivate canary: {{canary}}\n"
        template += "Run: {{context:run_id}}\nBase: {{context:base_sha}}\n"
        template += "\n".join("{{block:" + key + "}}" for key in blocks)
        canary = secrets.token_hex(24)
        prompt = render_prompt(
            template,
            canary=canary,
            blocks=blocks,
            context={"run_id": decision["run_id"], "base_sha": decision["base_sha"]},
        )
        key_env = os.environ.get("API_KEY_ENV", "LLM_API_KEY")
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env) is None:
            raise ValueError("invalid key environment")
        key = os.environ[key_env]
        if not key:
            raise ValueError("missing key")
        child_env = dict(os.environ, LLM_API_KEY=key)
        # Buffer output in memory so a leaked key is never persisted or logged.
        run = subprocess.run(
            [
                "timeout",
                "900",
                "omnigent",
                "run",
                str(agent),
                "--no-session",
                "--no-log",
                "-p",
                prompt,
            ],
            capture_output=True,
            env=child_env,
            timeout=915,
        )
        combined = run.stdout + run.stderr
        leak = subprocess.run(["grep", "-qF", "--", key], input=combined, capture_output=True)
        if leak.returncode != 1 or canary.encode() in combined or b"LLM_API_KEY" in combined:
            raise ValueError("unsafe output")
        if run.returncode != 0:
            raise ValueError("runner failed")
        tier2 = extract_json(run.stdout.decode("utf-8"))
        decoded = json.dumps(tier2, ensure_ascii=False)
        if key in decoded or canary in decoded or "LLM_API_KEY" in decoded:
            raise ValueError("unsafe decoded output")
        if validate(tier2, schema) or any(tier2[k] != decision[k] for k in ["run_id", "base_sha"]):
            raise ValueError("invalid output")
    except (KeyError, OSError, ValueError, subprocess.SubprocessError, RecursionError):
        error = "Tier-2 triage failed."
    if decision is None or validate(decision, load_schema("decision")):
        print("invalid Tier-1 input", file=sys.stderr)
        return 1
    try:
        result = merge(decision, tier2, error)
        encoded = json.dumps(result, indent=2) + "\n"
        output = Path(os.environ["OUTPUT_FILE"])
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists() or output.read_text() != encoded:
            output.write_text(encoded)
    except (KeyError, OSError, ValueError):
        print("cannot write triage result", file=sys.stderr)
        return 1
    if error:
        print("Tier-2 triage failed; maintainer escalation recorded.", file=sys.stderr)
        return 1
    print("Tier-2 triage validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
