import copy
import json
from pathlib import Path

import pytest
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


@pytest.mark.parametrize("system_one", ["absent", None])
def test_config_accepts_absent_or_null_system_one(system_one):
    config = _valid_schema_instance("config")
    if system_one == "absent":
        del config["system_one"]
    else:
        config["system_one"] = system_one
    assert validate(config, load_schema("config")) == []


def test_decision_accepts_llm_only_full_question_answers():
    decision = _valid_schema_instance("decision")
    decision["mode"] = "llm-only"
    decision["answers"] = {
        "type": {"value": "bug", "p": 0.8},
        "area": {"value": "core", "p": 0.8},
        "priority": {"value": "P2", "p": 0.8},
        "needs_info": {"value": False, "p": 0.8},
        "out_of_scope": {"value": False, "p": 0.8},
        "docs_ref": {"value": "none", "p": 0.8},
        "duplicate_of": {"value": "none", "p": 0.8},
        "security_sensitive": {"value": False, "p": 0.8},
    }
    assert validate(decision, load_schema("decision")) == []

    decision["answers"]["security_sensitive"]["p"] = 1.1
    assert "$.answers.security_sensitive.p: maximum" in validate(decision, load_schema("decision"))


def test_array_string_and_number_boundaries():
    assert validate([1, 2], {"type": "array", "maxItems": 2}) == []
    assert validate([1, 2, 3], {"type": "array", "maxItems": 2})
    assert validate("ab", {"type": "string", "maxLength": 2}) == []
    assert validate("abc", {"type": "string", "maxLength": 2})
    assert validate(2, {"type": "number", "minimum": 2}) == []
    assert validate(1, {"type": "number", "minimum": 2})
    assert validate(2, {"type": "number", "maximum": 2}) == []
    assert validate(3, {"type": "number", "maximum": 2})


def test_array_items_are_validated_individually():
    assert validate(["a", 1], {"type": "array", "items": {"type": "string"}}) == ["$[1]: type"]


def test_complete_fix_artifact_rejects_unsafe_test_id():
    artifact = {
        "schema_version": "1",
        "run_id": "fix-1",
        "base_sha": "a" * 40,
        "status": "patch",
        "rca": "",
        "tests": ["x; rm -rf /"],
        "risk": "low",
        "summary": "",
        "attempted": "",
        "escalate": None,
    }
    assert "$.tests[0]: pattern" in validate(artifact, load_schema("fix"))


def test_complete_decision_artifact_enforces_note_length():
    artifact = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": "a" * 40,
        "tier": 1,
        "answers": {},
        "labels": [],
        "suggestions": {"priority": None, "labels": []},
        "needs_info": None,
        "rca": None,
        "close": None,
        "escalate": None,
        "security": False,
        "notes": ["x" * 501],
    }
    assert "$.notes[0]: maxLength" in validate(artifact, load_schema("decision"))


def test_decision_and_review_artifacts_validate_base_sha_patterns():
    artifacts = [
        (
            load_schema("decision"),
            {
                "schema_version": "1",
                "run_id": "run-1",
                "base_sha": "a" * 40,
                "tier": 1,
                "answers": {},
                "labels": [],
                "suggestions": {"priority": None, "labels": []},
                "needs_info": None,
                "rca": None,
                "close": None,
                "escalate": None,
                "security": False,
                "notes": [],
            },
        ),
        (
            load_schema("review"),
            {
                "schema_version": "1",
                "run_id": "review-1",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "summary": "",
                "findings": [],
                "scope_ok": True,
                "tests_missing": False,
                "docs_needed": False,
                "escalate": None,
            },
        ),
    ]
    for schema, artifact in artifacts:
        for invalid_sha in ("A" * 40, "a" * 39):
            artifact["base_sha"] = invalid_sha
            assert "$.base_sha: pattern" in validate(artifact, schema)


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
            node_type = node.get("type")
            if node_type == "object" or (isinstance(node_type, list) and "object" in node_type):
                additional = node.get("additionalProperties")
                if isinstance(additional, dict):
                    assert additional.get("additionalProperties") is False, schema_path
                else:
                    assert additional is False, schema_path
            for child in node.get("properties", {}).values():
                visit(child, schema_path)
            if isinstance(node.get("items"), dict):
                visit(node["items"], schema_path)
            if isinstance(node.get("additionalProperties"), dict):
                visit(node["additionalProperties"], schema_path)

        visit(schema, path)


def test_complete_review_artifact_rejects_event_field():
    artifact = {
        "schema_version": "1",
        "run_id": "review-1",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "summary": "",
        "findings": [],
        "scope_ok": True,
        "tests_missing": False,
        "docs_needed": False,
        "escalate": None,
        "event": "APPROVE",
    }
    assert validate(artifact, load_schema("review")) == ["$.event: unexpected"]


