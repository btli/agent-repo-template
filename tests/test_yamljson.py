from pathlib import Path

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


def test_missing_yq_mentions_v4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "valid.yml"
    path.write_text("value: true\n")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(YamlError, match="yq v4"):
        load_yaml(path)


def test_path_starting_with_dash_is_read_as_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "-x.yml"
    path.write_text("value: safe\n")
    monkeypatch.chdir(tmp_path)
    assert load_yaml(Path("-x.yml")) == {"value": "safe"}
