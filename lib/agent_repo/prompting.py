import json
import re
import secrets


def untrusted_block(label: str, text: str) -> str:
    if re.fullmatch(r"[a-z_]+", label) is None:
        raise ValueError("invalid data label")
    nonce = secrets.token_hex(16)
    begin = f"[[BEGIN_UNTRUSTED_{label}_{nonce}]]"
    end = f"[[END_UNTRUSTED_{label}_{nonce}]]"
    escaped = text.replace(begin, begin.replace("[", r"\u005b")).replace(
        end, end.replace("[", r"\u005b")
    )
    return f"{begin}\n{escaped}\n{end}"


def render_prompt(
    template: str, *, canary: str, blocks: dict[str, str], context: dict[str, str]
) -> str:
    if not canary or template.count("{{canary}}") != 1:
        raise ValueError("prompt must contain one canary slot")
    values = {"canary": canary}
    values.update({f"block:{key}": untrusted_block(key, value) for key, value in blocks.items()})
    values.update({f"context:{key}": value for key, value in context.items()})

    def replace(match: re.Match) -> str:
        try:
            return values[match[1]]
        except KeyError:
            raise ValueError("unknown prompt slot") from None

    result = re.sub(r"\{\{([^{}]+)\}\}", replace, template)
    if result.count(canary) != 1:
        raise ValueError("prompt must contain the canary exactly once")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


def extract_json(output: str) -> dict:
    fences = re.findall(
        r"^```json[^\S\n]*\n(.*?)^```[^\S\n]*(?:\n|$)", output, re.MULTILINE | re.DOTALL
    )
    if not fences:
        raise ValueError("no JSON output")
    try:
        value = json.loads(fences[-1], parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ValueError("invalid JSON output") from None
    if not isinstance(value, dict):
        raise ValueError("JSON output must be an object")
    return value
