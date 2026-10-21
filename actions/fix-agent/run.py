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
    PatchTooLarge,
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
    ".git/config",
    ".git/config.worktree",
    ".agents",
    ".agents/config.yml",
    ".agents/areas.yml",
    ".agents/labels.yml",
    ".agents/charter.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
)
# Config keys that make git run a command; after setup, H's files could be that command.
COMMAND_CONFIG = (
    r"core\.(hookspath|fsmonitor)|filter\..*|diff\.external|diff\..*\.(textconv|command)"
    r"|merge\..*\.driver"
)
# Apply writes exactly this message, so no model text can add, drop or move a trailer.
AGENT_COMMIT = re.compile(
    r"Fix #[1-9][0-9]*\n\nAgent-Base: (?P<base>[0-9a-f]{40})\nAgent-Run: [A-Za-z0-9_.-]{1,64}\n"
    r"Agent-Tests:(?: [A-Za-z0-9_./:\[\]-]+){0,20}\n?"
)


class Refused(Exception):
    """A fixed, credential-free reason why no fix artifact was produced."""


def git(work: Path, git_dir: Path, *args: str) -> bytes:
    # Hooks stay off for every git command the action runs, whatever repository config says.
    return run_git(work, git_dir, "-c", f"core.hooksPath={os.devnull}", *args)


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
    command = ["git", f"--git-dir={git_dir}", "-c", f"core.hooksPath={os.devnull}", "rev-parse"]
    head = subprocess.run(
        [*command, "--verify", "HEAD^{commit}"],
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
    if git(work, git_dir, *status, *(["--ignored"] if ignored else [])):
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


def refuse_untrusted_checkouts(work: Path, policy: Path, expected_head: str) -> None:
    # Policy paths and both .git dirs are confirmed before any git command or policy read.
    refuse_escaping_policy_paths(policy)
    refuse_unplain_git_dir(work, "work tree")
    refuse_unplain_git_dir(policy, "policy checkout")
    # Git follows a linked config file anywhere, so even an in-checkout link is refused.
    if any((policy / ".git" / name).is_symlink() for name in ("config", "config.worktree")):
        raise Refused("policy checkout must be a plain git checkout")
    # A checkout at any other commit is refused before its repository config is inspected.
    refuse_unexpected_head(work / ".git", expected_head)
    refuse_persisted_credentials(policy, policy / ".git")
    refuse_persisted_credentials(work, work / ".git")


def feedback_inputs() -> tuple[str, str, str] | None:
    # Feedback names the prior agent commit H, its branch and the event commit checked out.
    head, ref = os.environ.get("FEEDBACK_HEAD_SHA", ""), os.environ.get("FEEDBACK_HEAD_REF", "")
    if not head and not ref:
        return None
    event = os.environ.get("GITHUB_SHA", "")
    if (
        re.fullmatch(r"[0-9a-f]{40}", head) is None
        or re.fullmatch(r"agent/fix-[1-9][0-9]*", ref) is None
        or re.fullmatch(r"[0-9a-f]{40}", event) is None
    ):
        raise Refused("invalid inputs")
    return head, ref, event


def is_ancestor(work: Path, git_dir: Path, ancestor: str, descendant: str) -> bool:
    try:
        git(work, git_dir, "merge-base", "--is-ancestor", ancestor, descendant)
    except subprocess.CalledProcessError:
        return False
    return True


def refuse_unverified_feedback(work: Path, base_sha: str, head: str, ref: str, event: str) -> None:
    git_dir = work / ".git"
    # A fetch-depth 0 checkout already holds every branch, so no token is needed to check H.
    if not is_ancestor(work, git_dir, head, f"refs/remotes/origin/{ref}"):
        raise Refused("feedback head is not on the agent branch")
    message = git(work, git_dir, "cat-file", "commit", head).partition(b"\n\n")[2]
    match = AGENT_COMMIT.fullmatch(message.decode("utf-8", "replace"))
    if match is None or match["base"] != base_sha:
        raise Refused("feedback head is not an agent commit on the base SHA")
    # Plain ancestry would accept an agent commit that reached the default branch by a merge.
    if base_sha.encode() not in git(work, git_dir, "rev-list", "--first-parent", event).split():
        raise Refused("feedback base is not on the default branch's first-parent chain")
    if not is_ancestor(work, git_dir, base_sha, head):
        raise Refused("feedback head does not descend from the base SHA")


def repository_config(work: Path, git_dir: Path) -> list[tuple[str, str]]:
    # Each repository-scope key with its scope; the action's own -c options are command scope.
    listing = git(work, git_dir, "config", "--includes", "--list", "-z", "--show-scope")
    fields = listing.decode().split("\0")
    pairs = zip(fields[0::2], fields[1::2], strict=False)
    return [(scope, entry.partition("\n")[0]) for scope, entry in pairs if scope != "command"]


def refuse_overlay_commands(work: Path) -> None:
    # Setup may have changed repository config or the tree, and H's content is agent-written.
    git_dir = work / ".git"
    if any(re.fullmatch(COMMAND_CONFIG, name) for _, name in repository_config(work, git_dir)):
        raise Refused("git config can run commands")
    try:
        refuse_repository_commands(work, git_dir)
    except ValueError as exc:
        raise Refused(str(exc)) from None


def refuse_setup_collisions(work: Path, base_sha: str, head: str) -> None:
    # read-tree overwrites ignored files, so any setup output on a path H changes is refused.
    git_dir = work / ".git"
    changed = git(work, git_dir, "diff", "--name-only", "-z", "--no-renames", base_sha, head)
    listing = git(work, git_dir, "ls-tree", "-r", "-t", "-z", base_sha).split(b"\0")
    # Each base path with its object type; a gitlink checks out as a directory, like a tree.
    tracked = {entry.partition(b"\t")[2]: entry.split(b" ")[1] for entry in listing if entry}
    for name in filter(None, changed.split(b"\0")):
        parts = name.split(b"/")
        for depth in range(1, len(parts) + 1):
            path = b"/".join(parts[:depth])
            entry = work / os.fsdecode(path)
            if not os.path.lexists(entry):
                continue
            # A link or file where the base tracks a directory, or the reverse, is setup output.
            is_directory = entry.is_dir() and not entry.is_symlink()
            if path not in tracked or (tracked[path] == b"blob") == is_directory:
                raise Refused("setup output conflicts with the feedback head")
        # H may replace a tracked directory with a file or link, deleting setup output inside it.
        top = work / os.fsdecode(name)
        if top.is_dir() and not top.is_symlink():
            for root, dirs, files in os.walk(top, onerror=raise_walk_error):
                below = (
                    os.fsencode((Path(root) / entry).relative_to(work)) for entry in dirs + files
                )
                if any(path not in tracked for path in below):
                    raise Refused("setup output conflicts with the feedback head")


def disable_artifact_hooks(work: Path, git_dir: Path) -> None:
    # The artifact's git commands take no -c options, so setup's config must not run anything.
    for scope, name in repository_config(work, git_dir):
        hooks = name == "core.hookspath"
        if (hooks and scope != "local") or (not hooks and re.fullmatch(COMMAND_CONFIG, name)):
            raise Refused("git config can run commands")
    # A husky-style local hooks path is kept off in the moved git dir, which the agent cannot reach.
    git(work, git_dir, "config", "--local", "core.hooksPath", os.devnull)
    # Read without the wrapper's -c option, the way the artifact's git commands see it.
    effective = run_git(work, git_dir, "config", "--get", "core.hooksPath").strip()
    if effective != os.fsencode(os.devnull):
        raise Refused("git config can run commands")


def move_head(work: Path, old: str, new: str) -> None:
    # A two-tree read-tree with hooks off; callers refuse filters and colliding setup output.
    git(work, work / ".git", "read-tree", "-m", "-u", old, new)
    git(work, work / ".git", "update-ref", "--no-deref", "HEAD", new)


def run_setup_command(work: Path, command: str) -> int:
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


def setup() -> int:
    try:
        work, policy = checkouts()
        # Setup trusts only base_sha code, so a checkout at any other commit never runs it.
        base_sha = os.environ.get("BASE_SHA", "")
        if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
            raise Refused("invalid inputs")
        feedback = feedback_inputs()
        # A feedback checkout starts at the event commit and moves to the base once H is verified.
        start = base_sha if feedback is None else feedback[2]
        # Setup runs unsandboxed, so a persisted token must be refused before it starts.
        refuse_untrusted_checkouts(work, policy, start)
        # An untracked, ignored or edited script would otherwise run as if it were base_sha code.
        reason = "work tree is not clean before setup"
        refuse_unclean_work_tree(work, work / ".git", reason, ignored=True)
        if feedback is not None:
            refuse_unverified_feedback(work, base_sha, *feedback)
            move_head(work, start, base_sha)
            refuse_unexpected_head(work / ".git", base_sha)
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
    elif code := run_setup_command(work, command):
        return code
    if feedback is None:
        return 0
    # The prior agent commit enters the tree only after setup has run the base's own code.
    try:
        refuse_overlay_commands(work)
        refuse_setup_collisions(work, base_sha, feedback[0])
        move_head(work, base_sha, feedback[0])
    except Refused as exc:
        print(f"Project setup refused: {exc}.", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Project setup refused: setup output conflicts with the feedback head.", file=sys.stderr
        )
        return 1
    return 0


def string_values(value: object) -> list[str]:
    # Decoded field values, since JSON escaping turns a newline separator into the letters \n.
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for item in value for text in string_values(item)]
    return []


