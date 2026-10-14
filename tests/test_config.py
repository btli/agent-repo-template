from pathlib import Path

import pytest
from agent_repo.config import ConfigError, load_config

FIXTURE = Path("tests/fixtures/agents")


def copy_agents(tmp_path: Path) -> Path:
    target = tmp_path / ".agents"
    target.mkdir()
    for path in FIXTURE.iterdir():
        (target / path.name).write_bytes(path.read_bytes())
    return target


def test_fixture_config_loads_and_has_default_modes():
    config = load_config(FIXTURE)
    assert config.areas
    assert config.labels
    assert config.mode_for("fix") == "dry-run"


def test_unknown_key_names_the_key(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "config.yml"
    path.write_text(path.read_text().replace("version: 1", "version: 1\nunknown_setting: true"))
    with pytest.raises(ConfigError, match="unknown_setting"):
        load_config(target)


def test_apply_min_must_be_numeric_in_range(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "config.yml"
    path.write_text(path.read_text().replace("apply_min: 0.8", "apply_min: 1.5"))
    with pytest.raises(ConfigError):
        load_config(target)


def test_area_sensitive_defaults_to_false(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "areas.yml"
    path.write_text(path.read_text().replace("    sensitive: true\n", ""))
    assert load_config(target).areas[0].sensitive is False


def test_missing_config_is_config_error(tmp_path: Path):
    target = copy_agents(tmp_path)
    (target / "config.yml").unlink()
    with pytest.raises(ConfigError):
        load_config(target)
