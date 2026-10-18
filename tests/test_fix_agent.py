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
import base64, json, os, pathlib, re, subprocess, sys
home = pathlib.Path(os.environ["HOME"])
prompt = sys.argv[sys.argv.index("-p") + 1]
spec = json.loads((pathlib.Path(sys.argv[2]) / "config.yaml").read_text())
work = pathlib.Path.cwd()
record = {"spec": spec, "prompt": prompt, "git": (work / ".git").exists()}
record["env"] = sorted(os.environ)
record["home"] = os.environ["HOME"]
with (home / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\\n")
mode = (home / "mode").read_text()
value = {key: re.search("^" + key + ": (\\\\S+)$", prompt, re.M)[1] for key in ["Run", "Base"]}
canary = re.search("^Private canary: (\\\\S+)$", prompt, re.M)[1]
(work / "lib.py").write_text("def add(a, b):\\n    return a + b\\n")
(work / "test_lib.py").write_text("def test_add():\\n    assert add(1, 2) == 3\\n")
key = os.environ["LLM_API_KEY"]
outputs = {"canary": canary, "split_canary": "-".join(canary), "oversized": "x" * 70000,
           "encoded_key": base64.b64encode(key.encode()).decode()}
if mode in outputs:
    (work / "notes.txt").write_text(outputs[mode])
if mode == "nested_repo":
    vendor = work / "vendor"
    vendor.mkdir()
    (vendor / ".gitattributes").write_text("*.txt filter=x\\n")
    (vendor / "a.txt").write_text("one\\n")
    git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid"]
    for args in [["init", "-q"], ["add", "."], ["-c", "commit.gpgsign=false", "commit", "-qm", "v"],
                 ["config", "filter.x.clean", str(home / "filter.sh")]]:
        subprocess.run([*git, *args], cwd=vendor, check=True, capture_output=True)
    (vendor / "a.txt").write_text("two\\n")
answer = {"schema_version": "1", "run_id": value["Run"], "base_sha": value["Base"],
          "status": "patch", "rca": "add subtracts.", "tests": ["test_lib.py::test_add"],
          "risk": "low", "summary": "Return the sum.", "attempted": "", "escalate": None}
if mode == "no_patch":
    answer.update(status="no_patch", attempted="Could not reproduce.",
                  escalate={"to": "maintainer", "reason": "No repro."})
if mode == "raw_key_summary":
    answer["summary"] += " " + key
print("Done.\\n```json\\n" + json.dumps(answer) + "\\n```")
"""
BWRAP = """#!/bin/bash
here="$(dirname "$0")"
echo "$*" >> "$here/bwrap.calls"
[ "$(cat "$here/bwrap.mode")" = pass ]
"""
TIMEOUT = """#!/bin/bash
echo "$*" >> "${0%/*}/timeout.calls"
shift 3
exec "$@"
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
    write_script(bin_dir / "timeout", TIMEOUT)
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
        "AGENT_TIMEOUT": "600",
        "ISSUE_FILE": str(issue),
        "OUTPUT_DIR": str(tmp_path / "out"),
        "API_KEY_ENV": "ROLE_KEY",
        "ROLE_KEY": KEY,
        "GITHUB_TOKEN": "github-sentinel",
    }
    return {"env": env, "bin": bin_dir, "home": home, "work": work, "out": tmp_path / "out"}


def run_preflight(path: str) -> subprocess.CompletedProcess:
    script = ROOT / "actions" / "fix-agent" / "preflight.sh"
    command = [shutil.which("bash"), str(script)]
    return subprocess.run(command, env={"PATH": path}, capture_output=True, text=True)


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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  cwd: .\n", "  cwd: ..\n"),
        ("  type: caller_process\n", "  type: remote_process\n"),
        ("  cwd: .\n", "  cwd: .\n  fork: true\n"),
    ],
)
def test_rendered_spec_refuses_drifted_os_env(tmp_path, old, new):
    agents_dir(tmp_path / "agents", "")
    drifted = tmp_path / "drifted"
    shutil.copytree(TEMPLATE, drifted)
    original = (TEMPLATE / "config.yaml").read_text()
    (drifted / "config.yaml").write_text(original.replace(old, new, 1))
    with pytest.raises(ValueError, match="cwd"):
        render_spec(drifted, load_config(tmp_path / "agents"), tmp_path / "unsafe")
    assert not (tmp_path / "unsafe").exists()


