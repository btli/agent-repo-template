import importlib.util
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
if mode != "attributes_only":
    (work / "lib.py").write_text("def add(a, b):\\n    return a + b\\n")
    (work / "test_lib.py").write_text("def test_add():\\n    assert add(1, 2) == 3\\n")
key = os.environ["LLM_API_KEY"]
outputs = {"canary": canary, "split_canary": "-".join(canary), "oversized": "x" * 70000,
           "encoded_key": base64.b64encode(key.encode()).decode()}
if mode in outputs:
    (work / "notes.txt").write_text(outputs[mode])
if mode == "nul_canary":
    (work / "notes.dat").write_bytes(b"\\0" + canary.encode())
if mode == "binary_attribute":
    (work / ".gitattributes").write_text("* binary\\n")
    (work / "notes.txt").write_text(canary)
if mode == "ebcdic_attribute":
    (work / ".gitattributes").write_text("secret.dat binary working-tree-encoding=IBM037\\n")
    (work / "secret.dat").write_bytes(("token=" + canary).encode("cp037"))
if mode == "attributes_only":
    (work / ".gitattributes").write_text("*.py text eol=crlf\\n")
if mode == "outside_symlink":
    (home / "outside.txt").write_text(canary)
    (work / "b.bin").unlink()
    os.symlink(home / "outside.txt", work / "b.bin")
if mode == "outside_parent":
    (home / "outside").mkdir()
    (home / "outside" / "logo.bin").write_text(canary)
    (work / "assets" / "logo.bin").unlink()
    (work / "assets").rmdir()
    os.symlink(home / "outside", work / "assets")
if mode == "delete_binary":
    (work / "secret.bin").unlink()
if mode == "modify_binary":
    (work / "secret.bin").write_bytes(b"\\0clean")
if mode == "max_fields":
    (work / "notes.txt").write_text(("y" * 99 + "\\n") * 590)
if mode == "large_binary":
    (work / "notes.dat").write_bytes(b"\\0" * 70000)
if mode == "wide_text":
    (work / "notes.txt").write_bytes(b"\\xc3\\xa9" * 40000)
if mode == "wide_binary":
    (work / "notes.dat").write_bytes(b"\\0" + b"\\xc3\\xa9" * 40000)
if mode == "large_text_edit":
    big = work / "big.py"
    big.write_text(big.read_text().replace("# filler\\n", "# changed\\n", 1))
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
if mode == "max_fields":
    answer.update(rca="r" * 4000, summary="s" * 4000, attempted="a" * 4000)
splits = {"newline_key_summary": ("summary", key, "\\n"),
          "newline_canary_rca": ("rca", canary, "\\n"),
          "tab_key_rca": ("rca", key, "\\t"), "tab_canary_summary": ("summary", canary, "\\t")}
if mode in splits:
    field, secret, separator = splits[mode]
    answer[field] = separator.join(secret[start:start + 4] for start in range(0, len(secret), 4))
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
    git(policy, "init", "-q")
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
    assert steps[2]["env"]["BASE_SHA"] == steps[3]["env"]["BASE_SHA"] == "${{ inputs.base-sha }}"
    key_setting = "${{ inputs.api-key-env }}"
    assert steps[2]["env"]["API_KEY_ENV"] == steps[3]["env"]["API_KEY_ENV"] == key_setting


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


def test_one_line_change_to_a_large_text_file_produces_a_patch(action):
    # Text changes are scanned in the patch, so a big file around them is not read whole.
    work, env = action["work"], action["env"]
    (work / "big.py").write_text("# filler\n" * 8000)
    git(work, "add", "big.py")
    git(work, "commit", "-q", "-m", "large file")
    env["BASE_SHA"] = git(work, "rev-parse", "HEAD").strip()
    (action["home"] / "mode").write_text("large_text_edit")
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    assert "+# changed" in (action["out"] / "fix.patch").read_text()


