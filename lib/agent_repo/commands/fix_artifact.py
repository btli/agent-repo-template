import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agent_repo.schema import load_schema, validate

MAX_PATCH_BYTES = 1_000_000
DIFF_OPTIONS = ("--binary", "--full-index", "--no-ext-diff", "--no-textconv")


def _git(repo: Path, git_dir: Path, *args: str) -> bytes:
    # Global and system config are ignored so runner-wide filters cannot change the patch.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    command = ["git", f"--git-dir={git_dir}", f"--work-tree={repo}", "-c", "core.fsmonitor=false"]
    return subprocess.run(
        [*command, "-c", "core.quotepath=off", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    ).stdout


def build_artifact(
    repo: Path, base_sha: str, run_id: str, agent_json: dict, *, git_dir: Path | None = None
) -> tuple[dict, str | None]:
    if re.fullmatch(r"[0-9a-f]{40}", base_sha) is None or not run_id:
        raise ValueError("invalid run identity")
    if not isinstance(agent_json, dict) or validate(agent_json, load_schema("fix")):
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
        # Intent-to-add makes untracked, non-ignored files part of the diff against base.
        _git(repo, git_dir, "add", "--intent-to-add", "--", ".")
        patch = _git(repo, git_dir, "diff", *DIFF_OPTIONS, base_sha)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot build patch") from exc
    if not patch:
        raise ValueError("patch status without changes")
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("patch too large")
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
