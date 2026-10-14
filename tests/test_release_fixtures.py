import json
import sys
from pathlib import Path

import pytest
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "tests" / "fixtures" / "releases"
ARTIFACT_SCHEMAS = ("decision", "review", "fix")


def _validate_release_fixtures():
    if not RELEASES.exists():
        return

    for release in RELEASES.iterdir():
        if not release.is_dir():
            continue

        for fixture in sorted(path for path in release.iterdir() if path.is_file()):
            if fixture.name == "config.yml":
                instance = load_yaml(fixture)
                schema_name = "config"
            else:
                schema_name = next(
                    (
                        name
                        for name in ARTIFACT_SCHEMAS
                        if (fixture.name.startswith(name) and fixture.suffix == ".json")
                        or fixture.name.endswith(f".{name}.json")
                    ),
                    None,
                )
                assert schema_name is not None, (
                    f"unrecognized release fixture: {fixture.relative_to(ROOT)}"
                )
                instance = json.loads(fixture.read_text(encoding="utf-8"))

            errors = validate(instance, load_schema(schema_name))
            assert not errors, f"{fixture.relative_to(ROOT)}: {errors}"


def test_release_fixtures_validate():
    _validate_release_fixtures()


def test_unknown_release_fixture_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = tmp_path / "v0.1.0"
    release.mkdir()
    (release / "notes.txt").write_text("unmapped fixture")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "RELEASES", tmp_path)

    with pytest.raises(AssertionError, match="unrecognized release fixture"):
        _validate_release_fixtures()
