import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from agent_repo.commands.fix_spec import render_spec
from agent_repo.config import load_config
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml
from test_fix_artifact import git

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "agents" / "fix"
FIXTURES = ROOT / "tests" / "fixtures"
KEY = "gateway-credential-value"

OMNIGENT = """
import json, os, pathlib, re, sys
home = pathlib.Path(os.environ["HOME"])
prompt = sys.argv[sys.argv.index("-p") + 1]
spec = json.loads((pathlib.Path(sys.argv[2]) / "config.yaml").read_text())
work = pathlib.Path.cwd()
record = {"spec": spec, "prompt": prompt, "git": (work / ".git").exists()}
record["env"] = sorted(os.environ)
with (home / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\\n")
mode = (home / "mode").read_text()
value = {key: re.search("^" + key + ": (\\\\S+)$", prompt, re.M)[1] for key in ["Run", "Base"]}
canary = re.search("^Private canary: (\\\\S+)$", prompt, re.M)[1]
(work / "lib.py").write_text("def add(a, b):\\n    return a + b\\n")
(work / "test_lib.py").write_text("def test_add():\\n    assert add(1, 2) == 3\\n")
if mode == "canary":
    (work / "notes.txt").write_text(canary)
answer = {"schema_version": "1", "run_id": value["Run"], "base_sha": value["Base"],
          "status": "patch", "rca": "add subtracts.", "tests": ["test_lib.py::test_add"],
          "risk": "low", "summary": "Return the sum.", "attempted": "", "escalate": None}
if mode == "no_patch":
    answer.update(status="no_patch", attempted="Could not reproduce.",
                  escalate={"to": "maintainer", "reason": "No repro."})
print("Done.\\n```json\\n" + json.dumps(answer) + "\\n```")
"""
BWRAP = """#!/bin/bash
here="$(dirname "$0")"
echo "$*" >> "$here/bwrap.calls"
[ "$(cat "$here/bwrap.mode")" = pass ]
"""
SYSCTL = """#!/bin/bash
here="$(dirname "$0")"
if [ "$1" = -n ]; then echo 1; exit 0; fi
echo "$*" >> "$here/sysctl.calls"
if [ "$(cat "$here/bwrap.mode")" = fail-once ]; then echo pass > "$here/bwrap.mode"; fi
"""


def write_script(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def agents_dir(path: Path, charter: str, *, turns: int = 40, setup: str = "") -> None:
    shutil.copytree(FIXTURES / "agents", path)
    config = (path / "config.yml").read_text()
    config = config.replace("fix_max_turns: 40", f"fix_max_turns: {turns}")
    (path / "config.yml").write_text(config.replace('setup: ""', f"setup: {json.dumps(setup)}"))
    (path / "charter.md").write_text((FIXTURES / "charter" / "example.md").read_text() + charter)


@pytest.fixture
def action(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_script(bin_dir / "omnigent", f"#!{sys.executable}\n{OMNIGENT}")
    write_script(bin_dir / "timeout", '#!/bin/bash\nshift 3\nexec "$@"\n')
    write_script(bin_dir / "bwrap", BWRAP)
    write_script(bin_dir / "sysctl", SYSCTL)
    write_script(bin_dir / "sudo", '#!/bin/bash\nexec "$@"\n')
    (bin_dir / "bwrap.mode").write_text("pass")
    home = tmp_path / "home"
    home.mkdir()
    (home / "mode").write_text("patch")
    policy = tmp_path / "policy"
    agents_dir(policy / ".agents", "\nDEFAULT-BRANCH CHARTER\n", setup="echo default > source.txt")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    (work / "lib.py").write_text("def add(a, b):\n    return a - b\n")
    agents_dir(
        work / ".agents", "\nAGENT-BRANCH CHARTER\n", turns=7, setup="echo agent > source.txt"
    )
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "base")
    issue = tmp_path / "issue.md"
    issue.write_text("add(1, 2) returns -1.")
    (tmp_path / "runner").mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "lib"),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "WORK_PATH": str(work),
        "POLICY_PATH": str(policy),
        "BASE_SHA": git(work, "rev-parse", "HEAD").strip(),
        "RUN_ID": "1234",
        "ISSUE_FILE": str(issue),
        "OUTPUT_DIR": str(tmp_path / "out"),
        "API_KEY_ENV": "ROLE_KEY",
        "ROLE_KEY": KEY,
        "GITHUB_TOKEN": "github-sentinel",
    }
    return {"env": env, "bin": bin_dir, "home": home, "work": work, "out": tmp_path / "out"}


