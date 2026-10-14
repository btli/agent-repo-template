import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from agent_repo.github import GitHub, GitHubError
from agent_repo.preflight import PreflightContext, analyze
from agent_repo.yamljson import YamlError, load_yaml


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("invalid Git object identity")
    return value


def _load(workflows_dir: Path, api: GitHub, branch: str) -> tuple[dict[str, object], str]:
    ref = api.request("GET", f"git/ref/heads/{quote(branch, safe='')}")
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        raise ValueError("invalid default branch identity")
    commit = _sha(ref["object"].get("sha"))
    directory = api.request(
        "GET",
        "contents/.github/workflows",
        params={"ref": commit},
        accept="application/vnd.github.object+json",
    )
    if not isinstance(directory, dict) or directory.get("type") != "dir":
        raise ValueError("invalid workflow directory")
    tree_sha = _sha(directory.get("sha"))
    entries = directory.get("entries")
    if not isinstance(entries, list) or len(entries) >= 1000:
        raise ValueError("incomplete workflow directory")
    expected = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("invalid workflow entry")
        name = entry["name"]
        if not name.endswith((".yml", ".yaml")):
            continue
        if "/" in name or entry.get("type") != "file" or name in expected:
            raise ValueError("invalid workflow file")
        expected[name] = _sha(entry.get("sha"))
    if not workflows_dir.is_dir():
        raise ValueError("workflow directory missing")
    local = {
        path.name: path for path in workflows_dir.iterdir() if path.name.endswith((".yml", ".yaml"))
    }
    if set(local) != set(expected):
        raise ValueError("workflow files do not match default branch")
    workflows = {}
    for name, path in sorted(local.items()):
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid local workflow file")
        data = path.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if blob != expected[name]:
            raise ValueError("workflow contents do not match default branch")
        workflows[name] = load_yaml(path)
    return workflows, tree_sha


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="preflight")
    parser.add_argument("--workflows-dir", type=Path, default=Path(".github/workflows"))
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--agent-ref-glob", default="agent/fix-*")
    parser.add_argument("--token-readonly", action="store_true")
    parser.add_argument("--safe-environment", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("preflight.json"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--base-sha", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    artifact = {
        "run_id": args.run_id,
        "base_sha": args.base_sha,
        "workflow_tree_sha": None,
        "exposures": [],
        "pass": False,
    }
    result = 0
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if not token or not repo:
            raise ValueError("GitHub credentials or repository missing")
        workflows, tree_sha = _load(args.workflows_dir, GitHub(token, repo), args.default_branch)
        context = PreflightContext(
            args.default_branch,
            args.agent_ref_glob,
            args.token_readonly,
            frozenset(args.safe_environment),
        )
        exposures = analyze(workflows, context)
        artifact["workflow_tree_sha"] = tree_sha
        artifact["exposures"] = [asdict(item) for item in exposures]
        artifact["pass"] = not exposures
    except (GitHubError, YamlError, ValueError, OSError, TypeError):
        print("Preflight could not verify consumer workflows.", file=sys.stderr)
        result = 1
    try:
        serialized = json.dumps(artifact, indent=2) + "\n"
        if not args.output.exists() or args.output.read_text() != serialized:
            args.output.write_text(serialized)
        output = f"pass={str(artifact['pass']).lower()}\n"
        if github_output := os.environ.get("GITHUB_OUTPUT"):
            with Path(github_output).open("a") as handle:
                handle.write(output)
        print(output, end="")
    except OSError:
        print("Preflight could not write its result.", file=sys.stderr)
        return 1
    return result
