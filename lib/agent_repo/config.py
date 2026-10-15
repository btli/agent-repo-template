from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import YamlError, load_yaml


class ConfigError(Exception):
    """Raised when repository agent configuration is missing or invalid."""


@dataclass(frozen=True)
class Area:
    name: str
    description: str
    paths: tuple[str, ...]
    owners: tuple[str, ...]
    sensitive: bool


@dataclass(frozen=True)
class RepoConfig:
    raw: dict
    areas: tuple[Area, ...]
    labels: tuple[str, ...]

    def mode_for(self, role: Literal["triage", "review", "fix"]) -> Literal["dry-run", "live"]:
        return self.raw["mode"][role]


def _read(path: Path) -> object:
    if not path.is_file():
        raise ConfigError(f"missing {path.name}")
    try:
        return load_yaml(path)
    except YamlError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(agents_dir: Path) -> RepoConfig:
    config_path = agents_dir / "config.yml"
    if not config_path.is_file():
        raise ConfigError(f"missing {config_path.name}")
    raw = _read(config_path)
    if not isinstance(raw, dict):
        raise ConfigError("config.yml must contain a mapping")
    errors = validate(raw, load_schema("config"))
    if errors:
        raise ConfigError("; ".join(errors))

    areas_data = _read(agents_dir / "areas.yml")
    if not isinstance(areas_data, dict) or not isinstance(areas_data.get("areas"), list):
        raise ConfigError("areas.yml must contain an areas list")
    unknown_areas = set(areas_data) - {"areas"}
    if unknown_areas:
        raise ConfigError(f"areas.yml: unexpected key {sorted(unknown_areas)[0]}")
    areas: list[Area] = []
    area_names: set[str] = set()
    for index, item in enumerate(areas_data["areas"]):
        prefix = f"areas[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{prefix} must be a mapping")
        unknown = set(item) - {"name", "description", "paths", "owners", "sensitive"}
        if unknown:
            raise ConfigError(f"{prefix}: unexpected key {sorted(unknown)[0]}")
        if not isinstance(item.get("name"), str) or not isinstance(item.get("description"), str):
            raise ConfigError(f"{prefix} requires name and description strings")
        if item["name"] in area_names:
            raise ConfigError(f"{prefix}: duplicate area name {item['name']}")
        area_names.add(item["name"])
        for key in ("paths", "owners"):
            if not isinstance(item.get(key), list) or not all(
                isinstance(value, str) for value in item[key]
            ):
                raise ConfigError(f"{prefix}.{key} must be strings")
        sensitive = item.get("sensitive", False)
        if not isinstance(sensitive, bool):
            raise ConfigError(f"{prefix}.sensitive must be boolean")
        areas.append(
            Area(
                item["name"],
                item["description"],
                tuple(item["paths"]),
                tuple(item["owners"]),
                sensitive,
            )
        )

    labels_data = _read(agents_dir / "labels.yml")
    if not isinstance(labels_data, dict) or not isinstance(labels_data.get("labels"), list):
        raise ConfigError("labels.yml must contain a labels list")
    labels = labels_data["labels"]
    if not all(isinstance(label, str) for label in labels):
        raise ConfigError("labels must be strings")
    unknown_labels = set(labels_data) - {"labels"}
    if unknown_labels:
        raise ConfigError(f"labels.yml: unexpected key {sorted(unknown_labels)[0]}")
    return RepoConfig(raw, tuple(areas), tuple(labels))
