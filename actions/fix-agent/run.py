import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_repo.commands.fix_artifact import (
    build_artifact,
    local_config,
    refuse_repository_commands,
    run_git,
    write_artifact,
)
from agent_repo.commands.fix_spec import render_spec
from agent_repo.config import ConfigError, load_config
from agent_repo.policies import agent_environment
from agent_repo.prompting import extract_json, render_prompt
from agent_repo.scan import find_leaks
from agent_repo.schema import load_schema
from agent_repo.yamljson import YamlError

ACTION = Path(__file__).resolve().parent
TEMPLATE = ACTION.parents[1] / "agents" / "fix"


class Refused(Exception):
    """A fixed, credential-free reason why no fix artifact was produced."""


def checkouts() -> tuple[Path, Path]:
    work = Path(os.environ["WORK_PATH"]).resolve(strict=True)
    policy = Path(os.environ["POLICY_PATH"]).resolve(strict=True)
    # Charter and config come from a separate default-branch checkout the agent cannot edit.
    if policy.is_relative_to(work) or work.is_relative_to(policy):
        raise Refused("policy checkout must be separate from the work tree")
    return work, policy


def refuse_persisted_credentials(tree: Path, git_dir: Path) -> None:
    # Read before any move so includeIf.gitdir still matches; includes are refused outright.
    pattern = r"http(\..*)?\.extraheader|credential\..*|include\.path|includeif\..*\.path"
    for entry in local_config(tree, git_dir):
        name = entry.partition("\n")[0]
        # A token can also sit in URL userinfo, in a value or in an insteadOf key name.
        if re.fullmatch(pattern, name) or re.search(r"://[^/]*@", entry):
            raise Refused("checkout persisted credentials; use persist-credentials: false")


def setup() -> int:
    try:
        work, policy = checkouts()
        # Setup runs unsandboxed, so a persisted token must be refused before it starts.
        refuse_persisted_credentials(work, work / ".git")
        if (policy / ".git").exists():
            refuse_persisted_credentials(policy, policy / ".git")
        command = load_config(policy / ".agents").raw["commands"]["setup"]
    except Refused as exc:
        print(f"Project setup refused: {exc}.", file=sys.stderr)
        return 1
    except (KeyError, OSError, ConfigError, subprocess.SubprocessError):
        print("Project setup failed: trusted configuration unavailable.", file=sys.stderr)
        return 1
    if not command.strip():
        print("No project setup command configured.")
        return 0
    # The allowlist drops the key and runner file-command paths; a fresh HOME hides user state.
    env = agent_environment(dict(os.environ), tool=True)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="agent-setup-home-") as home:
        env["HOME"] = home
        return subprocess.run(
            ["bash", "-c", command], cwd=work, env=env, stdin=subprocess.DEVNULL
        ).returncode


