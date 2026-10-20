import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from agent_repo.cli import main
from agent_repo.commands import fix_artifact
from agent_repo.commands.fix_artifact import MAX_PATCH_BYTES, build_artifact
from agent_repo.schema import load_schema, validate


def git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    command = ["git", *identity, "-c", "commit.gpgsign=false", *args]
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "lib.py").write_text("def add(a, b):\n    return a - b\n")
    (path / ".gitignore").write_text("*.log\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD").strip()


def answer(base_sha: str, **changes) -> dict:
    data = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": base_sha,
        "status": "patch",
        "rca": "add subtracts its operands.",
        "tests": ["tests/test_lib.py::test_add"],
        "risk": "low",
        "summary": "Make add return the sum.",
        "attempted": "",
        "escalate": None,
    }
    data.update(changes)
    return data


def plant_fix(repo: Path) -> None:
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_lib.py").write_text("def test_add():\n    assert add(1, 2) == 3\n")
    (repo / "debug.log").write_text("ignored output\n")


def test_patch_has_new_and_modified_files_but_not_ignored(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    # A binary file and a non-ASCII name must survive the round trip through git apply.
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    (repo / "数据.txt").write_text("data\n")
    _, patch = build_artifact(repo, base_sha, "run-1", answer(base_sha))
    assert "diff --git a/lib.py b/lib.py" in patch
    assert "+    return a + b" in patch
    assert "diff --git a/tests/test_lib.py b/tests/test_lib.py\nnew file mode" in patch
    assert "debug.log" not in patch
    assert "diff --git a/blob.bin b/blob.bin" in patch and "GIT binary patch" in patch
    full_index = r"^diff --git a/lib.py b/lib.py\nindex [0-9a-f]{40}\.\.[0-9a-f]{40} "
    assert re.search(full_index, patch, re.M)
    assert "diff --git a/数据.txt b/数据.txt" in patch
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "checkout", "-q", base_sha)
    (clone / "fix.patch").write_text(patch, encoding="utf-8")
    git(clone, "apply", "--index", "fix.patch")
    assert (clone / "tests" / "test_lib.py").is_file()
    assert (clone / "blob.bin").read_bytes() == bytes(range(256))
    assert (clone / "数据.txt").read_text() == "data\n"


def test_patch_uses_detached_git_dir(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    git_dir = tmp_path / "git"
    shutil.move(repo / ".git", git_dir)
    _, patch = build_artifact(repo, base_sha, "run-1", answer(base_sha), git_dir=git_dir)
    assert "b/tests/test_lib.py" in patch
    assert not (repo / ".git").exists()


def test_runner_git_config_cannot_change_the_patch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "ignore").write_text("tests/\n")
    (hostile / ".gitconfig").write_text("[diff]\n\tnoprefix = true\n")
    (hostile / "system").write_text(f"[core]\n\texcludesFile = {hostile / 'ignore'}\n")
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    (wrapper / "git").write_text(
        f"#!/bin/bash\nexport HOME={hostile} GIT_CONFIG_SYSTEM={hostile / 'system'}\n"
        f'exec {shutil.which("git")} "$@"\n'
    )
    (wrapper / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper}:{os.environ['PATH']}")
    _, patch = build_artifact(repo, base_sha, "run-1", answer(base_sha))
    assert "diff --git a/lib.py b/lib.py" in patch
    assert "diff --git a/tests/test_lib.py b/tests/test_lib.py" in patch


