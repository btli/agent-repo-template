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
    obj = reader(tmp_path, per_call_bytes=8)
    with pytest.raises(module.ToolError, match="per-call byte budget exceeded"):
        obj.read_file("file")
    assert obj._used == 8


def test_read_window_in_large_file(tmp_path):
    (tmp_path / "file").write_text("prefix\n" * 10000 + "wanted\nlast\n")
    module = importlib.import_module("agent_repo.repo_tools")
    obj = reader(tmp_path)
    before = obj._used
    with pytest.raises(module.ToolError, match="per-call byte budget exceeded"):
        obj.read_file("file", start_line=10001, max_lines=1)
    assert obj._used - before == 65536


def test_skipped_bytes_reduce_read_window(tmp_path):
    (tmp_path / "file").write_text("prefix\n" + "x" * 8)
    obj = reader(tmp_path, per_call_bytes=8)
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError, match="per-call byte budget exceeded"):
        obj.read_file("file", start_line=2)
    assert obj._used == 8


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


def test_grep_skips_large_files_without_charging_budget(tmp_path):
    for index in range(12):
        (tmp_path / f"a{index:02}").write_text("x" * 70000)
    (tmp_path / "small").write_text("needle\n")
    obj = reader(tmp_path, per_run_bytes=100)
    result = obj.grep("needle")
    assert ("small", 1, "needle") in result
    assert sum("skipped" in line for _, _, line in result) == 12
    assert obj._used == 7
    assert obj.read_file("small") == "needle\n"


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


@pytest.mark.parametrize("kwargs", [{}, {"per_call_bytes": 1000000}])
def test_scan_cap_is_65536_separate_from_response_cap(tmp_path, kwargs):
    (tmp_path / "a").write_text("x" * 20000 + "needle\n")
    obj = reader(tmp_path, **kwargs)
    assert obj.per_call_bytes == 65536
    assert obj.grep("needle")[0][2].endswith("needle")


@pytest.mark.parametrize("prefix", ["", "prefix\n"])
@pytest.mark.parametrize("last_line", ["abc", "abc\n"])
def test_exact_remaining_budget_allows_eof(tmp_path, prefix, last_line):
    content = prefix + last_line
    path = tmp_path / "a"
    path.write_text(content)
    obj = reader(tmp_path, per_run_bytes=len(content))
    assert obj.read_file("a", start_line=2 if prefix else 1) == last_line
    assert obj._used == len(content)
    path.write_text(content + "x")
    module = importlib.import_module("agent_repo.repo_tools")
    with pytest.raises(module.ToolError, match="per-run byte budget exceeded"):
        obj.read_file("a")
    assert obj._used == len(content)


def test_empty_file_after_exact_budget_grep(tmp_path):
    (tmp_path / "a").write_text("abc\n")
    (tmp_path / "b").touch()
    obj = reader(tmp_path, per_run_bytes=4)
    assert obj.grep("abc") == [("a", 1, "abc")]
    assert obj._used == 4


def test_grep_unicode_is_bounded_after_serialization(tmp_path):
    module = importlib.import_module("agent_repo.repo_tools")
    lines = ["é" * 500] * 20
    (tmp_path / "a").write_text("\n".join(lines) + "\n")
    unbounded = [("a", number, line) for number, line in enumerate(lines, 1)]
    assert module.response_bytes(unbounded) > 32768
    result = reader(tmp_path).grep("é")
    assert ("a", 1, lines[0]) in result
    assert any("truncated" in line for _, _, line in result)
    assert module.response_bytes(result) <= 32768


def test_grep_serialized_output_stays_within_call_cap(tmp_path):
    import json

    (tmp_path / "a").write_text("x\n" * 100)
    result = reader(tmp_path, per_call_bytes=256).grep("x")
    assert any("truncated" in line for _, _, line in result)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 256