def binary_file_bytes(work: Path, git_dir: Path, base_sha: str) -> list[bytes]:
    # Binary hunks are base85 in the patch, so those files' own bytes are scanned; text is in it.
    stats = git(work, git_dir, "diff", "--numstat", "-z", "--no-renames", base_sha)
    names = [stat.split(b"\t", 2)[2] for stat in stats.split(b"\0") if stat.startswith(b"-\t-\t")]
    # A binary patch also encodes the base pre-image, so deleted or replaced bytes are read too.
    base = set(git(work, git_dir, "ls-tree", "-r", "-z", "--name-only", base_sha).split(b"\0"))
    show = [f"{base_sha}:{os.fsdecode(name)}" for name in names if name in base]
    blobs = [git(work, git_dir, "cat-file", "blob", spec) for spec in show]
    paths = [work / os.fsdecode(name) for name in names]
    # A link's target is text in the patch; a linked file or parent dir could leave the tree.
    regular = [path for path in paths if path.is_file() and not path.is_symlink()]
    inside = [path for path in regular if path.resolve().is_relative_to(work)]
    # The patch encodes the blob git makes with the path's attributes, such as a re-encoding.
    hashed = [
        git(work, git_dir, "hash-object", "-w", f"--path={path.relative_to(work)}", str(path))
        for path in inside
    ]
    encoded = [git(work, git_dir, "cat-file", "blob", oid.strip().decode()) for oid in hashed]
    return blobs + [path.read_bytes() for path in inside] + encoded