def run_action(action: dict, mode: str) -> subprocess.CompletedProcess:
    script = ROOT / "actions" / "fix-agent" / "run.py"
    command = [sys.executable, str(script), mode]
    return subprocess.run(command, env=action["env"], capture_output=True, text=True, timeout=60)


def calls(action: dict) -> list[dict]:
    path = action["home"] / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_committed_spec_follows_sandbox_recommendation():
    spec = load_yaml(TEMPLATE / "config.yaml")
    assert spec["executor"] == {"type": "omnigent", "config": {"harness": "claude-sdk"}}
    assert spec["os_env"] == {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {
            "type": "linux_bwrap",
            "write_paths": ["."],
            "allow_network": False,
            "env_passthrough": [],
        },
    }
    policies = spec["guardrails"]["policies"]
    assert list(policies) == ["blast_radius", "allowlist_fix_tools", "tool_budget"]
    assert policies["blast_radius"]["function"]["arguments"] == {"gate_pushes": True}
    assert policies["allowlist_fix_tools"]["function"]["arguments"] == {
        "allowed": ["sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"]
    }
    assert policies["tool_budget"]["function"]["path"] == (
        "omnigent.policies.builtins.safety.max_tool_calls_per_session"
    )


def test_rendered_spec_takes_fix_max_turns(tmp_path):
    agents_dir(tmp_path / "agents", "", turns=7)
    output = render_spec(TEMPLATE, load_config(tmp_path / "agents"), tmp_path / "spec")
    rendered = load_yaml(output / "config.yaml")
    assert rendered["guardrails"]["policies"]["tool_budget"]["function"]["arguments"] == {
        "limit": 7
    }
    template = load_yaml(TEMPLATE / "config.yaml")
    template["guardrails"]["policies"]["tool_budget"]["function"]["arguments"]["limit"] = 7
    assert rendered == template
    assert (output / "prompt.md").read_text() == (TEMPLATE / "prompt.md").read_text()


@pytest.mark.parametrize(
    "sandbox",
    [
        "    type: none\n",
        "    type: linux_bwrap\n    write_paths: [.]\n    allow_network: true\n"
        "    env_passthrough: []\n",
        "    type: linux_bwrap\n    write_paths: [.]\n    allow_network: false\n"
        "    env_passthrough: [LLM_API_KEY]\n",
    ],
)
def test_rendered_spec_never_weakens_sandbox(tmp_path, sandbox):
    agents_dir(tmp_path / "agents", "")
    config = load_config(tmp_path / "agents")
    output = render_spec(TEMPLATE, config, tmp_path / "spec")
    text = (output / "config.yaml").read_text()
    assert '"type": "linux_bwrap"' in text
    assert '"type": "none"' not in text and "type: none" not in text
    drifted = tmp_path / "drifted"
    shutil.copytree(TEMPLATE, drifted)
    original = (TEMPLATE / "config.yaml").read_text()
    start = original.index("  sandbox:\n") + len("  sandbox:\n")
    end = original.index("guardrails:")
    (drifted / "config.yaml").write_text(original[:start] + sandbox + original[end:])
    with pytest.raises(ValueError, match="linux_bwrap"):
        render_spec(drifted, config, tmp_path / "unsafe")
    assert not (tmp_path / "unsafe").exists()


