import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def tools(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    root = tmp_path / "repo"
    root.mkdir()
    temp = tmp_path / "runner"
    temp.mkdir()
    budget = temp / "budget"
    budget.write_text("0\n")
    stub = types.ModuleType("omnigent_client.tools")
    stub.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "omnigent_client", types.ModuleType("omnigent_client"))
    monkeypatch.setitem(sys.modules, "omnigent_client.tools", stub)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root))
    monkeypatch.setenv("RUNNER_TEMP", str(temp))
    monkeypatch.setenv("AGENT_REPO_BUDGET_FILE", str(budget))
    result = {}
    for name in ["read_repo_file", "grep_repo"]:
        path = Path("agents/triage/tools/python") / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result[name] = getattr(mod, name)
    return result


def test_wrappers_read_and_grep_repo(tools):
    root = Path(os.environ["GITHUB_WORKSPACE"])
    (root / "file").write_text("a\nneedle\n")
    assert tools["read_repo_file"]("file", start_line=2, max_lines=1) == "needle\n"
    assert tools["grep_repo"]("needle") == [("file", 2, "needle")]


def test_wrapper_rejects_existing_outside_root(tools):
    root = Path(os.environ["GITHUB_WORKSPACE"])
    (root.parent / "outside").write_text("protected-sentinel")
    from agent_repo.repo_tools import ToolError

    with pytest.raises(ToolError) as error:
        tools["read_repo_file"]("../outside")
    assert "protected-sentinel" not in str(error.value)


def tool_process(name, *args, protocol=False):
    code = """import importlib.util,json,os,sys,types
stub=types.ModuleType('omnigent_client.tools');stub.tool=lambda fn:fn
sys.modules['omnigent_client']=types.ModuleType('omnigent_client')
sys.modules['omnigent_client.tools']=stub
name=sys.argv[1]
spec=importlib.util.spec_from_file_location(name,'agents/triage/tools/python/'+name+'.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
try:
 result=getattr(module,name)(*json.loads(sys.argv[2]))
 assert 'SENTINEL_JOB_SECRET' not in os.environ
 print(json.dumps(result))
except Exception as error:
 print(type(error).__name__);sys.exit(1)
"""
    env = dict(os.environ, PYTHONPATH=str(Path("lib").resolve()), SENTINEL_JOB_SECRET="job-secret")
    read_fd = write_fd = None
    if protocol:
        read_fd, write_fd = os.pipe()
        env["_AP_RESPONSE_FD"] = str(write_fd)
        code = code.replace(
            "print(json.dumps(result))",
            "serialized=result if isinstance(result,str) else json.dumps(result)\n "
            "os.write(int(os.environ['_AP_RESPONSE_FD']), "
            "json.dumps({'result':serialized}).encode())",
        )
    try:
        run = subprocess.run(
            [sys.executable, "-c", code, name, json.dumps(args)],
            env=env,
            pass_fds=() if write_fd is None else (write_fd,),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
            run.stdout = os.read(read_fd, 65536).decode()
        return run
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_subprocess_tools_share_run_budget(tools):
    root = Path(os.environ["GITHUB_WORKSPACE"])
    (root / "file").write_text("x" * 16)
    Path(os.environ["AGENT_REPO_BUDGET_FILE"]).write_text(str(524288 - 2 * 16) + "\n")
    first = tool_process("read_repo_file", "file")
    second = tool_process("grep_repo", "x", "file", 1)
    third = tool_process("read_repo_file", "file")
    assert first.returncode == second.returncode == 0
    assert third.returncode == 1 and third.stdout.strip() == "ToolError"


@pytest.mark.parametrize("state", ["missing", "corrupt", "outside", "symlink"])
def test_budget_state_fails_closed(tools, monkeypatch, state):
    root = Path(os.environ["GITHUB_WORKSPACE"])
    (root / "file").write_text("data")
    budget = Path(os.environ["AGENT_REPO_BUDGET_FILE"])
    if state == "missing":
        budget.unlink()
    if state == "corrupt":
        budget.write_text("00\n")
    if state == "outside":
        budget = root / "counter"
        budget.write_text("0\n")
        monkeypatch.setenv("AGENT_REPO_BUDGET_FILE", str(budget))
    if state == "symlink":
        original = budget.with_name("original")
        budget.rename(original)
        budget.symlink_to(original)
    result = tool_process("read_repo_file", "file")
    assert result.returncode == 1 and result.stdout.strip() == "ToolError"


def test_tool_process_drops_job_environment(tools):
    (Path(os.environ["GITHUB_WORKSPACE"]) / "file").write_text("safe")
    result = tool_process("read_repo_file", "file")
    assert result.returncode == 0 and json.loads(result.stdout) == "safe"


def test_tool_preserves_response_pipe_metadata(tools):
    (Path(os.environ["GITHUB_WORKSPACE"]) / "file").write_text("safe")
    result = tool_process("read_repo_file", "file", protocol=True)
    assert result.returncode == 0 and json.loads(result.stdout)["result"] == "safe"


@pytest.mark.parametrize("name", ["read_repo_file", "grep_repo"])
@pytest.mark.parametrize("content", ["x", "\x01", "é", '\\"'])
def test_maximum_response_completes_before_pipe_read(tools, name, content):
    root = Path(os.environ["GITHUB_WORKSPACE"])
    filename = "f" * 120
    (root / filename).write_text(
        content * 32768 if name == "read_repo_file" else (content * 32 + "\n") * 200
    )
    arguments = (filename,) if name == "read_repo_file" else (".", filename)
    result = tool_process(name, *arguments, protocol=True)
    assert result.returncode == 0
    assert len(result.stdout.encode()) <= 32768
    assert (
        "truncated" in json.loads(result.stdout)["result"]
        or "skipped" in json.loads(result.stdout)["result"]
    )