def _valid_schema_instance(name):
    if name == "config":
        return {
            "version": 1,
            "mode": {"triage": "dry-run", "review": "live", "fix": "dry-run"},
            "system_one": {"provider": "jev", "model": "model"},
            "models": {"triage": "t", "review": "r", "fix": "f"},
            "thresholds": {"apply_min": 0.5, "close_min": 0.5, "close_lexical_min": 0.5},
            "close": {"enabled": False},
            "triage": {"rca_authors": "all"},
            "limits": {
                "triage_runs_per_issue": 1,
                "tier2_per_author_per_day": 1,
                "tier2_per_day": 1,
                "edit_debounce_minutes": 1,
                "review_max_lines": 1,
                "fix_max_turns": 1,
                "fix_max_diff_lines": 1,
            },
            "commands": {"setup": "", "test": "", "lint": ""},
            "fix": {"allow_new_deps": False, "accept_ci_exposure": False},
        }
    if name == "decision":
        return {
            "schema_version": "1",
            "run_id": "run-1",
            "base_sha": "a" * 40,
            "tier": 1,
            "answers": {"q1": {"value": "yes", "p": 0.5}},
            "labels": [],
            "suggestions": {"priority": None, "labels": []},
            "needs_info": {"text": ""},
            "rca": {
                "hypothesis": "",
                "locations": [{"path": "src/file.py", "line": 1}],
                "confidence": "low",
                "confirm_with": "",
            },
            "close": {
                "kind": "duplicate",
                "target": 1,
                "docs_ref": None,
                "p": 0.5,
                "lexical": 0.5,
                "tier2_agrees": False,
            },
            "escalate": {"to": "maintainer", "reason": ""},
            "security": False,
            "notes": [""],
        }
    if name == "review":
        return {
            "schema_version": "1",
            "run_id": "review-1",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "summary": "",
            "findings": [{"severity": "low", "path": "src/file.py", "line": 1, "body": ""}],
            "scope_ok": True,
            "tests_missing": False,
            "docs_needed": False,
            "escalate": {"to": "maintainer", "reason": ""},
        }
    return {
        "schema_version": "1",
        "run_id": "fix-1",
        "base_sha": "a" * 40,
        "status": "patch",
        "rca": "",
        "tests": ["tests/test_one.py"],
        "risk": "low",
        "summary": "",
        "attempted": "",
        "escalate": {"to": "maintainer", "reason": ""},
    }


def _set_schema_path(instance, path, value):
    parent = instance
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value


def _schema_path(path):
    result = "$"
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


