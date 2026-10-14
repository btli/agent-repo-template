import json
from pathlib import Path

from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "tests" / "fixtures" / "releases"
ARTIFACT_SCHEMAS = ("decision", "review", "fix")


def test_release_fixtures_validate():
    if not RELEASES.exists():
        return

    for release in RELEASES.iterdir():
        if not release.is_dir():
            continue

        config_path = release / "config.yml"
        if config_path.is_file():
            errors = validate(load_yaml(config_path), load_schema("config"))
            assert not errors, f"{config_path.relative_to(ROOT)}: {errors}"

        for schema_name in ARTIFACT_SCHEMAS:
            candidates = {
                *release.glob(f"{schema_name}*.json"),
                *release.glob(f"*.{schema_name}.json"),
            }
            for artifact_path in sorted(candidates):
                if artifact_path.is_file():
                    instance = json.loads(artifact_path.read_text(encoding="utf-8"))
                    errors = validate(instance, load_schema(schema_name))
                    assert not errors, f"{artifact_path.relative_to(ROOT)}: {errors}"
