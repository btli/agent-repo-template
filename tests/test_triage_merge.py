import copy
import importlib
import json
from pathlib import Path

import pytest
from agent_repo.schema import load_schema, validate


@pytest.fixture
def decision():
    return json.loads(Path("tests/fixtures/triage/tier1.json").read_text())


def merge(*args, **kwargs):
    kwargs.setdefault("allowed_labels", {"type:bug", "type:question", "area:core", "area:ui"})
    return importlib.import_module("agent_repo.commands.triage_merge").merge(*args, **kwargs)


def test_keeps_tier1_authority_and_copies_only_corrections(decision):
    original = copy.deepcopy(decision)
    tier2 = copy.deepcopy(decision)
    tier2.update(
        tier=2,
        answers={},
        security=True,
        notes=["untrusted"],
        labels=["type:question", "area:ui", "priority:P0", "new"],
        suggestions={"priority": "P0", "labels": []},
    )
    tier2["close"].update(target=99, tier2_agrees=True)
    tier2["rca"] = {
        "hypothesis": "Likely parser.",
        "locations": [{"path": "src.py", "line": 2}],
        "confidence": "low",
        "confirm_with": "Run repro.",
    }
    result = merge(decision, tier2, None)
    assert result["answers"] == original["answers"]
    assert result["labels"] == ["priority:P2", "keep", "type:question", "area:ui"]
    assert result["suggestions"] == original["suggestions"]
    assert result["close"]["target"] == 42
    assert result["close"]["tier2_agrees"] is False
    assert result["security"] is False and result["notes"] == []
    assert result["rca"]["hypothesis"] == "Likely parser."
    assert result["tier"] == original["tier"] == 1
    assert validate(result, load_schema("decision")) == []
    assert decision == original


@pytest.mark.parametrize(
    "bad",
    [
        {"labels": "bad"},
        {"rca": {"hypothesis": "incomplete"}},
        {"run_id": "other"},
        {"base_sha": "b" * 40},
        {"extra": "credential-value"},
    ],
)
def test_invalid_tier2_escalates_and_keeps_tier1(decision, bad):
    tier2 = copy.deepcopy(decision)
    tier2.update(bad)
    result = merge(decision, tier2, None)
    assert result["escalate"]["to"] == "maintainer"
    for key in ["answers", "labels", "rca", "close", "security", "notes"]:
        assert result[key] == decision[key]
    assert "credential-value" not in json.dumps(result)
    assert validate(result, load_schema("decision")) == []


def test_runtime_error_is_sanitized(decision):
    result = merge(decision, None, "credential-value")
    assert result["escalate"]["to"] == "maintainer"
    assert "credential-value" not in json.dumps(result)
    assert result["answers"] == decision["answers"]


def test_matching_close_vote_and_info_request(decision):
    tier2 = copy.deepcopy(decision)
    tier2["close"]["tier2_agrees"] = True
    tier2["needs_info"] = {"text": "Which version?"}
    result = merge(decision, tier2, None)
    assert result["close"]["tier2_agrees"] is True
    assert result["needs_info"] == {"text": "Which version?"}
    assert validate(result, load_schema("decision")) == []


def test_merge_cli_is_idempotent(tmp_path, decision):
    command = importlib.import_module("agent_repo.commands.triage_merge")
    source = Path("tests/fixtures/triage/tier1.json")
    output = tmp_path / "decision.json"
    args = [str(source), str(source), str(output)]
    assert command.main(args) == 0
    before = output.stat().st_mtime_ns
    assert command.main(args) == 0
    assert output.stat().st_mtime_ns == before
    assert json.loads(output.read_text())["answers"] == decision["answers"]


@pytest.mark.parametrize(
    "labels", [["type:invented"], ["area:invented"], ["type:bug", "type:question"]]
)
def test_unknown_or_multiple_types_escalate(decision, labels):
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = labels
    result = merge(decision, tier2, None)
    assert result["escalate"]["to"] == "maintainer"
    assert result["labels"] == decision["labels"]


def test_label_corrections_are_deduplicated(decision):
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = ["type:question", "type:question", "area:ui", "area:ui"]
    assert merge(decision, tier2, None)["labels"] == [
        "priority:P2",
        "keep",
        "type:question",
        "area:ui",
    ]


def test_missing_taxonomy_escalates(decision):
    assert merge(decision, decision, None, allowed_labels=None)["escalate"]["to"] == "maintainer"


