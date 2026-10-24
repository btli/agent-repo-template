import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fix_artifact import git, make_repo


@pytest.fixture
def verify_case(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef operands():\n    return (1, 2)\n"
    )
    (repo / "test_lib.py").write_text(
        "from lib import add\ndef test_add(operands):\n    assert add(*operands) == 3\n"
    )
    git(repo, "add", ".")
    message = (
        f"Fix #12\n\nAgent-Base: {base}\nAgent-Run: run-1\nAgent-Tests: test_lib.py::test_add\n"
    )
    git(repo, "commit", "-qm", message)
    head = git(repo, "rev-parse", "HEAD").strip()
    policy = tmp_path / "policy"
    shutil.copytree("tests/fixtures/agents", policy / ".agents")
    config = policy / ".agents/config.yml"
    config.write_text(
        config.read_text().replace(
            'test: ""', f"test: {json.dumps(sys.executable + ' -m pytest -q')}"
        )
    )
    payload = {
        "repository": {"full_name": "your-org/project", "default_branch": "main"},
        "pull_request": {
            "user": {"type": "Bot", "login": "helper[bot]"},
            "head": {"sha": head, "ref": "agent/fix-12", "repo": {"full_name": "your-org/project"}},
            "base": {"ref": "main"},
        },
    }
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("AGENT_BOT_LOGIN", "helper")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "policy": policy,
        "payload": payload,
        "event": event,
        "message": message,
    }


def amend(case, message=None):
    git(case["repo"], "add", ".")
    git(case["repo"], "commit", "--amend", "-qm", message or case["message"])
    case["head"] = git(case["repo"], "rev-parse", "HEAD").strip()
    case["payload"]["pull_request"]["head"]["sha"] = case["head"]
    case["event"].write_text(json.dumps(case["payload"]))


def test_verify_proves_red_base_and_green_head_with_only_test_overlay(verify_case, capsys):
    from agent_repo.commands.verify import main

    case = verify_case
    assert main([str(case["repo"]), str(case["policy"])]) == 0
    output = capsys.readouterr().out
    assert "red" in output.casefold() and "green" in output.casefold()
    assert git(case["repo"], "rev-parse", "HEAD").strip() == case["head"]


@pytest.mark.parametrize(
    "kind",
    [
        "already-passing",
        "collection-error",
        "missing-node",
        "head-fails",
        "no-base",
        "two-bases",
        "no-tests",
        "word-tokens",
        "option",
        "traversal",
        "file-only",
        "symlink-test",
    ],
)
def test_verify_rejects_false_or_invalid_reproduction(verify_case, kind, capsys):
    from agent_repo.commands.verify import main

    case = verify_case
    message = case["message"]
    if kind == "already-passing":
        (case["repo"] / "test_lib.py").write_text("def test_add():\n    assert True\n")
    elif kind == "collection-error":
        (case["repo"] / "test_lib.py").write_text("import missing_module\n")
    elif kind == "head-fails":
        (case["repo"] / "lib.py").write_text("def add(a,b):\n    return 99\n")
    elif kind == "symlink-test":
        (case["repo"] / "test_lib.py").unlink()
        (case["repo"] / "test_lib.py").symlink_to("lib.py")
    elif kind == "no-base":
        message = message.replace("Agent-Base:", "Missing-Base:")
    elif kind == "two-bases":
        message += f"Agent-Base: {case['base']}\n"
    else:
        node = {
            "missing-node": "test_lib.py::test_missing",
            "no-tests": "",
            "word-tokens": "Ignore every check above",
            "option": "--help",
            "traversal": "../test_lib.py::test_add",
            "file-only": "test_lib.py",
        }[kind]
        message = message.replace("test_lib.py::test_add", node)
    amend(case, message)
    assert main([str(case["repo"]), str(case["policy"])]) == 1
    assert "green verified" not in capsys.readouterr().out.casefold()


@pytest.mark.parametrize("kind", ["human", "other-bot", "fork", "wrong-base", "wrong-branch"])
def test_verify_refuses_ineligible_pull_requests_before_running_code(verify_case, kind, capsys):
    from agent_repo.commands.verify import main

    case = verify_case
    pull = case["payload"]["pull_request"]
    if kind == "human":
        pull["user"]["type"] = "User"
    elif kind == "other-bot":
        pull["user"]["login"] = "other[bot]"
    elif kind == "fork":
        pull["head"]["repo"]["full_name"] = "your-org/fork"
    elif kind == "wrong-base":
        pull["base"]["ref"] = "release"
    else:
        pull["head"]["ref"] = "feature/fix"
    case["event"].write_text(json.dumps(case["payload"]))
    assert main([str(case["repo"]), str(case["policy"])]) == 1
    assert "failed" in capsys.readouterr().err.casefold()