def test_harden_runner_is_the_first_step():
    steps = load_yaml(ROOT / "actions" / "fix-agent" / "action.yml")["runs"]["steps"]
    assert steps[0]["uses"].startswith("step-security/harden-runner@")
    assert steps[0]["with"]["egress-policy"] == "block"
    assert [step.get("name") for step in steps[1:]] == [
        "Sandbox preflight",
        "Project setup",
        "Run fix agent",
    ]
    assert not any("if" in step or "continue-on-error" in step for step in steps)


def test_preflight_reports_missing_bwrap_distinctly(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_script(bin_dir / "sudo", '#!/bin/bash\nexec "$@"\n')
    write_script(bin_dir / "apt-get", '#!/bin/bash\necho "$*" >> "${0%/*}/apt.calls"\n')
    write_script(bin_dir / "sysctl", "#!/bin/bash\necho 1\n")
    preflight = run_preflight(str(bin_dir))
    assert preflight.returncode != 0
    assert "::error::bwrap is not installed" in preflight.stderr
    assert "user namespaces" not in preflight.stderr
    assert (bin_dir / "apt.calls").read_text().splitlines() == [
        "update -qq",
        "install -y -qq --no-install-recommends bubblewrap",
    ]


def test_preflight_fails_closed_and_agent_never_starts(action):
    (action["bin"] / "bwrap.mode").write_text("fail")
    preflight = run_preflight(action["env"]["PATH"])
    assert preflight.returncode != 0
    assert "::error::bwrap cannot create user namespaces" in preflight.stderr
    assert "not installed" not in preflight.stderr
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
    preflight = run_preflight(action["env"]["PATH"])
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
    wrapper = (action["bin"] / "timeout.calls").read_text()
    assert wrapper.startswith("-k 15 600 omnigent run ") and wrapper.count(" --no-session ") == 1


def test_no_patch_answer_writes_only_fix_json(action):
    (action["home"] / "mode").write_text("no_patch")
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    assert json.loads((action["out"] / "fix.json").read_text())["status"] == "no_patch"
    assert not (action["out"] / "fix.patch").exists()


LEAKS = {
    "canary": "credential or canary",
    "split_canary": "credential or canary",
    "encoded_key": "credential or canary",
    "raw_key_summary": "credential or canary",
    "oversized": "outbound scan budget",
}


@pytest.mark.parametrize("mode", sorted(LEAKS))
def test_canary_in_patch_fails_closed(action, mode):
    (action["home"] / "mode").write_text(mode)
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert LEAKS[mode] in result.stderr
    assert KEY not in result.stdout + result.stderr
    assert not (action["out"] / "fix.json").exists()
    assert not (action["out"] / "fix.patch").exists()
    canary = calls(action)[0]["prompt"].split("Private canary: ")[1].split()[0]
    assert canary not in result.stdout + result.stderr


def test_repository_filters_never_run_after_the_agent(action):
    marker = action["home"] / "filter.ran"
    write_script(action["home"] / "filter.sh", f"#!/bin/bash\ntouch {marker}\ncat\n")
    (action["home"] / "mode").write_text("nested_repo")
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "Fix agent refused: agent output rejected." in result.stderr
    assert len(calls(action)) == 1
    assert not marker.exists()
    assert not (action["out"] / "fix.json").exists()
    assert not (action["out"] / "fix.patch").exists()


REFUSALS = {
    "wrong_base": "not at the base SHA",
    "policy_inside_work": "policy checkout must be separate",
    "policy_nested_in_work": "policy checkout must be separate",
    "work_nested_in_policy": "policy checkout must be separate",
    "timeout_too_short": "invalid inputs",
    "timeout_too_long": "invalid inputs",
    "symlinked_git": "plain git checkout",
    "runner_temp_inside_work": "outside the work tree",
    "output_inside_work": "outside the work tree",
    "persisted_extraheader": "persisted credentials",
    "credential_helper": "persisted credentials",
    "unscoped_extraheader": "persisted credentials",
    "include_if_gitdir_token": "persisted credentials",
    "include_path_token": "persisted credentials",
    "include_if_gitdir": "persisted credentials",
    "include_path": "persisted credentials",
    "remote_url_token": "persisted credentials",
    "insteadof_token": "persisted credentials",
    "policy_checkout_token": "persisted credentials",
    "key_in_issue": "credential in prompt",
    "oversized_issue": "prompt byte cap exceeded",
}


@pytest.mark.parametrize("problem", sorted(REFUSALS))
def test_untrusted_checkout_layout_is_refused(action, problem, tmp_path):
    work, env = action["work"], action["env"]
    if problem == "wrong_base":
        env["BASE_SHA"] = "b" * 40
    elif problem == "policy_inside_work":
        env["POLICY_PATH"] = str(work)
    elif problem == "policy_nested_in_work":
        env["POLICY_PATH"] = str(work / ".agents")
    elif problem == "work_nested_in_policy":
        env["WORK_PATH"] = str(Path(env["POLICY_PATH"]) / ".agents")
    elif problem.startswith("timeout"):
        env["AGENT_TIMEOUT"] = "59" if problem.endswith("short") else "21601"
    elif problem == "symlinked_git":
        shutil.move(work / ".git", tmp_path / "real-git")
        (work / ".git").symlink_to(tmp_path / "real-git")
    elif problem == "runner_temp_inside_work":
        (work / "runner").mkdir()
        env["RUNNER_TEMP"] = str(work / "runner")
    elif problem == "output_inside_work":
        env["OUTPUT_DIR"] = str(work / "out")
    elif problem == "persisted_extraheader":
        git(work, "config", "http.https://example.test/.extraheader", "basic e30=")
    elif problem == "credential_helper":
        git(work, "config", "credential.helper", "store")
    elif problem == "unscoped_extraheader":
        git(work, "config", "http.extraheader", "basic eHg=")
    elif problem.startswith("include"):
        # Checkout keeps the token in a separate file under the runner temp directory.
        included = tmp_path / "included.config"
        token = "[http]\n\textraheader = basic eHg=\n"
        included.write_text(token if problem.endswith("_token") else "[core]\n\tautocrlf = false\n")
        key = "include.path" if "path" in problem else f"includeIf.gitdir:{work}/.git.path"
        git(work, "config", key, str(included))
    elif problem == "remote_url_token":
        git(work, "remote", "add", "origin", "https://x-access-token:tok3n@example.test/o/r")
    elif problem == "insteadof_token":
        git(work, "config", "url.https://x:tok3n@example.test/.insteadOf", "https://example.test/")
    elif problem == "policy_checkout_token":
        policy = Path(env["POLICY_PATH"])
        git(policy, "init", "-q")
        git(policy, "config", "http.extraheader", "basic eHg=")
    elif problem == "key_in_issue":
        Path(env["ISSUE_FILE"]).write_text(f"Please use {KEY} to reproduce.")
    else:
        Path(env["ISSUE_FILE"]).write_text("x" * 130000)
    # A refused run must not leave an earlier artifact for a downstream consumer.
    stale = [Path(env["OUTPUT_DIR"]) / name for name in ("fix.json", "fix.patch")]
    stale[0].parent.mkdir(exist_ok=True)
    for path in stale:
        path.write_text("stale")
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "Fix agent refused: " in result.stderr and REFUSALS[problem] in result.stderr
    assert KEY not in result.stdout + result.stderr
    assert calls(action) == []
    assert not any(path.exists() for path in stale)


def test_unignored_setup_output_refuses_agent(action):
    assert run_action(action, "setup").returncode == 0
    assert (action["work"] / "source.txt").read_text() == "default\n"
    result = run_action(action, "agent")
    assert result.returncode == 1
    assert "Fix agent refused: work tree is not clean after setup" in result.stderr
    assert calls(action) == []


def test_repository_filters_never_run_before_the_agent(action):
    work, marker = action["work"], action["home"] / "filter.ran"
    (work / ".gitattributes").write_text("*.py filter=hooked\n")
    git(work, "add", ".gitattributes")
    git(work, "commit", "-q", "-m", "attributes")
    action["env"]["BASE_SHA"] = git(work, "rev-parse", "HEAD").strip()
    write_script(action["home"] / "filter.sh", f"#!/bin/bash\ntouch {marker}\ncat\n")
    git(work, "config", "filter.hooked.clean", str(action["home"] / "filter.sh"))
    # A same-size edit after setup makes the dirty-tree status hash it through the filter.
    (work / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    result = run_action(action, "agent")
    assert not marker.exists()
    assert result.returncode == 1
    assert "Fix agent refused: git config defines filters." in result.stderr
    assert calls(action) == []


def test_setup_runs_default_branch_command_without_key(action):
    policy = Path(action["env"]["POLICY_PATH"]) / ".agents"
    config = (policy / "config.yml").read_text()
    command = 'echo default > source.txt; env > setup-env.txt; echo "$HOME" > home.txt'
    config = config.replace('setup: "echo default > source.txt"', f"setup: {json.dumps(command)}")
    (policy / "config.yml").write_text(config)
    action["env"]["LLM_API_KEY"] = KEY
    commands = ["GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"]
    action["env"].update({name: str(action["home"] / name.lower()) for name in commands})
    result = run_action(action, "setup")
    assert result.returncode == 0, result.stderr
    assert (action["work"] / "source.txt").read_text() == "default\n"
    environment = (action["work"] / "setup-env.txt").read_text()
    assert KEY not in environment
    assert "GITHUB_TOKEN" not in environment
    assert not any(f"{name}=" in environment for name in commands)
    assert "PYTHONPATH" not in {line.partition("=")[0] for line in environment.splitlines()}
    home = Path((action["work"] / "home.txt").read_text().strip())
    assert home != action["home"] and home.name.startswith("agent-setup-home-")


@pytest.mark.parametrize("checkout", ["work", "policy"])
def test_setup_refuses_persisted_credentials_before_running(action, checkout):
    policy = Path(action["env"]["POLICY_PATH"])
    marker = Path(action["env"]["RUNNER_TEMP"]) / "setup.ran"
    config = (policy / ".agents" / "config.yml").read_text()
    command = json.dumps(f"touch {marker}")
    config = config.replace('setup: "echo default > source.txt"', f"setup: {command}")
    (policy / ".agents" / "config.yml").write_text(config)
    tree = action["work"] if checkout == "work" else policy
    if checkout == "policy":
        git(policy, "init", "-q")
    git(tree, "config", "http.https://example.test/.extraheader", "basic e30=")
    result = run_action(action, "setup")
    assert result.returncode == 1
    assert "Project setup refused: checkout persisted credentials" in result.stderr
    assert not marker.exists()


def test_setup_home_is_discarded_before_the_agent(action):
    policy = Path(action["env"]["POLICY_PATH"]) / ".agents"
    command = (
        'mkdir "$HOME/.cache" && echo dep > "$HOME/.cache/dep"; echo "$HOME" > "$RUNNER_TEMP/home"'
    )
    config = (policy / "config.yml").read_text()
    config = config.replace('setup: "echo default > source.txt"', f"setup: {json.dumps(command)}")
    (policy / "config.yml").write_text(config)
    assert run_action(action, "setup").returncode == 0
    setup_home = Path((Path(action["env"]["RUNNER_TEMP"]) / "home").read_text().strip())
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    assert not (setup_home / ".cache" / "dep").exists()
    assert Path(calls(action)[0]["home"]) != setup_home