def raise_walk_error(error: OSError) -> None:
    raise error


def attribute_files(work: Path) -> dict[str, tuple[str, str | bytes | None]]:
    # Every attributes file in the tree, ignored ones included, with its link target or bytes.
    found = {}
    # A directory the walk cannot list could hide an attributes file, so it fails closed.
    for root, dirs, files in os.walk(work, onerror=raise_walk_error):
        for name in dirs + files:
            if name.casefold() == ".gitattributes":
                path = Path(root) / name
                if path.is_symlink():
                    entry = ("link", os.readlink(path))
                else:
                    entry = ("file", path.read_bytes()) if path.is_file() else ("other", None)
                found[path.relative_to(work).as_posix()] = entry
    return found


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
    feedback = feedback_inputs()
    key = os.environ.get(key_env, "")
    if not key:
        raise Refused("missing gateway key")
    temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if temp.is_relative_to(work):
        raise Refused("runner temp and output must be outside the work tree")
    preflight = subprocess.run(
        ["bash", str(ACTION / "preflight.sh")],
        env={"PATH": os.environ["PATH"]},
        stdin=subprocess.DEVNULL,
    )
    if preflight.returncode != 0:
        raise Refused("bwrap sandbox unavailable")
    # Setup overlays a feedback tree with the prior agent commit, so HEAD is H rather than base.
    refuse_untrusted_checkouts(work, policy, base_sha if feedback is None else feedback[0])
    config = load_config(policy / ".agents")
    scratch = Path(tempfile.mkdtemp(prefix="agent-fix-", dir=temp))
    dot_git = work / ".git"
    # Setup output that is not gitignored would otherwise land in the patch as agent work.
    reason = "work tree is not clean after setup; gitignore setup outputs"
    refuse_unclean_work_tree(work, dot_git, reason, ignored=False)
    # Git metadata leaves the sandbox root so the agent cannot plant config the diff would run.
    git_dir = scratch / "git"
    shutil.move(dot_git, git_dir)
    disable_artifact_hooks(work, git_dir)
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
    # Attributes decide how git encodes shipped content, so the agent's changes are compared
    # with what the base tree and trusted setup left, such as files in installed dependencies.
    attributes = attribute_files(work)
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
    except PatchTooLarge:
        raise Refused("agent output exceeds the outbound scan budget") from None
    except ValueError:
        raise Refused("agent output rejected") from None
    # The patch is scanned on its own, so its documented limit is not shared with the fields.
    outputs = [(patch or "").encode("utf-8", "surrogateescape")]
    if patch is not None:
        outputs += binary_file_bytes(work, git_dir, base_sha)
    # The documented limit is in bytes, while the scan budget counts decoded characters.
    if any(len(data) > 64 * 1024 for data in outputs):
        raise Refused("agent output exceeds the outbound scan budget")
    texts = [json.dumps(artifact, ensure_ascii=False), *string_values(artifact)]
    texts += [data.decode("utf-8", "surrogateescape") for data in outputs]
    # Separator-aware scan; output too large to scan fails closed as well.
    leaks = sorted({leak for text in texts for leak in find_leaks(text, [key], canary)})
    if leaks == ["scan_budget"]:
        raise Refused("agent output exceeds the outbound scan budget")
    if leaks:
        raise Refused("credential or canary in agent output")
    # Checked for every answer, since a no-patch answer still leaves the edited tree behind.
    try:
        changed = attribute_files(work) != attributes
    except OSError:
        raise Refused("agent output rejected") from None
    if changed:
        raise Refused("agent output changes .gitattributes")
    write_artifact(output, artifact, patch)
    return artifact["status"]


def agent() -> int:
    try:
        output = Path(os.environ["OUTPUT_DIR"]).resolve()
        # Stale outputs are removed only once the directory is known to lie outside the work tree.
        if output.is_relative_to(Path(os.environ["WORK_PATH"]).resolve(strict=True)):
            raise Refused("runner temp and output must be outside the work tree")
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
