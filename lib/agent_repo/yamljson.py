import json
import re
import shutil
import subprocess
from pathlib import Path


class YamlError(Exception):
    """Raised when yq is unavailable or YAML cannot be decoded."""


def load_yaml(path: Path) -> object:
    binary = shutil.which("yq")
    if binary is None:
        raise YamlError("yq v4 is required to read YAML")
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YamlError("yq v4 is required to read YAML") from exc
    if version.returncode != 0 or re.search(r"version v?4\.", version.stdout) is None:
        raise YamlError("yq v4 is required to read YAML")
    try:
        result = subprocess.run(
            [binary, "-o=json", str(path)], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YamlError(f"cannot read YAML file: {path}") from exc
    if result.returncode != 0:
        raise YamlError(f"invalid YAML: {path}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise YamlError(f"invalid YAML: {path}") from exc
