import copy
import importlib
import json
from pathlib import Path

import pytest
from agent_repo.schema import load_schema, validate


@pytest.fixture
def decision():
    return json.loads(Path("tests/fixtures/triage/tier1.json").read_text())


def merge(*args):
    return importlib.import_module("agent_repo.commands.triage_merge").merge(*args)


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


@pytest.mark.parametrize(
    "mode", ["success", "nonzero", "prose", "invalid", "key", "canary", "escapedkey"]
)
def test_action_fails_closed_on_runner_output(tmp_path, mode):
    import os
    import subprocess
    import sys

    decision = json.loads(Path("tests/fixtures/triage/tier1.json").read_text())
    issue = tmp_path / "issue.txt"
    issue.write_text("The parser fails on an empty body.")
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                key: "reference"
                for key in ["charter", "areas", "labels", "contributing", "docs_index"]
            }
        )
    )
    output = tmp_path / "decision.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "timeout"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,re,sys
prompt=sys.argv[-1]
mode=os.environ['FAKE_MODE']
expected=['900','omnigent','run',os.environ['TRIAGE_AGENT'],'--no-session','--no-log']
assert sys.argv[1:7] == expected
assert os.environ['LLM_API_KEY'] == 'credential-value'
if mode=='nonzero': sys.exit(7)
if mode=='prose': print('no JSON');sys.exit(0)
if mode=='key': print(os.environ['LLM_API_KEY']);sys.exit(0)
if mode=='canary': print(re.search(r'Private canary: (\\S+)',prompt)[1]);sys.exit(0)
d=json.load(open(os.environ['DECISION_FILE']))
if mode=='invalid': d['labels']='wrong'
if mode=='escapedkey':
 d['rca']={'hypothesis':'credential-value','locations':[],
           'confidence':'low','confirm_with':'check'}
 escaped=''.join(chr(92)+'u%04x'%ord(c) for c in 'credential-value')
 text=json.dumps(d).replace('credential-value',escaped)
 print('```json\\n'+text+'\\n```');sys.exit(0)
print('```json\\n'+json.dumps(d)+'\\n```')
"""
    )
    compile(executable.read_text(), str(executable), "exec")
    executable.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
        PYTHONPATH=str(Path("lib").resolve()),
        TRIAGE_AGENT=str(Path("agents/triage").resolve()),
        DECISION_FILE=str(Path("tests/fixtures/triage/tier1.json").resolve()),
        ISSUE_FILE=str(issue),
        CONTEXT_FILE=str(context),
        OUTPUT_FILE=str(output),
        API_KEY_ENV="ROLE_KEY",
        ROLE_KEY="credential-value",
        FAKE_MODE=mode,
    )
    result = subprocess.run(
        [sys.executable, "actions/triage-agent/run.py"], env=env, capture_output=True, text=True
    )
    assert result.returncode == (0 if mode == "success" else 1)
    merged = json.loads(output.read_text())
    assert merged["answers"] == decision["answers"]
    assert validate(merged, load_schema("decision")) == []
    assert bool(merged["escalate"]) == (mode != "success")
    assert "credential-value" not in result.stdout + result.stderr + output.read_text()


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