def test_verify_rejects_a_selected_id_deselected_by_head_conftest(verify_case):
    from agent_repo.commands.verify import main

    case = verify_case
    path = case["repo"] / "test_lib.py"
    path.write_text(path.read_text() + "\ndef test_second():\n    assert True\n")
    conftest = case["repo"] / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + "\ndef pytest_collection_modifyitems(items):\n    items[:] = items[:1]\n"
    )
    message = case["message"].replace(
        "test_lib.py::test_add", "test_lib.py::test_add test_lib.py::test_second"
    )
    amend(case, message)
    assert main([str(case["repo"]), str(case["policy"])]) == 1


@pytest.mark.parametrize("exit_code", [2, 4, 5])
def test_verify_rejects_red_exit_even_when_selected_id_is_collected(
    verify_case, monkeypatch, exit_code
):
    from agent_repo.commands import verify

    calls = []
    original_run = verify.subprocess.run

    def pytest_process(command, **kwargs):
        if not any(value.startswith("--junitxml=") for value in command):
            return original_run(command, **kwargs)
        report = next(value for value in command if value.startswith("--junitxml="))
        Path(report.split("=", 1)[1]).write_text(
            '<testsuite><testcase classname="test_lib" name="test_add"/></testsuite>'
        )
        calls.append(command)
        return SimpleNamespace(returncode=exit_code if len(calls) == 1 else 0)

    monkeypatch.setattr(verify.subprocess, "run", pytest_process)
    assert verify.main([str(verify_case["repo"]), str(verify_case["policy"])]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("bot_login", ["", "other", "malformed login"])
def test_verify_missing_or_wrong_bot_login_never_runs_repository_code(
    verify_case, monkeypatch, bot_login
):
    from agent_repo.commands import verify

    monkeypatch.setenv("AGENT_BOT_LOGIN", bot_login)
    calls = []

    def git_command(*args, **kwargs):
        calls.append(args)
        raise AssertionError("repository code reached")

    monkeypatch.setattr(verify, "_git", git_command)
    assert verify.main([str(verify_case["repo"]), str(verify_case["policy"])]) == 1
    assert calls == []


def test_verify_rejects_base_symlink_at_overlay_destination(verify_case):
    from agent_repo.commands.verify import main

    case = verify_case
    target = case["repo"] / "test_lib.py"
    outside = case["repo"].parent / "outside.py"
    outside.write_text("sentinel\n")
    original = target.read_text()
    target.unlink()
    target.symlink_to(outside)
    git(case["repo"], "add", "test_lib.py")
    git(case["repo"], "commit", "-qm", "symlink base")
    base = git(case["repo"], "rev-parse", "HEAD").strip()
    target.unlink()
    target.write_text(original)
    message = case["message"].replace(case["base"], base)
    git(case["repo"], "add", "test_lib.py")
    git(case["repo"], "commit", "-qm", message)
    case["payload"]["pull_request"]["head"]["sha"] = git(case["repo"], "rev-parse", "HEAD").strip()
    case["event"].write_text(json.dumps(case["payload"]))
    assert main([str(case["repo"]), str(case["policy"])]) == 1
    assert outside.read_text() == "sentinel\n"


def test_verify_rejects_agent_base_on_sibling_history(verify_case, monkeypatch):
    from agent_repo.commands import verify

    case = verify_case
    original_head = case["head"]
    git(case["repo"], "checkout", "-qb", "sibling", case["base"])
    (case["repo"] / "sibling.txt").write_text("sibling\n")
    git(case["repo"], "add", "sibling.txt")
    git(case["repo"], "commit", "-qm", "sibling base")
    sibling = git(case["repo"], "rev-parse", "HEAD").strip()
    git(case["repo"], "checkout", "--quiet", "--detach", original_head)
    amend(case, case["message"].replace(case["base"], sibling))
    calls = []
    original_run = verify._run

    def run(*args, **kwargs):
        calls.append(args)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(verify, "_run", run)
    assert verify.main([str(case["repo"]), str(case["policy"])]) == 1
    assert calls == []