def test_preflight_fails_closed_and_agent_never_starts(action):
    (action["bin"] / "bwrap.mode").write_text("fail")
    preflight = subprocess.run(
        ["bash", str(ROOT / "actions" / "fix-agent" / "preflight.sh")],
        env={"PATH": action["env"]["PATH"]},
        capture_output=True,
        text=True,
    )
    assert preflight.returncode != 0
    assert "never runs unsandboxed" in preflight.stderr
    assert (action["bin"] / "bwrap.calls").read_text().splitlines() == [
        "--unshare-pid --proc /proc --ro-bind / / true"
    ] * 2
    assert (action["bin"] / "sysctl.calls").read_text() == (
        "-w kernel.apparmor_restrict_unprivileged_userns=0\n"
    )
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "bwrap sandbox unavailable" in result.stderr
    assert calls(action) == []
    assert not action["out"].exists()
    assert (action["work"] / ".git").is_dir()


def test_preflight_retries_once_after_apparmor_change(action):
    (action["bin"] / "bwrap.mode").write_text("fail-once")
    preflight = subprocess.run(
        ["bash", str(ROOT / "actions" / "fix-agent" / "preflight.sh")],
        env={"PATH": action["env"]["PATH"]},
        capture_output=True,
        text=True,
    )
    assert preflight.returncode == 0
    assert len((action["bin"] / "bwrap.calls").read_text().splitlines()) == 2


def test_agent_reads_policy_from_default_branch_checkout(action):
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    [call] = calls(action)
    limit = call["spec"]["guardrails"]["policies"]["tool_budget"]["function"]["arguments"]
    assert limit == {"limit": 40}
    assert "DEFAULT-BRANCH CHARTER" in call["prompt"]
    assert "AGENT-BRANCH CHARTER" not in call["prompt"]
    assert not call["git"], "agent could reach git metadata"
    assert "GITHUB_TOKEN" not in call["env"] and "ROLE_KEY" not in call["env"]
    artifact = json.loads((action["out"] / "fix.json").read_text())
    assert validate(artifact, load_schema("fix")) == []
    assert artifact["base_sha"] == action["env"]["BASE_SHA"]
    assert artifact["run_id"] == "1234"
    patch = (action["out"] / "fix.patch").read_text()
    assert "diff --git a/lib.py b/lib.py" in patch and "b/test_lib.py\nnew file mode" in patch
    assert KEY not in result.stdout + result.stderr + patch + json.dumps(artifact)


def test_no_patch_answer_writes_only_fix_json(action):
    (action["home"] / "mode").write_text("no_patch")
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    assert json.loads((action["out"] / "fix.json").read_text())["status"] == "no_patch"
    assert not (action["out"] / "fix.patch").exists()


def test_canary_in_patch_fails_closed(action):
    (action["home"] / "mode").write_text("canary")
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "credential or canary" in result.stderr
    assert not (action["out"] / "fix.json").exists()
    assert not (action["out"] / "fix.patch").exists()
    canary = calls(action)[0]["prompt"].split("Private canary: ")[1].split()[0]
    assert canary not in result.stdout + result.stderr


@pytest.mark.parametrize("problem", ["wrong_base", "policy_inside_work"])
def test_untrusted_checkout_layout_is_refused(action, problem):
    if problem == "wrong_base":
        action["env"]["BASE_SHA"] = "b" * 40
    else:
        action["env"]["POLICY_PATH"] = str(action["work"])
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "Fix agent refused" in result.stderr
    assert calls(action) == []


def test_setup_runs_default_branch_command_without_key(action):
    policy = Path(action["env"]["POLICY_PATH"]) / ".agents"
    config = (policy / "config.yml").read_text()
    command = "echo default > source.txt; env > setup-env.txt"
    config = config.replace('setup: "echo default > source.txt"', f"setup: {json.dumps(command)}")
    (policy / "config.yml").write_text(config)
    action["env"]["LLM_API_KEY"] = KEY
    result = run_action(action, "setup")
    assert result.returncode == 0, result.stderr
    assert (action["work"] / "source.txt").read_text() == "default\n"
    environment = (action["work"] / "setup-env.txt").read_text()
    assert KEY not in environment
    assert "GITHUB_TOKEN" not in environment
