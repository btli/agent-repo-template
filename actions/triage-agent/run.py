import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_repo.commands.triage_merge import label_corrections, merge
from agent_repo.policies import agent_environment
from agent_repo.prompting import extract_json, render_prompt
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import YamlError, load_yaml


def context_taxonomy(context: dict[str, str], directory: Path) -> set[str]:
    values = {}
    for key in ["labels", "areas"]:
        path = directory / f"{key}.yaml"
        path.write_text(context[key])
        values[key] = load_yaml(path)
    labels = values["labels"]["labels"]
    areas = values["areas"]["areas"]
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError("invalid label taxonomy")
    if not isinstance(areas, list) or not all(
        isinstance(x, dict) and isinstance(x.get("name"), str) for x in areas
    ):
        raise ValueError("invalid area taxonomy")
    return {label for label in labels if label.startswith("type:")} | {
        "area:" + x["name"] for x in areas
    }


def main() -> int:
    decision = None
    tier2 = None
    error = None
    allowed_labels = None
    canary = secrets.token_hex(24)
    key_env = os.environ.get("API_KEY_ENV", "LLM_API_KEY")
    private = {
        value
        for name, value in os.environ.items()
        if value
        and (
            name == key_env
            or any(part in name.upper() for part in ["TOKEN", "KEY", "SECRET", "PASSWORD"])
        )
    }
    sensitive = private | {canary, "LLM_API_KEY"}

    def unsafe(text: str) -> bool:
        return any(
            value in text or json.dumps(value, ensure_ascii=False)[1:-1] in text
            for value in sensitive
        )

    def discard_output() -> None:
        try:
            Path(os.environ["OUTPUT_FILE"]).unlink(missing_ok=True)
        except (KeyError, OSError):
            pass

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
        workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
        temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
        if temp.is_relative_to(workspace):
            raise ValueError("unsafe runner temp")
        with tempfile.TemporaryDirectory(prefix="agent-triage-", dir=temp) as scratch:
            scratch = Path(scratch)
            if any(unsafe(context[key]) for key in ["labels", "areas"]):
                raise ValueError("unsafe taxonomy")
            allowed_labels = context_taxonomy(context, scratch)
            budget = scratch / "budget"
            budget.write_text("0\n")
            budget.chmod(0o600)
            agent = Path(os.environ["TRIAGE_AGENT"])
            blocks = dict(
                context,
                issue=Path(os.environ["ISSUE_FILE"]).read_text(),
                tier_one=json.dumps(decision),
                schema=json.dumps(schema),
            )
            for name, text in blocks.items():
                for value in private:
                    text = text.replace(value, "[redacted credential]")
                blocks[name] = text
            template = (agent / "prompt.md").read_text() + "\nPrivate canary: {{canary}}\n"
            template += "Run: {{context:run_id}}\nBase: {{context:base_sha}}\n"
            template += "\n".join("{{block:" + key + "}}" for key in blocks)
            prompt = render_prompt(
                template,
                canary=canary,
                blocks=blocks,
                context={"run_id": decision["run_id"], "base_sha": decision["base_sha"]},
            )
            if any(value in prompt for value in private):
                raise ValueError("unsafe prompt")
            if len(prompt.encode()) > 120 * 1024:
                raise ValueError("prompt byte cap exceeded")
            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env) is None:
                raise ValueError("invalid key environment")
            key = os.environ[key_env]
            if not key:
                raise ValueError("missing key")
            child_env = agent_environment(
                dict(os.environ, LLM_API_KEY=key, AGENT_REPO_BUDGET_FILE=str(budget))
            )
            # Scan buffered output before persisting any model-authored content.
            run = subprocess.run(
                [
                    "timeout",
                    "-k",
                    "15",
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
                timeout=930,
            )
            combined = (run.stdout + run.stderr).decode("utf-8")
            if unsafe(combined):
                raise ValueError("unsafe output")
            if run.returncode != 0:
                raise ValueError("runner failed")
            tier2 = extract_json(run.stdout.decode("utf-8"))
            if unsafe(json.dumps(tier2, ensure_ascii=False)):
                raise ValueError("unsafe decoded output")
            if validate(tier2, schema) or any(
                tier2[k] != decision[k] for k in ["run_id", "base_sha"]
            ):
                raise ValueError("invalid output")
            if label_corrections(tier2, allowed_labels) is None:
                raise ValueError("invalid label corrections")
    except (
        KeyError,
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
        RecursionError,
        YamlError,
    ):
        error = "Tier-2 triage failed."
    if decision is None or validate(decision, load_schema("decision")):
        discard_output()
        print("Invalid Tier-1 input; escalate to maintainer.", file=sys.stderr)
        return 1
    try:
        result = merge(decision, tier2, error, allowed_labels=allowed_labels)
        encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if unsafe(encoded):
            discard_output()
            print("Unsafe artifact refused; escalate to maintainer.", file=sys.stderr)
            return 1
        output = Path(os.environ["OUTPUT_FILE"])
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.exists() or output.read_text() != encoded:
            output.write_text(encoded)
    except (KeyError, OSError, ValueError):
        discard_output()
        print("Cannot write triage result; escalate to maintainer.", file=sys.stderr)
        return 1
    if error:
        print("Tier-2 triage failed; maintainer escalation recorded.", file=sys.stderr)
        return 1
    print("Tier-2 triage validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
