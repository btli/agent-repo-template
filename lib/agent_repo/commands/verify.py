import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from agent_repo.apply.actions import canonical_bot_login
from agent_repo.apply.fix import AGENT_MESSAGE, Violation, _git, _sha, test_files
from agent_repo.commands.fix_artifact import git_environment
from agent_repo.config import load_config


def _eligible(payload: dict) -> bool:
    pull = payload["pull_request"]
    repository = payload["repository"]
    bot = canonical_bot_login(os.environ.get("AGENT_BOT_LOGIN", ""))
    return bool(
        bot
        and pull["user"]["type"] == "Bot"
        and canonical_bot_login(pull["user"]["login"]) == bot
        and pull["head"]["repo"]["full_name"] == repository["full_name"]
        and re.fullmatch(r"agent/fix-[1-9][0-9]*", pull["head"]["ref"])
        and pull["base"]["ref"] == repository["default_branch"]
    )


def _copy_test(repo: Path, head: str, path: str, destination: Path, *, required: bool) -> None:
    entry = _git(repo, "ls-tree", "-z", head, "--", path)
    if not entry:
        if required:
            raise Violation("Verify refused: test file missing.")
        return
    metadata = entry.partition(b"\t")[0].split()
    if metadata[0] != b"100644" or metadata[1] != b"blob":
        raise Violation("Verify refused: unsafe test file.")
    target = destination / path
    # A base symlink must not redirect overlay writes outside the disposable checkout.
    if target.is_symlink() or any(
        parent.is_symlink() for parent in target.parents if parent.is_relative_to(destination)
    ):
        raise Violation("Verify refused: unsafe test destination.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_git(repo, "cat-file", "blob", metadata[2].decode()))


def _collected(report: Path, ids: list[str]) -> bool:
    root = ET.parse(report).getroot()
    cases = root.findall(".//testcase")
    observed = {(case.get("classname"), case.get("name")) for case in cases}
    expected = set()
    for node in ids:
        path, *components = node.split("::")
        expected.add((".".join([path[:-3].replace("/", "."), *components[:-1]]), components[-1]))
    return expected <= observed and not root.findall(".//error")


def _run(
    repo: Path, commit: str, command: list[str], ids: list[str], *, red_base: str | None = None
) -> bool:
    with tempfile.TemporaryDirectory(prefix="agent-verify-") as directory:
        scratch = Path(directory).resolve()
        checkout = scratch / "work"
        _git(
            repo,
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            str(repo.resolve()),
            str(checkout),
        )
        _git(checkout, "checkout", "--quiet", "--detach", red_base or commit)
        required = test_files(ids)
        if red_base:
            files = set(required)
            for path in required:
                for parent in (PurePosixPath(path).parent, *PurePosixPath(path).parents):
                    files.add(str(parent / "conftest.py"))
            for path in sorted(files):
                _copy_test(repo, commit, path, checkout, required=path in required)
        else:
            # Validate the same files for the green run before any head code executes.
            for path in required:
                _copy_test(repo, commit, path, checkout, required=True)
        report = scratch / "junit.xml"
        environment = git_environment()
        environment.update(HOME=str(scratch), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(
            [*command, "--junitxml=" + str(report), "--", *ids],
            cwd=checkout,
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=600,
        )
        return result.returncode == (1 if red_base else 0) and _collected(report, ids)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: verify <work-path> <policy-path>", file=sys.stderr)
        return 2
    try:
        if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
            raise Violation("Verify refused: invalid event.")
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        if not _eligible(payload):
            print("Verify skipped: ineligible pull request.")
            return 0
        repo = Path(argv[0]).resolve(strict=True)
        head = _sha(payload["pull_request"]["head"]["sha"])
        if _git(repo, "rev-parse", "HEAD").decode().strip() != head:
            raise Violation("Verify refused: checkout identity mismatch.")
        message = _git(repo, "show", "-s", "--format=%B", head).decode()
        match = AGENT_MESSAGE.fullmatch(message.rstrip("\n"))
        if not match:
            raise Violation("Verify refused: missing agent trailers.")
        base = match["base"]
        ids = match["tests"].split()
        test_files(ids)
        _git(repo, "merge-base", "--is-ancestor", base, head)
        command = shlex.split(load_config(Path(argv[1]) / ".agents").raw["commands"]["test"])
        if not command:
            raise Violation("Verify refused: test command missing.")
        if not _run(repo, head, command, ids, red_base=base):
            raise Violation("Verify refused: red reproduction not proven.")
        if not _run(repo, head, command, ids):
            raise Violation("Verify refused: green result not proven.")
        print("Red reproduced at Agent-Base; green verified at head.")
        return 0
    except Exception:
        # Test processes and repository files may contain credentials or arbitrary model text.
        print("Verify failed: reproduction or trusted inputs unavailable.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