def test_repository_diff_and_monitor_hooks_never_run(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    hooks = {}
    for name in ["fsmonitor", "external", "textconv"]:
        hooks[name] = tmp_path / f"{name}.sh"
        hooks[name].write_text(f"#!/bin/bash\ntouch {tmp_path / name}.ran\necho HOOK\n")
        hooks[name].chmod(0o755)
    (repo / ".gitattributes").write_text("*.py diff=conv\n")
    git(repo, "config", "core.fsmonitor", str(hooks["fsmonitor"]))
    git(repo, "config", "diff.external", str(hooks["external"]))
    git(repo, "config", "diff.conv.textconv", str(hooks["textconv"]))
    _, patch = build_artifact(repo, base_sha, "run-1", answer(base_sha))
    assert "+    return a + b" in patch
    assert sorted(path.name for path in tmp_path.glob("*.ran")) == []


def marker_script(tmp_path: Path) -> Path:
    script = tmp_path / "filter.sh"
    script.write_text(f"#!/bin/bash\ntouch {tmp_path / 'filter.ran'}\ncat\n")
    script.chmod(0o755)
    return script


# Worktree-scoped config (extensions.worktreeConfig) is outside --local but still applies,
# and a filter defined in an included file runs just like a local one.
@pytest.mark.parametrize(
    ("kind", "scope"),
    [("clean", "--local"), ("process", "--local"), ("clean", "--worktree"), ("clean", "include")],
)
def test_configured_filter_never_runs_on_edited_attributes(tmp_path, kind, scope):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    git(repo, "config", "extensions.worktreeConfig", "true")
    if scope == "include":
        included = tmp_path / "filters.config"
        included.write_text(f'[filter "hooked"]\n\t{kind} = {marker_script(tmp_path)}\n')
        git(repo, "config", "include.path", str(included))
    else:
        git(repo, "config", scope, f"filter.hooked.{kind}", str(marker_script(tmp_path)))
    plant_fix(repo)
    (repo / ".gitattributes").write_text("*.py filter=hooked\n")
    with pytest.raises(ValueError, match="filters"):
        build_artifact(repo, base_sha, "run-1", answer(base_sha))
    assert not (tmp_path / "filter.ran").exists()


def test_nested_repository_filter_never_runs(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    vendor = repo / "vendor"
    make_repo(vendor)
    (vendor / ".gitattributes").write_text("*.py filter=x\n")
    git(vendor, "add", ".gitattributes")
    git(vendor, "commit", "-q", "-m", "attributes")
    git(vendor, "config", "filter.x.clean", str(marker_script(tmp_path)))
    # A same-size edit makes git hash the file, which runs the nested clean filter.
    (vendor / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    git_dir = tmp_path / "git"
    shutil.move(repo / ".git", git_dir)
    with pytest.raises(ValueError) as refused:
        build_artifact(repo, base_sha, "run-1", answer(base_sha), git_dir=git_dir)
    assert not (tmp_path / "filter.ran").exists()
    assert "git metadata" in str(refused.value)


@pytest.mark.parametrize("kind", ["file", "symlink", "uppercase_directory"])
def test_any_dot_git_entry_is_refused(tmp_path, kind, monkeypatch):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    git_dir = tmp_path / "git"
    shutil.move(repo / ".git", git_dir)
    planted = repo / "tests" / ".git"
    if kind == "file":
        planted.write_text(f"gitdir: {git_dir}\n")
    elif kind == "symlink":
        planted.symlink_to(git_dir)
    else:
        (repo / "tests" / ".GIT").mkdir()
    commands = []
    monkeypatch.setattr(fix_artifact, "run_git", lambda *args: commands.append(args) or b"")
    with pytest.raises(ValueError) as refused:
        build_artifact(repo, base_sha, "run-1", answer(base_sha), git_dir=git_dir)
    assert commands == []
    assert "git metadata" in str(refused.value)


def test_gitlink_in_patch_is_refused(tmp_path):
    repo = tmp_path / "repo"
    make_repo(repo)
    gitlink = f"160000,{git(repo, 'rev-parse', 'HEAD').strip()},vendor"
    git(repo, "update-index", "--add", "--cacheinfo", gitlink)
    git(repo, "commit", "-q", "-m", "submodule")
    base_sha = git(repo, "rev-parse", "HEAD").strip()
    plant_fix(repo)
    with pytest.raises(ValueError, match="gitlink"):
        build_artifact(repo, base_sha, "run-1", answer(base_sha))


def test_oversized_patch_is_refused(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    (repo / "data.txt").write_text("x\n" * (MAX_PATCH_BYTES // 2))
    with pytest.raises(ValueError, match="too large"):
        build_artifact(repo, base_sha, "run-1", answer(base_sha))


def test_no_patch_produces_no_patch_and_requires_attempted(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    escalate = {"to": "maintainer", "reason": "Could not reproduce."}
    tried = answer(
        base_sha, status="no_patch", attempted="Ran the parser tests.", escalate=escalate
    )
    artifact, patch = build_artifact(repo, base_sha, "run-1", tried)
    assert patch is None
    assert artifact["status"] == "no_patch"
    for attempted in ["", "   "]:
        with pytest.raises(ValueError, match="attempted"):
            build_artifact(repo, base_sha, "run-1", dict(tried, attempted=attempted))
    missing = dict(tried)
    del missing["attempted"]
    with pytest.raises(ValueError, match="schema"):
        build_artifact(repo, base_sha, "run-1", missing)


def test_patch_status_requires_changes(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    with pytest.raises(ValueError, match="without changes"):
        build_artifact(repo, base_sha, "run-1", answer(base_sha))


def test_shell_metacharacters_in_test_names_are_rejected(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    hostile = answer(base_sha, tests=["x; rm -rf /"])
    assert "$.tests[0]: pattern" in validate(hostile, load_schema("fix"))
    with pytest.raises(ValueError, match="schema"):
        build_artifact(repo, base_sha, "run-1", hostile)


def test_artifact_validates_and_echoes_base_sha(tmp_path):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    artifact, _ = build_artifact(repo, base_sha, "run-1", answer(base_sha))
    assert validate(artifact, load_schema("fix")) == []
    assert artifact["base_sha"] == base_sha
    assert artifact["run_id"] == "run-1"
    for wrong in [{"base_sha": "b" * 40}, {"run_id": "run-2"}]:
        with pytest.raises(ValueError, match="identity"):
            build_artifact(repo, base_sha, "run-1", dict(answer(base_sha), **wrong))


def test_cli_writes_artifact_files(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_sha = make_repo(repo)
    plant_fix(repo)
    source = tmp_path / "answer.json"
    source.write_text(json.dumps(answer(base_sha)))
    output = tmp_path / "out"
    assert main(["fix_artifact", str(repo), base_sha, "run-1", str(source), str(output)]) == 0
    assert json.loads((output / "fix.json").read_text())["base_sha"] == base_sha
    assert "b/tests/test_lib.py" in (output / "fix.patch").read_text()
    source.write_text(json.dumps(answer(base_sha, tests=["x; rm -rf /"])))
    assert main(["fix_artifact", str(repo), base_sha, "run-1", str(source), str(tmp_path)]) == 1
    assert capsys.readouterr().err == "fix artifact failed\n"
    assert not (tmp_path / "fix.json").exists()