def read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def run_agent(output: Path, canary: str) -> str:
    work, policy = checkouts()
    base_sha = os.environ["BASE_SHA"]
    run_id = os.environ["RUN_ID"]
    key_env = os.environ.get("API_KEY_ENV", "LLM_API_KEY")
    limit = int(os.environ.get("AGENT_TIMEOUT", "2400"))
    if (
        re.fullmatch(r"[0-9a-f]{40}", base_sha) is None
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", run_id) is None
        or re.fullmatch(r"[A-Z_][A-Z0-9_]*", key_env) is None
        or not 60 <= limit <= 21600
    ):
        raise Refused("invalid inputs")
    key = os.environ.get(key_env, "")
    if not key:
        raise Refused("missing gateway key")
    temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if temp.is_relative_to(work) or output.is_relative_to(work):
        raise Refused("runner temp and output must be outside the work tree")
    preflight = subprocess.run(
        ["bash", str(ACTION / "preflight.sh")],
        env={"PATH": os.environ["PATH"]},
        stdin=subprocess.DEVNULL,
    )
    if preflight.returncode != 0:
        raise Refused("bwrap sandbox unavailable")
    if (policy / ".git").exists():
        refuse_persisted_credentials(policy, policy / ".git")
    config = load_config(policy / ".agents")
    scratch = Path(tempfile.mkdtemp(prefix="agent-fix-", dir=temp))
    dot_git = work / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir():
        raise Refused("work tree must be a plain git checkout")
    refuse_persisted_credentials(work, dot_git)
    head = subprocess.run(
        ["git", f"--git-dir={dot_git}", "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if head.returncode != 0 or head.stdout.strip() != base_sha:
        raise Refused("work tree is not at the base SHA")
    # Status hashes modified files, so filters and nested repositories are refused first.
    try:
        refuse_repository_commands(work, dot_git)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    # Setup output that is not gitignored would otherwise land in the patch as agent work.
    if run_git(work, dot_git, "status", "--porcelain", "-z", "--untracked-files=all"):
        raise Refused("work tree is not clean after setup; gitignore setup outputs")
    # Git metadata leaves the sandbox root so the agent cannot plant config the diff would run.
    git_dir = scratch / "git"
    shutil.move(dot_git, git_dir)
    spec = render_spec(TEMPLATE, config, scratch / "agent")
    settings = {
        "commands": {name: config.raw["commands"][name] for name in ("test", "lint")},
        "fix_max_diff_lines": config.raw["limits"]["fix_max_diff_lines"],
        "allow_new_deps": config.raw["fix"]["allow_new_deps"],
    }
    blocks = {
        "issue": Path(os.environ["ISSUE_FILE"]).read_text(encoding="utf-8"),
        "charter": (policy / ".agents" / "charter.md").read_text(encoding="utf-8"),
        "agents_md": read_optional(policy / "AGENTS.md"),
        "contributing": read_optional(policy / "CONTRIBUTING.md"),
        "configuration": json.dumps(settings),
        "schema": json.dumps(load_schema("fix")),
    }
    template = "Private canary: {{canary}}\nRun: {{context:run_id}}\nBase: {{context:base_sha}}\n"
    template += "\n".join("{{block:" + name + "}}" for name in blocks)
    prompt = render_prompt(
        template, canary=canary, blocks=blocks, context={"run_id": run_id, "base_sha": base_sha}
    )
    if key in prompt:
        raise Refused("credential in prompt")
    if len(prompt.encode()) > 120 * 1024:
        raise Refused("prompt byte cap exceeded")
    command = ["timeout", "-k", "15", str(limit), "omnigent", "run", str(spec)]
    try:
        run = subprocess.run(
            [*command, "--no-session", "--no-log", "-p", prompt],
            cwd=work,
            env=agent_environment(dict(os.environ, LLM_API_KEY=key)),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=limit + 30,
        )
    except subprocess.TimeoutExpired:
        raise Refused("agent run timed out") from None
    if run.returncode != 0:
        raise Refused("agent run failed")
    try:
        answer = extract_json(run.stdout.decode("utf-8"))
        artifact, patch = build_artifact(work, base_sha, run_id, answer, git_dir=git_dir)
    except ValueError:
        raise Refused("agent output rejected") from None
    text = json.dumps(artifact, ensure_ascii=False) + (patch or "")
    # Separator-aware scan; output too large to scan fails closed as well.
    leaks = find_leaks(text, [key], canary)
    if leaks == ["scan_budget"]:
        raise Refused("agent output exceeds the outbound scan budget")
    if leaks:
        raise Refused("credential or canary in agent output")
    write_artifact(output, artifact, patch)
    return artifact["status"]


def agent() -> int:
    try:
        output = Path(os.environ["OUTPUT_DIR"]).resolve()
        for name in ("fix.json", "fix.patch"):
            (output / name).unlink(missing_ok=True)
        status = run_agent(output, secrets.token_hex(24))
    except Refused as exc:
        print(f"Fix agent refused: {exc}.", file=sys.stderr)
        return 1
    except (
        KeyError,
        TypeError,
        OSError,
        ValueError,
        ConfigError,
        YamlError,
        subprocess.SubprocessError,
    ):
        # Environment and process errors may carry credentials; publish fixed text.
        print("Fix agent failed: trusted inputs unavailable.", file=sys.stderr)
        return 1
    print(f"Fix agent produced fix.json with status {status}.")
    return 0


if __name__ == "__main__":
    modes = {"setup": setup, "agent": agent}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        print("usage: run.py setup|agent", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(modes[sys.argv[1]]())
