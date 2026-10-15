import json
import math
import re
from pathlib import Path

SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "items",
        "maxItems",
        "minItems",
    }
)


def load_schema(name: str) -> dict:
    if not re.fullmatch(r"[a-z_]+", name):
        raise ValueError("invalid schema name")
    path = Path(__file__).resolve().parents[2] / "schemas" / f"{name}.schema.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load schema {name!r}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"schema {name!r} must be an object")
    return value


def _matches_type(instance: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def validate(instance: object, schema: dict) -> list[str]:
    errors: list[str] = []

    def visit(value: object, rule: dict, path: str) -> None:
        expected = rule.get("type")
        if expected is not None:
            types = expected if isinstance(expected, list) else [expected]
            if not any(_matches_type(value, kind) for kind in types):
                errors.append(f"{path}: type")
                return

        if "const" in rule and not _json_equal(value, rule["const"]):
            errors.append(f"{path}: const")
            return
        if "enum" in rule and not any(_json_equal(value, option) for option in rule["enum"]):
            errors.append(f"{path}: enum")
            return

        if isinstance(value, dict):
            for key in rule.get("required", []):
                if key not in value:
                    errors.append(f"{path}.{key}: required")
            properties = rule.get("properties", {})
            additional = rule.get("additionalProperties", True)
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in properties:
                    visit(child, properties[key], child_path)
                elif additional is False:
                    errors.append(f"{child_path}: unexpected")
                elif isinstance(additional, dict):
                    visit(child, additional, child_path)

        if isinstance(value, list):
            if len(value) < rule.get("minItems", 0):
                errors.append(f"{path}: minItems")
            if len(value) > rule.get("maxItems", math.inf):
                errors.append(f"{path}: maxItems")
            if "items" in rule:
                for index, child in enumerate(value):
                    visit(child, rule["items"], f"{path}[{index}]")

        if isinstance(value, str):
            if len(value) < rule.get("minLength", 0):
                errors.append(f"{path}: minLength")
            if len(value) > rule.get("maxLength", math.inf):
                errors.append(f"{path}: maxLength")
            if "pattern" in rule:
                pattern = rule["pattern"]
                if pattern.endswith("$") and (len(pattern) - len(pattern.rstrip("\\"))) % 2 == 0:
                    pattern = pattern[:-1] + r"\Z"
                if re.search(pattern, value) is None:
                    errors.append(f"{path}: pattern")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"{path}: number")
                return
            if value < rule.get("minimum", -math.inf):
                errors.append(f"{path}: minimum")
            if value > rule.get("maximum", math.inf):
                errors.append(f"{path}: maximum")

    visit(instance, schema, "$")
    return errors
