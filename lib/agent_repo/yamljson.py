import json
import re
import shutil
import subprocess
from pathlib import Path


class YamlError(Exception):
    """Raised when yq is unavailable or YAML cannot be decoded."""


def load_yaml(path: Path) -> object:
    if not path.is_file():
        raise YamlError(f"not a regular file: {path}")
    binary = shutil.which("yq")
    if binary is None:
        raise YamlError("yq v4 is required to read YAML")
    try:
        version = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YamlError("yq v4 is required to read YAML") from exc
    if version.returncode != 0 or re.search(r"version v?4\.", version.stdout) is None:
        raise YamlError("yq v4 is required to read YAML")
    try:
        result = subprocess.run(
            [binary, "-o=json", "eval", ".", str(path.resolve())],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YamlError(f"cannot read YAML file: {path}") from exc
    if result.returncode != 0:
        raise YamlError(f"invalid YAML: {path}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key, value in pairs:
            if key in mapping:
                raise YamlError(f"duplicate key {key}: {path}")
            mapping[key] = value
        return mapping

    try:
        return json.loads(result.stdout, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise YamlError(f"invalid YAML: {path}") from exc