@pytest.mark.parametrize("name", ["config", "decision", "fix", "review"])
def test_schema_contract_bounds_enums_and_required_values(name):
    schema = load_schema(name)
    bounds = {
        "config": [
            (("system_one", "model"), "minLength", 1),
            (("models", "triage"), "minLength", 1),
            (("models", "review"), "minLength", 1),
            (("models", "fix"), "minLength", 1),
            (("thresholds", "apply_min"), "minimum", 0),
            (("thresholds", "apply_min"), "maximum", 1),
            (("thresholds", "close_min"), "minimum", 0),
            (("thresholds", "close_min"), "maximum", 1),
            (("thresholds", "close_lexical_min"), "minimum", 0),
            (("thresholds", "close_lexical_min"), "maximum", 1),
            (("limits", "triage_runs_per_issue"), "minimum", 1),
            (("limits", "tier2_per_author_per_day"), "minimum", 1),
            (("limits", "tier2_per_day"), "minimum", 1),
            (("limits", "edit_debounce_minutes"), "minimum", 1),
            (("limits", "review_max_lines"), "minimum", 1),
            (("limits", "fix_max_turns"), "minimum", 1),
            (("limits", "fix_max_diff_lines"), "minimum", 1),
        ],
        "decision": [
            (("run_id",), "minLength", 1),
            (("answers", "q1", "p"), "minimum", 0),
            (("answers", "q1", "p"), "maximum", 1),
            (("needs_info", "text"), "maxLength", 2000),
            (("rca", "hypothesis"), "maxLength", 4000),
            (("rca", "locations"), "maxItems", 10),
            (("rca", "locations", 0, "line"), "minimum", 1),
            (("rca", "confirm_with"), "maxLength", 1000),
            (("close", "target"), "minimum", 1),
            (("close", "p"), "minimum", 0),
            (("close", "p"), "maximum", 1),
            (("close", "lexical"), "minimum", 0),
            (("close", "lexical"), "maximum", 1),
            (("escalate", "reason"), "maxLength", 500),
            (("notes",), "maxItems", 20),
            (("notes", 0), "maxLength", 500),
        ],
        "fix": [
            (("run_id",), "minLength", 1),
            (("rca",), "maxLength", 4000),
            (("tests",), "maxItems", 20),
            (("summary",), "maxLength", 4000),
            (("attempted",), "maxLength", 4000),
            (("escalate", "reason"), "maxLength", 500),
        ],
        "review": [
            (("run_id",), "minLength", 1),
            (("summary",), "maxLength", 4000),
            (("findings",), "maxItems", 30),
            (("findings", 0, "line"), "minimum", 1),
            (("findings", 0, "body"), "maxLength", 2000),
            (("escalate", "reason"), "maxLength", 500),
        ],
    }[name]
    enums = {
        "config": [
            (("mode", "triage"), "off"),
            (("mode", "review"), "off"),
            (("mode", "fix"), "off"),
            (("system_one", "provider"), "x"),
            (("triage", "rca_authors"), "none"),
        ],
        "decision": [
            (("tier",), 3),
            (("suggestions", "priority"), "P2"),
            (("rca", "confidence"), "certain"),
            (("close", "kind"), "other"),
        ],
        "fix": [(("status",), "partial"), (("risk",), "none")],
        "review": [(("findings", 0, "severity"), "nit")],
    }[name]
    constants = {
        "config": [(("version",), 2)],
        "decision": [
            (("schema_version",), "2"),
            (("escalate", "to"), "other"),
        ],
        "fix": [
            (("schema_version",), "2"),
            (("escalate", "to"), "other"),
        ],
        "review": [
            (("schema_version",), "2"),
            (("escalate", "to"), "other"),
        ],
    }[name]
    patterns = {
        "config": [],
        "decision": [(("base_sha",), "A" * 40)],
        "fix": [(("base_sha",), "not-a-sha")],
        "review": [(("base_sha",), "not-a-sha"), (("head_sha",), "not-a-sha")],
    }[name]

    def values_for(keyword, limit):
        if keyword == "minimum":
            return limit, limit - 1
        if keyword == "maximum":
            return limit, limit + 1
        if keyword == "minLength":
            return "x" * limit, "x" * (limit - 1)
        if keyword == "maxLength":
            return "x" * limit, "x" * (limit + 1)
        if keyword == "maxItems":
            item = "x"
            if name == "decision" and limit == 10:
                item = {"path": "src/file.py", "line": 1}
            elif name == "review":
                item = {"severity": "low", "path": "src/file.py", "line": 1, "body": ""}
            return [item] * limit, [item] * (limit + 1)
        raise AssertionError(keyword)

    for path, keyword, limit in bounds:
        valid_value, invalid_value = values_for(keyword, limit)
        valid = copy.deepcopy(_valid_schema_instance(name))
        _set_schema_path(valid, path, valid_value)
        assert validate(valid, schema) == [], (name, path, "at limit")
        invalid = copy.deepcopy(_valid_schema_instance(name))
        _set_schema_path(invalid, path, invalid_value)
        assert f"{_schema_path(path)}: {keyword}" in validate(invalid, schema)

    for path, invalid_value in enums + constants:
        invalid = copy.deepcopy(_valid_schema_instance(name))
        _set_schema_path(invalid, path, invalid_value)
        keyword = "enum" if (path, invalid_value) in enums else "const"
        assert f"{_schema_path(path)}: {keyword}" in validate(invalid, schema)

    for path, invalid_value in patterns:
        invalid = copy.deepcopy(_valid_schema_instance(name))
        _set_schema_path(invalid, path, invalid_value)
        assert f"{_schema_path(path)}: pattern" in validate(invalid, schema)

    if name == "config":
        wrong_type = copy.deepcopy(_valid_schema_instance(name))
        wrong_type["limits"]["review_max_lines"] = 1.5
        assert "$.limits.review_max_lines: type" in validate(wrong_type, schema)
        missing_required = copy.deepcopy(_valid_schema_instance(name))
        del missing_required["limits"]["fix_max_turns"]
        assert "$.limits.fix_max_turns: required" in validate(missing_required, schema)
    if name == "decision":
        wrong_type = copy.deepcopy(_valid_schema_instance(name))
        wrong_type["security"] = "false"
        assert "$.security: type" in validate(wrong_type, schema)
    if name == "fix":
        wrong_pattern = copy.deepcopy(_valid_schema_instance(name))
        wrong_pattern["tests"] = ["unsafe test id"]
        assert "$.tests[0]: pattern" in validate(wrong_pattern, schema)


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
