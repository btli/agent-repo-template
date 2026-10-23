import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from agent_repo.github import GitHub
from agent_repo.preflight import FIX_RULE, PreflightContext, analyze
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml


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
    if local.keys() != expected.keys():
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
    parser.add_argument("--template-ref")
    args = parser.parse_args(argv)
    artifact = {
        "schema_version": "1",
        "run_id": args.run_id,
        "base_sha": args.base_sha,
        "workflow_tree_sha": None,
        "exposures": [],
        "accepted_template_exposures": [],
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
            args.template_ref,
        )
        exposures = analyze(workflows, context)
        artifact["workflow_tree_sha"] = tree_sha
        for item in exposures:
            if item.rule == FIX_RULE:
                artifact["accepted_template_exposures"].append(
                    {"rule": item.rule, "job": item.job, "secret": "AGENT_FIX_KEY"}
                )
            else:
                artifact["exposures"].append({**asdict(item), "secrets": list(item.secrets)})
        artifact["pass"] = not artifact["exposures"]
    except Exception:
        print("Preflight could not verify consumer workflows.", file=sys.stderr)
        result = 1
    try:
        if validate(artifact, load_schema("preflight")):
            raise ValueError("invalid preflight artifact")
        serialized = json.dumps(artifact, indent=2) + "\n"
        if args.output.is_symlink():
            raise OSError("output must not be a symlink")
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            handle.write(serialized)
    except Exception:
        artifact["pass"] = False
        print("Preflight could not write its result.", file=sys.stderr)
        result = 1
    try:
        for _accepted in artifact["accepted_template_exposures"]:
            message = (
                "Accepted template exposure: template-fix-propose "
                "job=propose secret=AGENT_FIX_KEY\n"
            )
            print(message, end="")
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with Path(summary).open("a") as handle:
                    handle.write(message)
        output = f"pass={str(artifact['pass']).lower()}\n"
        if github_output := os.environ.get("GITHUB_OUTPUT"):
            with Path(github_output).open("a") as handle:
                handle.write(output)
        print(output, end="")
    except Exception:
        print("Preflight could not write its result.", file=sys.stderr)
        return 1
    return result
