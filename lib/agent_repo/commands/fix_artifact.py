import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agent_repo.schema import load_schema, validate

MAX_PATCH_BYTES = 1_000_000
DIFF_OPTIONS = ("--binary", "--full-index", "--no-ext-diff", "--no-textconv")


class PatchTooLarge(ValueError):
    """The patch is over MAX_PATCH_BYTES, so callers can report it as a size refusal."""


def git_environment() -> dict[str, str]:
    # Global and system config are ignored so runner-wide settings cannot change any git result.
    return {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }


def run_git(repo: Path, git_dir: Path, *args: str) -> bytes:
    command = ["git", f"--git-dir={git_dir}", f"--work-tree={repo}", "-c", "core.fsmonitor=false"]
    return subprocess.run(
        [*command, "-c", "core.quotepath=off", *args],
        cwd=repo,
        env=git_environment(),
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    ).stdout


def local_config(repo: Path, git_dir: Path) -> list[str]:
    # Every repository scope, including config.worktree; run_git blanks global and system.
    entries = run_git(repo, git_dir, "config", "--includes", "--list", "-z")
    # Strict decoding: config that cannot be decoded is refused rather than half-read.
    return entries.decode().split("\0")


def refuse_repository_commands(repo: Path, git_dir: Path) -> None:
    # A nested repository's own config could run filters while the parent diff recurses into it.
    pending = [repo]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                # Only the git dir in use is exempt; the agent step moves it out of the tree.
                if Path(entry.path) == git_dir:
                    continue
                # Case-insensitive filesystems resolve .GIT and other variants as .git.
                if entry.name.casefold() == ".git":
                    raise ValueError("work tree contains git metadata")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
    # Clean and process filters are the commands git still runs during add and diff.
    if any(entry.startswith("filter.") for entry in local_config(repo, git_dir)):
        raise ValueError("git config defines filters")


def build_artifact(
    repo: Path, base_sha: str, run_id: str, agent_json: dict, *, git_dir: Path | None = None
) -> tuple[dict, str | None]:
    if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None or not run_id:
        raise ValueError("invalid run identity")
    if validate(agent_json, load_schema("fix")):
        raise ValueError("fix output does not match the schema")
    if agent_json["run_id"] != run_id or agent_json["base_sha"] != base_sha:
        raise ValueError("fix output does not echo the run identity")
    artifact = dict(agent_json)
    if artifact["status"] == "no_patch":
        if not artifact["attempted"].strip():
            raise ValueError("no_patch requires attempted")
        return artifact, None
    git_dir = git_dir or repo / ".git"
    try:
        refuse_repository_commands(repo, git_dir)
        # Intent-to-add makes untracked, non-ignored files part of the diff against base.
        run_git(repo, git_dir, "add", "--intent-to-add", "--", ".")
        patch = run_git(repo, git_dir, "diff", *DIFF_OPTIONS, base_sha)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot build patch") from exc
    if not patch:
        raise ValueError("patch status without changes")
    if len(patch) > MAX_PATCH_BYTES:
        raise PatchTooLarge("patch too large")
    gitlink = rb"^(?:(?:new|deleted) file mode|old mode|new mode|index \S+) 160000$"
    if re.search(gitlink, patch, re.M):
        raise ValueError("patch contains a gitlink")
    return artifact, patch.decode("utf-8", "surrogateescape")


def write_artifact(output: Path, artifact: dict, patch: str | None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "fix.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    if patch is not None:
        (output / "fix.patch").write_bytes(patch.encode("utf-8", "surrogateescape"))


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        usage = "usage: fix_artifact <repo> <base-sha> <run-id> <agent.json> <output-dir>"
        print(usage, file=sys.stderr)
        return 2
    try:
        agent_json = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
        artifact, patch = build_artifact(Path(argv[0]), argv[1], argv[2], agent_json)
        write_artifact(Path(argv[4]), artifact, patch)
    except (OSError, ValueError, RecursionError):
        print("fix artifact failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
