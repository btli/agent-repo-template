import fnmatch
import json
import re
import tomllib
from pathlib import PurePosixPath

MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "package.json",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "composer.json",
        "pipfile",
        "setup.py",
        "setup.cfg",
        "environment.yml",
        "environment.yaml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "mix.exs",
        "pubspec.yaml",
    }
)


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return name in MANIFESTS or fnmatch.fnmatchcase(name, "requirements*.txt")


def _python_names(values: list) -> set[str]:
    names = set()
    if not isinstance(values, list):
        raise ValueError
    for value in values:
        if not isinstance(value, str):
            raise ValueError
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?=$|\s*(?:\[|[<>=!~;@]))", value.strip())
        if not match:
            raise ValueError
        names.add(re.sub(r"[-_.]+", "-", match[0]).casefold())
    return names


def _dependencies(path: str, text: str) -> set[str]:
    if not text.strip():
        return set()
    name = PurePosixPath(path).name.casefold()
    if fnmatch.fnmatchcase(name, "requirements*.txt"):
        return _python_names(
            [
                line.split("#", 1)[0].strip()
                for line in text.splitlines()
                if line.split("#", 1)[0].strip()
            ]
        )
    if name == "pyproject.toml":
        data = tomllib.loads(text)
        project = data.get("project", {})
        groups = [project.get("dependencies", [])]
        groups.extend(project.get("optional-dependencies", {}).values())
        groups.extend(data.get("dependency-groups", {}).values())
        return set().union(*(_python_names(group) for group in groups))
    if name == "package.json":
        data = json.loads(text)
        names = set()
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            values = data.get(key, {})
            if not isinstance(values, dict):
                raise ValueError
            names.update(values)
        return names
    raise ValueError


def new_dependencies(path: str, before: str, after: str) -> set[str]:
    if not is_manifest(path) or before == after:
        return set()
    try:
        return _dependencies(path, after) - _dependencies(path, before)
    except (ValueError, TypeError, AttributeError):
        return {f"{path}: unparsed change"}
