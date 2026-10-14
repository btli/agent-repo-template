import importlib

import pytest


def reader(root, **kwargs):
    return importlib.import_module("agent_repo.repo_tools").RepoReader(root, **kwargs)


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", ".git/config", "/proc/self/environ"])
def test_rejects_forbidden_paths(tmp_path, path):
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError):
        reader(tmp_path).read_file(path)


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside").write_text("private")
    (root / "link").symlink_to(tmp_path / "outside")
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError):
        reader(root).read_file("link")


def test_line_selection_and_utf8(tmp_path):
    (tmp_path / "file").write_text("first\ncafé\nlast\n")
    assert reader(tmp_path).read_file("file", 2, 1) == "café\n"


@pytest.mark.parametrize("method", ["read_file", "grep"])
def test_per_call_cap(tmp_path, method):
    (tmp_path / "file").write_text("x" * 9)
    module = importlib.import_module("agent_repo.repo_tools")
    obj = reader(tmp_path, per_call_bytes=8)
    with pytest.raises(module.ToolError):
        getattr(obj, method)("file" if method == "read_file" else "x")


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
