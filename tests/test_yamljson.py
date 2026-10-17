import subprocess
from pathlib import Path
from types import SimpleNamespace

import agent_repo.yamljson as yamljson
import pytest
from agent_repo.yamljson import YamlError, load_yaml


def test_yaml_12_preserves_on_key(tmp_path: Path):
    path = tmp_path / "workflow.yml"
    path.write_text("on:\n  push:\n    branches: [main]\n")
    assert load_yaml(path) == {"on": {"push": {"branches": ["main"]}}}


def test_invalid_yaml_raises_yaml_error(tmp_path: Path):
    path = tmp_path / "bad.yml"
    path.write_text("key: [unterminated\n")
    with pytest.raises(YamlError):
        load_yaml(path)


@pytest.mark.parametrize(
    ("content", "duplicate"),
    [
        ("repeat: first\nrepeat: second\n", "repeat"),
        ("outer:\n  repeat: first\n  repeat: second\n", "repeat"),
    ],
)
def test_duplicate_yaml_keys_raise_yaml_error(tmp_path: Path, content: str, duplicate: str):
    path = tmp_path / "duplicate.yml"
    path.write_text(content)
    with pytest.raises(YamlError, match=f"duplicate key {duplicate}") as caught:
        load_yaml(path)
    assert str(path) in str(caught.value)
    assert "first" not in str(caught.value)
    assert "second" not in str(caught.value)


def test_missing_yq_mentions_v4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "valid.yml"
    path.write_text("value: true\n")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(YamlError, match="yq v4"):
        load_yaml(path)


def test_yq_v3_is_rejected_with_v4_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "valid.yml"
    path.write_text("value: true\n")
    monkeypatch.setattr(yamljson.shutil, "which", lambda _: "/usr/bin/yq")
    monkeypatch.setattr(
        yamljson.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="yq version 3.4.1", stderr=""),
    )
    with pytest.raises(YamlError, match="yq v4"):
        load_yaml(path)


def test_path_starting_with_dash_is_read_as_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "-x.yml"
    path.write_text("value: safe\n")
    monkeypatch.chdir(tmp_path)
    assert load_yaml(Path("-x.yml")) == {"value": "safe"}


def test_missing_yaml_path_is_rejected_as_not_a_file(tmp_path: Path):
    with pytest.raises(YamlError, match="not a regular file"):
        load_yaml(tmp_path / "missing.yml")


def test_yq_uses_explicit_file_expression_and_closed_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "workflow.yml"
    path.write_text("value: safe\n")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        stdout = "yq version v4.54.1" if command[-1] == "--version" else '{"value":"safe"}'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(yamljson.shutil, "which", lambda _: "/usr/bin/yq")
    monkeypatch.setattr(yamljson.subprocess, "run", run)
    assert load_yaml(path) == {"value": "safe"}
    command, kwargs = calls[1]
    assert command == ["/usr/bin/yq", "-o=json", "eval", ".", str(path.resolve())]
    assert kwargs["stdin"] == subprocess.DEVNULL
