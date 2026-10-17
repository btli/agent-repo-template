import base64
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from agent_repo.commands.triage_merge import label_corrections, merge, taxonomy
from agent_repo.config import ConfigError
from agent_repo.policies import agent_environment
from agent_repo.prompting import extract_json, render_prompt
from agent_repo.schema import load_schema, validate


def run_bounded(command: list[str], *, env: dict[str, str], timeout: float):
    limit = 1024 * 1024
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    ) as process:
        try:
            stdout, stderr = bytearray(), bytearray()
            used = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in selector.select(remaining):
                        data = os.read(key.fd, min(65536, limit - used + 1))
                        if not data:
                            selector.unregister(key.fd)
                            continue
                        used += len(data)
                        if used > limit:
                            raise ValueError("runner output byte cap exceeded")
                        key.data.extend(data)
            process.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(
                command, process.returncode, bytes(stdout), bytes(stderr)
            )
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def context_taxonomy(context: dict[str, str], directory: Path) -> set[str]:
    for key in ["config", "labels", "areas"]:
        (directory / f"{key}.yml").write_text(context[key])
    return taxonomy(directory)


def main() -> int:
    decision = None
    tier2 = None
    error = None
    allowed_labels = None
    canary = secrets.token_hex(24)
    key_env = os.environ.get("API_KEY_ENV", "LLM_API_KEY")
    explicit = {
        key_env,
        "LLM_API_KEY",
        "GITHUB_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    }
    # Ignore incidental values under four characters, but always protect explicit credentials.
    private = {
        value
        for name, value in os.environ.items()
        if value
        and (
            name in explicit
            or (
                len(value) >= 4
                and not re.fullmatch(r"true|false|null|none|yes|no|on|off|[0-9.]+", value, re.I)
                and name.upper() != "SSH_AUTH_SOCK"
                and re.search(
                    r"(?:^|_)(?:PAT|AUTH|KEY|TOKEN|SECRET|PASSWORD|PASS|PASSWD|CREDENTIALS?|PRIVATE|DSN)(?:_|$)",
                    name,
                    re.IGNORECASE,
                )
            )
        )
    }
    ambiguous = False
    for value in os.environ.values():
        try:
            url = urlsplit(value)
        except ValueError:
            ambiguous |= "@" in value
            continue
        if url.username is not None:
            private.add(value)
            for part in [url.username, url.password]:
                if part is not None:
                    private.update(value for value in {part, unquote(part)} if len(value) >= 4)
    sensitive = set()
    for value in private | {canary, "LLM_API_KEY"}:
        raw = value.encode()
        # Trim partial edge sextets so embedded base64 matches at each byte alignment.
        for offset in (1, 2):
            start = (offset * 8 + 5) // 6
            stop = ((offset + len(raw)) * 8) // 6
            sensitive.update(
                encode(b"\0" * offset + raw).decode()[start:stop]
                for encode in (base64.b64encode, base64.urlsafe_b64encode)
            )
        sensitive.update(
            {
                value,
                base64.b64encode(raw).decode(),
                base64.b64encode(raw).decode().rstrip("="),
                base64.urlsafe_b64encode(raw).decode(),
                base64.urlsafe_b64encode(raw).decode().rstrip("="),
                raw.hex(),
                raw.hex().upper(),
                quote(value, safe=""),
                value[::-1],
            }
        )
    sensitive |= {json.dumps(value, ensure_ascii=False)[1:-1] for value in sensitive}
    ambiguous |= any(len(value) < 4 for value in sensitive)

    def unsafe(text: str) -> bool:
        return ambiguous or any(value in text for value in sensitive)

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
        required = {"config", "charter", "areas", "labels", "contributing", "docs_index"}
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
            if any(unsafe(context[key]) for key in ["config", "labels", "areas"]):
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
                for value in sorted(private, key=len, reverse=True):
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
            run = run_bounded(
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
        ConfigError,
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