def commit_base_file(action: dict, name: str, data: bytes) -> None:
    work = action["work"]
    (work / name).parent.mkdir(exist_ok=True)
    (work / name).write_bytes(data)
    git(work, "add", name)
    git(work, "commit", "-q", "-m", f"add {name}")
    action["env"]["BASE_SHA"] = git(work, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize(
    ("name", "mode"), [("b.bin", "outside_symlink"), ("assets/logo.bin", "outside_parent")]
)
def test_changed_file_scan_never_follows_a_symlink_out_of_the_tree(action, name, mode):
    # A base binary replaced by a link, or under a linked directory, stays a binary change;
    # the link's target holds the canary.
    commit_base_file(action, name, b"\0x")
    (action["home"] / "mode").write_text(mode)
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    link = name.split("/")[0]
    assert f"b/{link}\nnew file mode 120000" in (action["out"] / "fix.patch").read_text()


def test_patch_under_the_limit_passes_beside_maximal_fields(action):
    # The patch has its own scan, so long rca, summary and attempted fields cannot crowd it out.
    (action["home"] / "mode").write_text("max_fields")
    result = run_action(action, "agent")
    assert result.returncode == 0, result.stderr
    patch = (action["out"] / "fix.patch").read_text()
    fields = json.dumps(json.loads((action["out"] / "fix.json").read_text()), ensure_ascii=False)
    assert len(patch) < 65536 < len(patch) + len(fields)


@pytest.mark.parametrize("mode", ["setup", "agent"])
def test_runner_git_config_never_reaches_the_base_check(action, mode, tmp_path):
    # A malformed runner-wide config would make an ambient git fail on a valid checkout.
    malformed = tmp_path / "malformed.gitconfig"
    malformed.write_text("[core\n")
    action["env"].update({"GIT_CONFIG_GLOBAL": str(malformed), "GIT_CONFIG_SYSTEM": str(malformed)})
    result = run_action(action, mode)
    assert result.returncode == 0, result.stderr


LEAKS = {
    "canary": "credential or canary",
    "split_canary": "credential or canary",
    "encoded_key": "credential or canary",
    "raw_key_summary": "credential or canary",
    "oversized": "outbound scan budget",
    # The key sits only in the base pre-image, which the binary patch still encodes.
    "delete_binary": "credential or canary",
    "modify_binary": "credential or canary",
    # Binary hunks are base85 in the patch, so only the file scan sees these.
    "nul_canary": "credential or canary",
    "binary_attribute": "credential or canary",
    "large_binary": "outbound scan budget",
    # The raw bytes are EBCDIC, but the patch encodes the blob git re-encodes to UTF-8.
    "ebcdic_attribute": "credential or canary",
    # Attributes decide how git encodes shipped content, so a fix may not change them.
    "attributes_only": "agent output changes .gitattributes",
    # The limit is in bytes, and these are under it in decoded characters.
    "wide_text": "outbound scan budget",
    "wide_binary": "outbound scan budget",
    # JSON escaping turns these separators into letters, so only the field-value scan sees them.
    "newline_key_summary": "credential or canary",
    "newline_canary_rca": "credential or canary",
    "tab_key_rca": "credential or canary",
    "tab_canary_summary": "credential or canary",
}


@pytest.mark.parametrize("mode", sorted(LEAKS))
def test_canary_in_patch_fails_closed(action, mode):
    if mode in ("delete_binary", "modify_binary"):
        commit_base_file(action, "secret.bin", b"\0" + KEY.encode())
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


# Checkout can persist an SSH key or client certificate instead of a token.
CREDENTIAL_SETTINGS = {
    "ssh_command": ("core.sshCommand", "ssh -i /runner/temp/deploy-key"),
    "ssl_key": ("http.sslKey", "/runner/temp/client.key"),
    "url_scoped_ssl_cert": ("http.https://example.test/.sslCert", "/runner/temp/client.crt"),
}
# Worktree-scoped config is outside --local but still applies to the checkout.
WORKTREE_SETTINGS = {
    "worktree_filter": ("filter.hooked.clean", "cat"),
    "worktree_extraheader": ("http.https://example.test/.extraheader", "basic e30="),
}


def plant_worktree_config(work: Path, problem: str) -> None:
    git(work, "config", "extensions.worktreeConfig", "true")
    git(work, "config", "--worktree", *WORKTREE_SETTINGS[problem])


POLICY_GIT_PROBLEMS = (
    "policy_gitfile",
    "missing_policy_git",
    "linked_policy_config",
    "linked_policy_worktree_config",
)


def replace_policy_git(policy: Path, problem: str, tmp_path: Path) -> None:
    git_dir = policy / ".git"
    if problem.startswith("linked"):
        # Git would read a linked config file even when the link stays inside the checkout.
        name = "config.worktree" if "worktree" in problem else "config"
        shutil.copy(git_dir / "config", git_dir / "copied.config")
        (git_dir / name).unlink(missing_ok=True)
        (git_dir / name).symlink_to(git_dir / "copied.config")
        return
    # A policy directory without its own plain .git is not a checkout at all.
    shutil.rmtree(git_dir)
    if problem == "policy_gitfile":
        (policy / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n")


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
    "ssh_command": "persisted credentials",
    "ssl_key": "persisted credentials",
    "url_scoped_ssl_cert": "persisted credentials",
    "policy_checkout_token": "persisted credentials",
    "worktree_filter": "git config defines filters",
    "worktree_extraheader": "persisted credentials",
    "policy_gitfile": "policy checkout must be a plain git checkout",
    "missing_policy_git": "policy checkout must be a plain git checkout",
    "linked_policy_config": "policy checkout must be a plain git checkout",
    "linked_policy_worktree_config": "policy checkout must be a plain git checkout",
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
    elif problem in CREDENTIAL_SETTINGS:
        git(work, "config", *CREDENTIAL_SETTINGS[problem])
    elif problem in WORKTREE_SETTINGS:
        plant_worktree_config(work, problem)
    elif problem in POLICY_GIT_PROBLEMS:
        replace_policy_git(Path(env["POLICY_PATH"]), problem, tmp_path)
    elif problem == "policy_checkout_token":
        git(Path(env["POLICY_PATH"]), "config", "http.extraheader", "basic eHg=")
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
    # The same output passes once it is ignored, as setup outputs must be.
    exclude = action["work"] / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("source.txt\n")
    assert run_action(action, "agent").returncode == 0


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


@pytest.mark.parametrize("key_env", ["ROLE_KEY", "LC_SECRET"])
def test_setup_runs_default_branch_command_without_key(action, key_env):
    policy = Path(action["env"]["POLICY_PATH"]) / ".agents"
    config = (policy / "config.yml").read_text()
    command = 'echo default > source.txt; env > setup-env.txt; echo "$HOME" > home.txt'
    config = config.replace('setup: "echo default > source.txt"', f"setup: {json.dumps(command)}")
    (policy / "config.yml").write_text(config)
    # LC_* names pass the allowlist, so a key setting that uses one is removed by name.
    action["env"].update({"API_KEY_ENV": key_env, key_env: KEY, "LLM_API_KEY": KEY})
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
@pytest.mark.parametrize(
    "setting",
    [("http.https://example.test/.extraheader", "basic e30="), *CREDENTIAL_SETTINGS.values()],
)
def test_setup_refuses_persisted_credentials_before_running(action, checkout, setting):
    policy = Path(action["env"]["POLICY_PATH"])
    marker = Path(action["env"]["RUNNER_TEMP"]) / "setup.ran"
    config = (policy / ".agents" / "config.yml").read_text()
    command = json.dumps(f"touch {marker}")
    config = config.replace('setup: "echo default > source.txt"', f"setup: {command}")
    (policy / ".agents" / "config.yml").write_text(config)
    tree = action["work"] if checkout == "work" else policy
    git(tree, "config", *setting)
    result = run_action(action, "setup")
    assert result.returncode == 1
    assert "Project setup refused: checkout persisted credentials" in result.stderr
    assert not marker.exists()


SETUP_REFUSALS = {
    "commit_after_base": "work tree is not at the base SHA",
    "missing_base": "invalid inputs",
    "malformed_base": "invalid inputs",
    "untracked_script": "work tree is not clean before setup",
    "modified_script": "work tree is not clean before setup",
    "filter_configured": "git config defines filters",
    "ignored_script": "work tree is not clean before setup",
    "worktree_filter": "git config defines filters",
    "worktree_extraheader": "checkout persisted credentials; use persist-credentials: false",
    "symlinked_work_git": "work tree must be a plain git checkout",
    "policy_gitfile": "policy checkout must be a plain git checkout",
    "missing_policy_git": "policy checkout must be a plain git checkout",
    "linked_policy_config": "policy checkout must be a plain git checkout",
    "linked_policy_worktree_config": "policy checkout must be a plain git checkout",
}


@pytest.mark.parametrize("problem", sorted(SETUP_REFUSALS))
def test_setup_never_runs_code_outside_base_sha(action, problem, tmp_path):
    work, env = action["work"], action["env"]
    policy = Path(env["POLICY_PATH"]) / ".agents"
    marker = Path(env["RUNNER_TEMP"]) / "untrusted-ran"
    script = 'touch "$RUNNER_TEMP/untrusted-ran"\n'
    if problem == "modified_script":
        (work / "bootstrap.sh").write_text("true\n")
    elif problem == "ignored_script":
        (work / ".gitignore").write_text("bootstrap.sh\n")
    if problem in ("modified_script", "ignored_script"):
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "setup files")
        env["BASE_SHA"] = git(work, "rev-parse", "HEAD").strip()
    (work / "bootstrap.sh").write_text(script)
    config = (policy / "config.yml").read_text()
    config = config.replace('setup: "echo default > source.txt"', 'setup: "bash bootstrap.sh"')
    (policy / "config.yml").write_text(config)
    if problem == "commit_after_base":
        git(work, "add", "bootstrap.sh")
        git(work, "commit", "-q", "-m", "after base")
    elif problem == "missing_base":
        del env["BASE_SHA"]
    elif problem == "malformed_base":
        env["BASE_SHA"] = env["BASE_SHA"][:39] + "G"
    elif problem == "filter_configured":
        git(work, "config", "filter.hooked.clean", "cat")
    elif problem in WORKTREE_SETTINGS:
        plant_worktree_config(work, problem)
    elif problem == "symlinked_work_git":
        shutil.move(work / ".git", tmp_path / "real-git")
        (work / ".git").symlink_to(tmp_path / "real-git")
    elif problem in POLICY_GIT_PROBLEMS:
        replace_policy_git(policy.parent, problem, tmp_path)
    result = run_action(action, "setup")
    assert not marker.exists()
    assert result.returncode == 1
    assert f"Project setup refused: {SETUP_REFUSALS[problem]}." in result.stderr


# Every policy path the action reads, listed here so dropping one from run.py fails a case.
POLICY_PATHS = [
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
]


def test_action_confines_every_policy_path():
    # A dropped .agents entry is masked by its files, so the list itself is pinned too.
    path = ROOT / "actions" / "fix-agent" / "run.py"
    spec = importlib.util.spec_from_file_location("fix_agent_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert list(module.POLICY_FILES) == POLICY_PATHS


@pytest.mark.parametrize("mode", ["setup", "agent"])
@pytest.mark.parametrize("name", POLICY_PATHS)
def test_policy_path_outside_the_checkout_is_refused(action, name, mode):
    work, policy = action["work"], Path(action["env"]["POLICY_PATH"])
    # Each policy path the action reads is linked into the agent-branch checkout in turn.
    link, target = policy / name, work / name
    if not target.exists():
        target = work / "lib.py"
    if link.is_dir():
        shutil.rmtree(link)
    else:
        link.unlink(missing_ok=True)
    link.symlink_to(target)
    result = run_action(action, mode)
    assert not (work / "source.txt").exists(), "setup command ran"
    assert result.returncode == 1
    prefix = "Project setup refused" if mode == "setup" else "Fix agent refused"
    assert f"{prefix}: policy file resolves outside the policy checkout." in result.stderr
    assert calls(action) == []


UNDECODABLE_CONFIG = {
    "benign": b"[core]\n\tcomment = \xff\xfe\n",
    "extraheader": b"[http]\n\textraheader = basic \xff\xfe\n",
}
FIXED_FAILURES = {
    "setup": "Project setup failed: trusted configuration unavailable.\n",
    "agent": "Fix agent failed: trusted inputs unavailable.\n",
}


@pytest.mark.parametrize("mode", ["setup", "agent"])
@pytest.mark.parametrize("checkout", ["work", "policy"])
@pytest.mark.parametrize("entry", sorted(UNDECODABLE_CONFIG))
def test_undecodable_git_config_is_refused(action, mode, checkout, entry):
    # Config that cannot be decoded is refused outright, even when the bad byte looks harmless.
    tree = action["work"] if checkout == "work" else Path(action["env"]["POLICY_PATH"])
    with (tree / ".git" / "config").open("ab") as config:
        config.write(UNDECODABLE_CONFIG[entry])
    result = run_action(action, mode)
    assert result.returncode == 1
    assert result.stderr == FIXED_FAILURES[mode]
    assert not (action["work"] / "source.txt").exists()
    assert calls(action) == []


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
