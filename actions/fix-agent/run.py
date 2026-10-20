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
    git_environment,
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
POLICY_FILES = (
    ".git",
    ".agents",
    ".agents/config.yml",
    ".agents/areas.yml",
    ".agents/labels.yml",
    ".agents/charter.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
)


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
    # Git lowercases section and key names, so camel-cased keys such as sshCommand match too.
    pattern = (
        r"http(\..*)?\.(extraheader|sslkey|sslcert)|core\.sshcommand|credential\..*"
        r"|include\.path|includeif\..*\.path"
    )
    for entry in local_config(tree, git_dir):
        name = entry.partition("\n")[0]
        # A token can also sit in URL userinfo, in a value or in an insteadOf key name.
        if re.fullmatch(pattern, name) or re.search(r"://[^/]*@", entry):
            raise Refused("checkout persisted credentials; use persist-credentials: false")


def refuse_unexpected_head(git_dir: Path, base_sha: str) -> None:
    # rev-parse reads only the git dir, not the work tree, so it may run before the filter guard.
    head = subprocess.run(
        ["git", f"--git-dir={git_dir}", "rev-parse", "--verify", "HEAD^{commit}"],
        env=git_environment(),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if head.returncode != 0 or head.stdout.strip() != base_sha:
        raise Refused("work tree is not at the base SHA")


def refuse_unclean_work_tree(work: Path, git_dir: Path, reason: str, *, ignored: bool) -> None:
    # Status hashes modified files, so filters and nested repositories are refused first.
    try:
        refuse_repository_commands(work, git_dir)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    status = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if run_git(work, git_dir, *status, *(["--ignored"] if ignored else [])):
        raise Refused(reason)


def refuse_escaping_policy_paths(policy: Path) -> None:
    # A symlink in the policy checkout could point config or charter at agent-branch files.
    for name in POLICY_FILES:
        if not (policy / name).resolve().is_relative_to(policy):
            raise Refused("policy file resolves outside the policy checkout")


def refuse_unplain_git_dir(tree: Path, label: str) -> None:
    # A symlink or gitfile would point git at metadata outside the checkout.
    dot_git = tree / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir():
        raise Refused(f"{label} must be a plain git checkout")


def refuse_untrusted_checkouts(work: Path, policy: Path) -> None:
    # Policy paths and both .git dirs are confirmed before any git command or policy read.
    refuse_escaping_policy_paths(policy)
    refuse_unplain_git_dir(work, "work tree")
    refuse_unplain_git_dir(policy, "policy checkout")
    refuse_persisted_credentials(policy, policy / ".git")
    refuse_persisted_credentials(work, work / ".git")


def setup() -> int:
    try:
        work, policy = checkouts()
        # Setup runs unsandboxed, so a persisted token must be refused before it starts.
        refuse_untrusted_checkouts(work, policy)
        # Setup trusts only base_sha code, so a checkout at any other commit never runs it.
        base_sha = os.environ.get("BASE_SHA", "")
        if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
            raise Refused("invalid inputs")
        refuse_unexpected_head(work / ".git", base_sha)
        # An untracked, ignored or edited script would otherwise run as if it were base_sha code.
        reason = "work tree is not clean before setup"
        refuse_unclean_work_tree(work, work / ".git", reason, ignored=True)
        command = load_config(policy / ".agents").raw["commands"]["setup"]
    except Refused as exc:
        print(f"Project setup refused: {exc}.", file=sys.stderr)
        return 1
    except (KeyError, OSError, ValueError, ConfigError, subprocess.SubprocessError):
        print("Project setup failed: trusted configuration unavailable.", file=sys.stderr)
        return 1
    if not command.strip():
        print("No project setup command configured.")
        return 0
    # The allowlist drops the key and runner file-command paths; a fresh HOME hides user state.
    env = agent_environment(dict(os.environ), tool=True)
    env.pop("PYTHONPATH", None)
    # The allowlist keeps every LC_* name, so a key setting such as LC_SECRET is dropped by name.
    for name in (os.environ.get("API_KEY_ENV", "LLM_API_KEY"), "LLM_API_KEY"):
        env.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="agent-setup-home-") as home:
        env["HOME"] = home
        return subprocess.run(
            ["bash", "-c", command], cwd=work, env=env, stdin=subprocess.DEVNULL
        ).returncode


def string_values(value: object) -> list[str]:
    # Decoded field values, since JSON escaping turns a newline separator into the letters \n.
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for item in value for text in string_values(item)]
    return []


def binary_file_texts(work: Path, git_dir: Path, base_sha: str) -> list[str]:
    # Binary hunks are base85 in the patch, so those files' own bytes are scanned; text is in it.
    stats = run_git(work, git_dir, "diff", "--numstat", "-z", "--no-renames", base_sha)
    names = [stat.split(b"\t", 2)[2] for stat in stats.split(b"\0") if stat.startswith(b"-\t-\t")]
    paths = [work / os.fsdecode(name) for name in names]
    # A symlink's target is already text in the patch; following it could leave the tree.
    files = [path for path in paths if path.is_file() and not path.is_symlink()]
    return [path.read_bytes().decode("utf-8", "surrogateescape") for path in files]


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
    refuse_untrusted_checkouts(work, policy)
    config = load_config(policy / ".agents")
    scratch = Path(tempfile.mkdtemp(prefix="agent-fix-", dir=temp))
    dot_git = work / ".git"
    refuse_unexpected_head(dot_git, base_sha)
    # Setup output that is not gitignored would otherwise land in the patch as agent work.
    reason = "work tree is not clean after setup; gitignore setup outputs"
    refuse_unclean_work_tree(work, dot_git, reason, ignored=False)
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
    texts = [json.dumps(artifact, ensure_ascii=False) + (patch or ""), *string_values(artifact)]
    if patch is not None:
        texts += binary_file_texts(work, git_dir, base_sha)
    # Separator-aware scan; output too large to scan fails closed as well.
    leaks = sorted({leak for text in texts for leak in find_leaks(text, [key], canary)})
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
