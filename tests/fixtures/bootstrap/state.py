"""Offline GitHub API states served by the bootstrap fake `gh`."""

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPO = "your-org/example"
TARGET = f"repos/{REPO}"
UPSTREAM = "repos/btli/agent-repo-template"
PIN = "4f1c9e2d8b7a6c5d4e3f2a1b0c9d8e7f6a5b4c3d"
TAG_OBJECT = "9e8d7c6b5a49382716051f2e3d4c5b6a79881726"
BASE = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
BASE_TREE = "b2c3d4e5f60718293a4b5c6d7e8f9012345678a1"
VERSION = "v0.1.0"
T10B = "43c2d3b58ce3ebccd38d9ee2a05464966b6a94da"
ENVIRONMENTS = ("agent-propose-triage", "agent-propose-review", "agent-propose-fix", "agent-apply")
GATEWAY_URL = "https://gateway.example.com"
ENDPOINT_HOSTS = (
    "gateway.example.com",
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "results-receiver.actions.githubusercontent.com",
    "*.blob.core.windows.net",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
ENDPOINTS = " ".join(f"{host}:443" for host in ENDPOINT_HOSTS)
HUMAN_BYPASS = [
    {"actor_id": role, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    for role in (2, 4, 5)
]
RULESETS = {
    11: {
        "id": 11,
        "name": "agent-repo-template branches",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": HUMAN_BYPASS,
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/agent/**"]}},
        "rules": [
            {"type": "creation"},
            {"type": "update"},
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    },
    12: {
        "id": 12,
        "name": "agent-repo-template tags",
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": HUMAN_BYPASS,
        "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
        "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
    },
}
SECRETS = {
    "agent-propose-triage": ["AGENT_SYSTEM_ONE_KEY", "AGENT_TRIAGE_KEY"],
    "agent-propose-review": ["AGENT_REVIEW_KEY"],
    "agent-propose-fix": ["AGENT_FIX_KEY"],
    "agent-apply": ["AGENT_APP_PRIVATE_KEY", "AGENT_SCAN_KEYS"],
}


CUSTOM_POLICY = {"protected_branches": False, "custom_branch_policies": True}
# Deployment patterns by environment name: the first group excludes every agent and pull ref.
SAFE_PATTERNS = {"production": "main", "release": "release/*", "heads-main": "refs/heads/main"}
UNSAFE_PATTERNS = {
    "staging": "*",
    "preview": "pull/*/merge",
    "agentish": "agent/*",
    "fix-wide": "agent/fix-??*",
    "fix-range": "agent/fix-[2-9]*",
    "pull-wide": "pull/??*/merge",
    "fix-exact": "agent/fix-2",
    "pull-exact": "pull/2/merge",
    "a-star": "a*",
    "brace": "{agent,x}/fix-*",
}


def add_environments(state: dict, patterns: dict[str, str]) -> None:
    listing = state[f"GET {TARGET}/environments"]["json"]["environments"]
    for name, pattern in patterns.items():
        listing.append({"name": name, "deployment_branch_policy": dict(CUSTOM_POLICY)})
        state[f"GET {TARGET}/environments/{name}/deployment-branch-policies"] = {
            "json": {"branch_policies": [{"id": 1, "name": pattern, "type": "branch"}]}
        }


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def consumer_files(sha: str = PIN, version: str = VERSION) -> dict[str, bytes]:
    """The consumer tree as a finished bootstrap leaves it on the default branch."""
    files = {}
    source = ROOT / "consumer"
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        text = text.replace("__TEMPLATE_SHA__", sha).replace("__TEMPLATE_VERSION__", version)
        relative = path.relative_to(source).as_posix()
        files["CONTRIBUTING.md" if relative == "CONTRIBUTING.agents.md" else relative] = (
            text.encode()
        )
    return files


def set_files(state: dict, files: dict[str, bytes]) -> None:
    tree = []
    for path, data in sorted(files.items()):
        sha = blob_sha(data)
        tree.append({"path": path, "type": "blob", "mode": "100644", "sha": sha})
        state[f"GET {TARGET}/git/blobs/{sha}"] = {
            "json": {"encoding": "base64", "content": base64.b64encode(data).decode()}
        }
    state[f"GET {TARGET}/git/trees/{BASE}"] = {
        "json": {"sha": BASE_TREE, "tree": tree, "truncated": False}
    }


def labels() -> list[str]:
    text = (ROOT / "consumer/.agents/labels.yml").read_text()
    return [line.strip()[2:] for line in text.splitlines() if line.startswith("  - ")]


def _upstream(state: dict) -> None:
    state[f"GET {UPSTREAM}/git/ref/tags/{VERSION}"] = {
        "json": {"ref": f"refs/tags/{VERSION}", "object": {"type": "tag", "sha": TAG_OBJECT}}
    }
    state[f"GET {UPSTREAM}/git/tags/{TAG_OBJECT}"] = {
        "json": {"object": {"type": "commit", "sha": PIN}}
    }
    state[f"GET {UPSTREAM}/commits/{PIN}"] = {"json": {"sha": PIN}}
    state[f"GET {UPSTREAM}/compare/{PIN}...main"] = {"json": {"status": "ahead", "behind_by": 0}}
    state[f"GET {UPSTREAM}/compare/{T10B}...{PIN}"] = {"json": {"status": "ahead", "behind_by": 0}}


def empty_target() -> dict:
    """A fresh repository with an installed owner App and nothing bootstrapped yet."""
    state = {
        "default-write": {
            "id": 101,
            "number": 7,
            "sha": "c" * 40,
            "html_url": "https://github.com/your-org/example/pull/7",
        },
        "GET user": {"json": {"login": "maintainer"}},
        f"GET {TARGET}": {
            "json": {
                "full_name": REPO,
                "default_branch": "main",
                "permissions": {"admin": True},
                "owner": {"login": "your-org", "type": "User"},
            }
        },
        f"GET {TARGET}/git/ref/heads/main": {"json": {"object": {"type": "commit", "sha": BASE}}},
        f"GET {TARGET}/git/matching-refs/heads/bootstrap/agent-repo-template-{VERSION}": {
            "json": []
        },
        f"GET {TARGET}/git/matching-refs/heads/agent/": {"json": []},
        f"GET {TARGET}/labels": {"json": []},
        f"GET {TARGET}/environments": {"json": {"total_count": 0, "environments": []}},
        f"GET {TARGET}/actions/variables": {"json": {"total_count": 0, "variables": []}},
        f"GET {TARGET}/actions/permissions/workflow": {
            "json": {"default_workflow_permissions": "read"}
        },
        f"GET {TARGET}/rulesets": {"json": []},
        f"GET {TARGET}/actions/secrets": {"json": {"secrets": []}},
        f"GET {TARGET}/actions/runners": {"json": {"runners": []}},
        # A private App, the usual adopter case: GET /apps/{slug} answers 404 even for its owner.
        "GET apps/example-agent": {"status": 404},
        "GET users/example-agent%5Bbot%5D": {
            "json": {"login": "example-agent[bot]", "type": "Bot"}
        },
    }
    _upstream(state)
    set_files(state, {"README.md": b"# Example\n", "uv.lock": b"version = 1\n"})
    return state


def healthy_target() -> dict:
    """A repository exactly as a finished bootstrap leaves it."""
    state = empty_target()
    set_files(state, {"README.md": b"# Example\n", **consumer_files()})
    state[f"GET {TARGET}/labels"] = {"json": [{"name": name} for name in labels()]}
    custom = {"protected_branches": False, "custom_branch_policies": True}
    state[f"GET {TARGET}/environments"] = {
        "json": {
            "total_count": 4,
            "environments": [
                {"name": name, "deployment_branch_policy": custom} for name in ENVIRONMENTS
            ],
        }
    }
    for name in ENVIRONMENTS:
        state[f"GET {TARGET}/environments/{name}/deployment-branch-policies"] = {
            "json": {"branch_policies": [{"id": 5, "name": "main", "type": "branch"}]}
        }
        state[f"GET {TARGET}/environments/{name}/secrets"] = {
            "json": {"secrets": [{"name": secret} for secret in SECRETS[name]]}
        }
    variables = {
        "AGENTS_ENABLED": "true",
        "AGENT_BOT_LOGIN": "example-agent[bot]",
        "AGENT_APP_CLIENT_ID": "Iv23liExample",
        "AGENT_GATEWAY_KIND": "databricks",
        "AGENT_GATEWAY_URL": GATEWAY_URL,
        "AGENT_SYSTEM_ONE_URL": "https://systemone.example.com",
        "AGENT_TRIAGE_MODEL": "claude-sonnet-4-5",
        "AGENT_REVIEW_MODEL": "claude-sonnet-4-5",
        "AGENT_FIX_MODEL": "claude-sonnet-4-5",
        "AGENT_FIX_ALLOWED_ENDPOINTS": ENDPOINTS,
        "AGENT_SAFE_ENVIRONMENTS": "\n".join(sorted(ENVIRONMENTS)),
        "AGENT_CI_DEFAULT_TOKEN_READONLY": "true",
    }
    state[f"GET {TARGET}/actions/variables"] = {
        "json": {"variables": [{"name": key, "value": value} for key, value in variables.items()]}
    }
    state[f"GET {TARGET}/rulesets"] = {
        "json": [{"id": key, "name": value["name"]} for key, value in RULESETS.items()]
    }
    for key, value in RULESETS.items():
        state[f"GET {TARGET}/rulesets/{key}"] = {"json": value}
    return state


def variables(state: dict) -> dict[str, str]:
    entries = state[f"GET {TARGET}/actions/variables"]["json"]["variables"]
    return {entry["name"]: entry["value"] for entry in entries}


def set_variables(state: dict, values: dict[str, str | None]) -> None:
    current: dict[str, str | None] = {**variables(state), **values}
    state[f"GET {TARGET}/actions/variables"] = {
        "json": {
            "variables": [
                {"name": key, "value": value} for key, value in current.items() if value is not None
            ]
        }
    }


class FakeGitHub:
    """Runs bootstrap.sh with a PATH holding only python3, yq, the fake gh and a recording git."""

    def __init__(self, tmp_path: Path, responses: dict, *, yq: bool = True, gh: bool = True):
        self.responses = responses
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.state_path = tmp_path / "gh-state.json"
        self.log_path = tmp_path / "gh-log.jsonl"
        self.git_log = tmp_path / "git-log.txt"
        self._script("python3", f'exec "{sys.executable}" "$@"')
        if gh:
            fake = Path(__file__).with_name("gh")
            self._script("gh", f'exec "{sys.executable}" "{fake}" "$@"')
        if yq:
            self._script("yq", f'exec "{shutil.which("yq")}" "$@"')
        self._script("git", f'echo "$*" >> "{self.git_log}"\necho {"f" * 40}')
        assert shutil.which("gh", path=str(self.bin)) in (None, str(self.bin / "gh"))

    def _script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def run(self, *arguments: str, cwd: Path | None = None, env: dict | None = None, command=None):
        self.state_path.write_text(json.dumps(self.responses))
        if self.log_path.exists():
            self.log_path.unlink()
        environment = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "FAKE_GH_STATE": str(self.state_path),
            "FAKE_GH_LOG": str(self.log_path),
            **(env or {}),
        }
        return subprocess.run(
            command or [str(ROOT / "bootstrap.sh"), *arguments],
            cwd=cwd or self.home,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def writes(self) -> list[dict]:
        return [call for call in self.calls() if call["method"] != "GET" or call["body"]]

    def journal(self) -> list[dict]:
        path = self.home / ".agent-repo-template/journal/your-org__example.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]
