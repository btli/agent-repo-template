import json
from pathlib import Path

from agent_repo.schema import SUPPORTED_KEYWORDS, load_schema, validate


def test_required_key_missing_has_path_error():
    assert validate({}, {"type": "object", "required": ["a"]}) == ["$.a: required"]


def test_unknown_key_is_rejected():
    schema = {"type": "object", "additionalProperties": False}
    assert validate({"x": 1}, schema) == ["$.x: unexpected"]


def test_enum_const_type_and_pattern_mismatches_each_give_one_error():
    cases = [
        ("bad", {"enum": ["good"]}),
        ("bad", {"const": "good"}),
        ("bad", {"type": "integer"}),
        ("bad", {"pattern": "^[0-9]+$"}),
    ]
    for instance, schema in cases:
        assert len(validate(instance, schema)) == 1


def test_bool_is_not_an_integer():
    assert validate(True, {"type": "integer"})


def test_union_type_accepts_none():
    assert validate(None, {"type": ["object", "null"]}) == []


def test_array_string_and_number_boundaries():
    assert validate([1, 2], {"type": "array", "maxItems": 2}) == []
    assert validate([1, 2, 3], {"type": "array", "maxItems": 2})
    assert validate("ab", {"type": "string", "maxLength": 2}) == []
    assert validate("abc", {"type": "string", "maxLength": 2})
    assert validate(2, {"type": "number", "minimum": 2}) == []
    assert validate(1, {"type": "number", "minimum": 2})
    assert validate(2, {"type": "number", "maximum": 2}) == []
    assert validate(3, {"type": "number", "maximum": 2})


def test_schemas_use_only_supported_keywords():
    schema_dir = Path(__file__).resolve().parents[1] / "schemas"
    paths = list(schema_dir.glob("*.json"))
    assert paths
    for path in paths:
        schema = json.loads(path.read_text())

        def visit(node, schema_path):
            assert set(node) <= SUPPORTED_KEYWORDS, (
                schema_path,
                set(node) - SUPPORTED_KEYWORDS,
            )
            for child in node.get("properties", {}).values():
                visit(child, schema_path)
            if isinstance(node.get("items"), dict):
                visit(node["items"], schema_path)
            if isinstance(node.get("additionalProperties"), dict):
                visit(node["additionalProperties"], schema_path)

        visit(schema, path)


def test_every_schema_has_schema_version_const():
    schema_dir = Path(__file__).resolve().parents[1] / "schemas"
    paths = [schema_dir / f"{name}.schema.json" for name in ("decision", "review", "fix")]
    for path in paths:
        schema = json.loads(path.read_text())
        assert schema["properties"]["schema_version"] == {"const": "1"}


def test_config_schema_has_version_const():
    schema = load_schema("config")
    assert "version" in schema["required"]
    assert schema["properties"]["version"] == {"const": 1}
    assert "schema_version" not in schema["properties"]


def test_config_schema_loads_from_repository():
    assert load_schema("config")["properties"]["version"]["const"] == 1