@pytest.fixture
def action(monkeypatch, tmp_path, decision):
    import importlib.util
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    for key, value in {
        "DECISION_FILE": json.dumps(decision),
        "CONTEXT_FILE": json.dumps(
            {
                "charter": "report",
                "contributing": "tests",
                "docs_index": "README.md",
                "labels": json.dumps({"labels": ["type:bug", "type:question", "area:core"]}),
                "areas": json.dumps({"areas": [{"name": "core"}]}),
            }
        ),
        "ISSUE_FILE": "Parser bug",
    }.items():
        path = runner_temp / key
        path.write_text(value)
        monkeypatch.setenv(key, str(path))
    output = runner_temp / "result.json"
    for key, value in {
        "GITHUB_WORKSPACE": str(root),
        "RUNNER_TEMP": str(runner_temp),
        "TRIAGE_AGENT": str(Path("agents/triage").resolve()),
        "OUTPUT_FILE": str(output),
        "API_KEY_ENV": "ROLE_KEY",
        "ROLE_KEY": "credential-value",
        "SENTINEL_JOB_SECRET": "unrelated-job-secret",
        "GITHUB_TOKEN": "github-sentinel",
        "ACTIONS_RUNTIME_TOKEN": "actions-sentinel",
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("triage_action", "actions/triage-agent/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {
        "mode": "success",
        "calls": [],
        "output": output,
        "decision": decision,
        "module": module,
    }
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if Path(command[0]).name == "yq":
            return real_run(command, **kwargs)
        if command[0] == "grep":
            state["calls"].append((command, kwargs))
            return subprocess.CompletedProcess(
                command, int(b"credential-value" not in kwargs["input"]), b"", b""
            )
        state["calls"].append((command, kwargs))
        prompt = command[-1]
        import re

        canary = re.search(r"Private canary: (\S+)", prompt)[1]
        data = copy.deepcopy(decision)
        data["notes"] = []
        mode = state["mode"]
        prefix = ""
        if mode == "key":
            prefix = "credential-value\n"
        if mode == "canary":
            prefix = canary + "\n"
        if mode == "other_key":
            prefix = "unrelated-job-secret\n"
        if mode == "run_id":
            data["run_id"] = "other"
        if mode == "base_sha":
            data["base_sha"] = "b" * 40
        if mode == "taxonomy":
            data["labels"] = ["type:invented"]
        if mode == "escapedkey":
            data["rca"] = {
                "hypothesis": "credential-value",
                "locations": [],
                "confidence": "low",
                "confirm_with": "check",
            }
        if mode == "environment":
            script = (
                "import os; "
                "assert all(k not in os.environ for k in "
                "['SENTINEL_JOB_SECRET','GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ROLE_KEY']); "
                "assert os.environ['LLM_API_KEY']=='credential-value'"
            )
            import sys

            child = real_run([sys.executable, "-c", script], env=kwargs["env"], capture_output=True)
            assert child.returncode == 0, "job secret inherited by child"
        encoded = json.dumps(data)
        if mode == "escapedkey":
            encoded = encoded.replace(
                "credential-value", "".join(chr(92) + f"u{ord(c):04x}" for c in "credential-value")
            )
        stdout = (prefix + "```json\n" + encoded + "\n```\n").encode()
        # Every adverse mode has otherwise schema-valid JSON.
        assert validate(data, load_schema("decision")) == []
        return subprocess.CompletedProcess(command, 7 if mode == "nonzero" else 0, stdout, b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return state


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "key",
        "canary",
        "nonzero",
        "run_id",
        "base_sha",
        "escapedkey",
        "other_key",
        "taxonomy",
    ],
)
def test_action_targeted_failures_with_valid_json(action, mode, capsys):
    action["mode"] = mode
    rc = action["module"].main()
    assert rc == (0 if mode == "success" else 1)
    data = json.loads(action["output"].read_text())
    assert bool(data["escalate"]) == (mode != "success")
    assert data["answers"] == action["decision"]["answers"]
    assert validate(data, load_schema("decision")) == []
    captured = capsys.readouterr()
    assert "credential-value" not in captured.out + captured.err + action["output"].read_text()


@pytest.mark.parametrize("credential", ["credential-value", "unrelated-job-secret"])
@pytest.mark.parametrize("failure", [False, True])
def test_final_artifact_scans_preserved_tier1_credential(
    action, monkeypatch, failure, capsys, credential
):
    import os

    decision = copy.deepcopy(action["decision"])
    decision["notes"] = [credential]
    Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    action["output"].write_text("stale artifact")
    action["mode"] = "nonzero" if failure else "success"
    assert action["module"].main() == 1
    assert action["calls"]
    assert not action["output"].exists()
    captured = capsys.readouterr()
    assert "credential-value" not in captured.out + captured.err


def test_final_artifact_scans_preserved_canary(action, monkeypatch):
    import os

    monkeypatch.setattr(action["module"].secrets, "token_hex", lambda _: "private-canary")
    decision = copy.deepcopy(action["decision"])
    decision["notes"] = ["private-canary"]
    Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    assert action["module"].main() == 1
    assert not action["output"].exists()


def test_agent_child_has_only_allowed_environment(action):
    action["mode"] = "environment"
    assert action["module"].main() == 0


def test_timeout_has_kill_grace_and_no_key_in_argv(action):
    assert action["module"].main() == 0
    command = action["calls"][0][0]
    assert command[:4] == ["timeout", "-k", "15", "900"]
    assert all(
        "credential-value" not in argument for command, _ in action["calls"] for argument in command
    )


def test_oversize_prompt_escalates_without_spawning(action):
    import os

    Path(os.environ["ISSUE_FILE"]).write_text("x" * 140000)
    assert action["module"].main() == 1
    assert not action["calls"]
    assert json.loads(action["output"].read_text())["escalate"]["to"] == "maintainer"


@pytest.mark.parametrize(
    "context", [{"labels": "labels: [42]"}, {"areas": "areas: broken"}, {"labels": "labels: ["}]
)
def test_invalid_taxonomy_fails_closed(action, context):
    import os

    path = Path(os.environ["CONTEXT_FILE"])
    data = json.loads(path.read_text())
    data.update(context)
    path.write_text(json.dumps(data))
    assert action["module"].main() == 1
    assert not action["calls"]
    assert json.loads(action["output"].read_text())["escalate"]["to"] == "maintainer"
