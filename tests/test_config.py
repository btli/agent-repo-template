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


def test_apply_min_rejects_nan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = copy_agents(tmp_path)
    from agent_repo import config
    from agent_repo.yamljson import load_yaml

    config_data = load_yaml(target / "config.yml")
    config_data["thresholds"]["apply_min"] = float("nan")
    original_load_yaml = config.load_yaml

    def load_yaml_with_nan(path: Path) -> object:
        if path.name == "config.yml":
            return config_data
        return original_load_yaml(path)

    monkeypatch.setattr(config, "load_yaml", load_yaml_with_nan)
    with pytest.raises(ConfigError):
        config.load_config(target)


def test_area_sensitive_must_be_boolean(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "areas.yml"
    path.write_text(path.read_text().replace("sensitive: true", "sensitive: 'yes'"))
    with pytest.raises(ConfigError, match="sensitive must be boolean"):
        load_config(target)


def test_version_boolean_is_not_integer_one(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "config.yml"
    path.write_text(path.read_text().replace("version: 1", "version: true"))
    with pytest.raises(ConfigError, match="version: const"):
        load_config(target)


@pytest.mark.parametrize("filename", ["areas.yml", "labels.yml"])
def test_missing_auxiliary_config_names_missing_file(tmp_path: Path, filename: str):
    target = copy_agents(tmp_path)
    (target / filename).unlink()
    with pytest.raises(ConfigError, match=f"missing {filename}"):
        load_config(target)


def test_area_sensitive_defaults_to_false(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "areas.yml"
    path.write_text(path.read_text().replace("    sensitive: true\n", ""))
    assert load_config(target).areas[0].sensitive is False


def test_areas_reject_unknown_top_level_key(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "areas.yml"
    path.write_text(path.read_text() + "unexpected: true\n")
    with pytest.raises(ConfigError, match="unexpected key"):
        load_config(target)


def test_labels_must_be_strings(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "labels.yml"
    path.write_text(path.read_text() + "  - 1\n")
    with pytest.raises(ConfigError, match="labels must be strings"):
        load_config(target)


def test_labels_reject_unknown_top_level_key(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "labels.yml"
    path.write_text(path.read_text() + "unexpected: true\n")
    with pytest.raises(ConfigError, match="unexpected key"):
        load_config(target)


def test_areas_reject_duplicate_names(tmp_path: Path):
    target = copy_agents(tmp_path)
    path = target / "areas.yml"
    content = path.read_text()
    path.write_text(
        content
        + "  - name: core\n"
        + "    description: duplicate area\n"
        + "    paths: []\n"
        + "    owners: []\n"
    )
    with pytest.raises(ConfigError, match="duplicate area name"):
        load_config(target)


def test_missing_config_is_config_error(tmp_path: Path):
    target = copy_agents(tmp_path)
    (target / "config.yml").unlink()
    with pytest.raises(ConfigError):
        load_config(target)
