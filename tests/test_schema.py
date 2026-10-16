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


def test_anchored_pattern_rejects_trailing_newline():
    fix = load_schema("fix")
    assert validate("a" * 40 + "\n", fix["properties"]["base_sha"])
    assert validate("price$", {"type": "string", "pattern": r"price\$"}) == []
    escaped_backslash_anchor = {"type": "string", "pattern": r"path\\$"}
    assert validate("path\\", escaped_backslash_anchor) == []
    assert validate("path\\\n", escaped_backslash_anchor)


def test_const_and_enum_use_json_type_equality():
    decision = load_schema("decision")
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "tier": True,
        "answers": {},
        "labels": [],
        "suggestions": {"priority": None, "labels": []},
        "needs_info": None,
        "rca": None,
        "close": None,
        "escalate": None,
        "security": False,
        "notes": [],
    }
    assert "$.tier: enum" in validate(artifact, decision)
    assert validate({"child": True}, {"const": {"child": 1}})
    assert validate({"child": True}, {"enum": [{"child": 1}]})


def test_bool_is_not_an_integer():
    assert validate(True, {"type": "integer"})
    assert validate(True, {"type": "number"}) == ["$: type"]


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


def test_minimum_string_length_boundary():
    schema = {"type": "string", "minLength": 1}
    assert validate("x", schema) == []
    assert validate("", schema) == ["$: minLength"]


def test_minimum_array_items_boundary():
    schema = {"type": "array", "minItems": 1}
    assert validate(["x"], schema) == []
    assert validate([], schema) == ["$: minItems"]


def test_review_schema_allows_thirty_findings_and_rejects_thirty_one():
    schema = load_schema("review")

    def artifact_with_findings(count):
        return {
            "schema_version": "1",
            "run_id": "review-1",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "summary": "",
            "findings": [
                {"severity": "low", "path": "src/file.py", "line": 1, "body": "finding"}
                for _ in range(count)
            ],
            "scope_ok": True,
            "tests_missing": False,
            "docs_needed": False,
            "escalate": None,
        }

    assert validate(artifact_with_findings(30), schema) == []
    assert "$.findings: maxItems" in validate(artifact_with_findings(31), schema)


def test_non_finite_number_is_rejected_in_complete_decision_artifact():
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "tier": 1,
        "answers": {"q1": {"value": "yes", "p": float("nan")}},
        "labels": [],
        "suggestions": {"priority": None, "labels": []},
        "needs_info": None,
        "rca": None,
        "close": None,
        "escalate": None,
        "security": False,
        "notes": [],
    }
    assert "$.answers.q1.p: number" in validate(artifact, load_schema("decision"))
    for value in (float("nan"), float("inf"), float("-inf")):
        assert validate(value, {"type": "number"}) == ["$: number"]


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
