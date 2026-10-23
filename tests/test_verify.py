import json
import shutil
import sys

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
def test_verify_skips_ineligible_pull_requests_before_running_code(verify_case, kind, capsys):
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
    assert main([str(case["repo"]), str(case["policy"])]) == 0
    assert "skipped" in capsys.readouterr().out.casefold()


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
