import importlib
from pathlib import Path

import pytest


def reader(root, **kwargs):
    return importlib.import_module("agent_repo.repo_tools").RepoReader(root, **kwargs)


@pytest.mark.parametrize(
    "kind", ["parent", "absolute", "git", "mixed_git", "symlink", "directory_symlink"]
)
def test_rejects_existing_forbidden_targets(tmp_path, kind):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("protected-sentinel")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("protected-sentinel")
    (root / ".GIT").mkdir(exist_ok=True)
    (root / ".GIT/config").write_text("protected-sentinel")
    (root / "link").symlink_to(outside)
    (root / "directory").symlink_to(tmp_path, target_is_directory=True)
    paths = {
        "parent": "../outside",
        "absolute": str(outside),
        "git": ".git/config",
        "mixed_git": ".GIT/config",
        "symlink": "link",
        "directory_symlink": "directory/outside",
    }
    assert (root / paths[kind]).is_file()
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError) as error:
        reader(root).read_file(paths[kind])
    assert "protected-sentinel" not in str(error.value)


@pytest.mark.skipif(not Path("/proc/self/environ").is_file(), reason="Linux /proc required")
def test_proc_environment_is_rejected(tmp_path):
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError):
        reader(tmp_path).read_file("/proc/self/environ")


def test_line_selection_and_utf8(tmp_path):
    (tmp_path / "file").write_text("first\ncafé\nlast\n")
    assert reader(tmp_path).read_file("file", 2, 1) == "café\n"


def test_read_per_call_cap(tmp_path):
    (tmp_path / "file").write_text("x" * 9)
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError):
        reader(tmp_path, per_call_bytes=8).read_file("file")


@pytest.mark.parametrize("method", ["read_file", "grep"])
def test_per_run_cap(tmp_path, method):
    (tmp_path / "file").write_text("abcd")
    module = importlib.import_module("agent_repo.repo_tools")
    obj = reader(tmp_path, per_call_bytes=8, per_run_bytes=6)
    obj.read_file("file")
    with pytest.raises(module.ToolError):
        getattr(obj, method)("file" if method == "read_file" else "a")


def test_grep_skips_symlinks_and_git(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "safe.py").write_text("a\nneedle\nneedle\n")
    (tmp_path / "outside").write_text("needle secret")
    (root / "link").symlink_to(tmp_path / "outside")
    (root / "directory").symlink_to(tmp_path, target_is_directory=True)
    (root / ".git").mkdir()
    (root / ".git/config").write_text("needle secret")
    assert reader(root).grep("needle", glob="**/*.py", max_results=1) == [("safe.py", 2, "needle")]
    assert reader(root).grep("secret") == []


@pytest.mark.parametrize("kwargs", [{"start_line": 0}, {"max_lines": 0}])
def test_invalid_line_limits(tmp_path, kwargs):
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError):
        reader(tmp_path).read_file("file", **kwargs)


def test_grep_skips_large_files_and_accounts_budget(tmp_path):
    (tmp_path / "large").write_text("x" * 300)
    (tmp_path / "small").write_text("needle\n")
    obj = reader(tmp_path, per_call_bytes=128, per_run_bytes=250)
    result = obj.grep("needle")
    assert ("small", 1, "needle") in result
    assert any(path == "large" and "skipped" in line for path, _, line in result)
    from agent_repo.repo_tools import ToolError

    with pytest.raises(ToolError):
        obj.grep("needle")


def test_grep_returns_partial_with_marker(tmp_path):
    (tmp_path / "a").write_text("needle\n" * 10)
    (tmp_path / "b").write_text("needle\n" * 10)
    result = reader(tmp_path, per_call_bytes=128).grep("needle")
    assert ("a", 1, "needle") in result
    assert any("truncated" in line for _, _, line in result)


def test_default_glob_searches_template_with_large_lockfiles():
    result = reader(Path.cwd()).grep("def merge")
    assert any(
        path == "lib/agent_repo/commands/triage_merge.py" and "def merge" in line
        for path, _, line in result
    )
    assert any("skipped" in line for _, _, line in result)


def test_grep_serialized_output_stays_within_call_cap(tmp_path):
    import json

    (tmp_path / "a").write_text("x\n" * 100)
    result = reader(tmp_path, per_call_bytes=256).grep("x")
    assert any("truncated" in line for _, _, line in result)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 256
