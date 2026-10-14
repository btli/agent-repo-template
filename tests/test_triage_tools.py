import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def tools(monkeypatch, tmp_path):
    stub = types.ModuleType("omnigent_client.tools")
    stub.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "omnigent_client", types.ModuleType("omnigent_client"))
    monkeypatch.setitem(sys.modules, "omnigent_client.tools", stub)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    result = {}
    for name in ["read_repo_file", "grep_repo"]:
        path = Path("agents/triage/tools/python") / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result[name] = getattr(mod, name)
    return result


def test_wrappers_read_and_grep_repo(tools, tmp_path):
    (tmp_path / "file").write_text("a\nneedle\n")
    assert tools["read_repo_file"]("file", start_line=2, max_lines=1) == "needle\n"
    assert tools["grep_repo"]("needle") == [("file", 2, "needle")]


@pytest.mark.parametrize("path", ["../outside", "/proc/self/environ"])
def test_wrapper_rejects_outside_root(tools, path):
    from agent_repo.repo_tools import ToolError

    with pytest.raises(ToolError):
        tools["read_repo_file"](path)


def test_wrappers_share_run_budget(tools, tmp_path):
    from agent_repo.repo_tools import ToolError

    (tmp_path / "file").write_text("x" * 65536)
    for _ in range(8):
        tools["read_repo_file"]("file")
    with pytest.raises(ToolError):
        tools["grep_repo"]("x")
