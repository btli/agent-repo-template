import base64
import html
import re
import unicodedata
from urllib.parse import unquote


def _variants(secret: str) -> dict[str, str]:
    if not secret:
        return {}
    data = secret.encode("utf-8")
    standard = base64.b64encode(data).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(data).decode("ascii")
    hexadecimal = data.hex()
    variants = {
        "literal": secret,
        "base64": standard,
        "base64_unpadded": standard.rstrip("="),
        "base64_urlsafe": urlsafe,
        "base64_urlsafe_unpadded": urlsafe.rstrip("="),
        "hex_lower": hexadecimal,
        "hex_upper": hexadecimal.upper(),
        "reversed": secret[::-1],
    }
    for offset in (1, 2):
        for kind, encoder in (
            ("base64", base64.b64encode),
            ("base64_urlsafe", base64.urlsafe_b64encode),
        ):
            encoded = encoder(b"\0" * offset + data).decode("ascii")
            start = (offset * 8 + 5) // 6
            end = ((offset + len(data)) * 8) // 6
            core = encoded[start:end]
            if core:
                variants[f"{kind}_offset_{offset}"] = core
    return variants


def encodings(secret: str) -> list[str]:
    return list(dict.fromkeys(_variants(secret).values()))


class _ScanLimit(ValueError):
    pass


def _spend(budget: list[int], amount: int) -> None:
    budget[0] -= amount
    if budget[0] < 0:
        raise _ScanLimit


def _normalize(text: str, budget: list[int]) -> str:
    for _ in range(3):
        if len(text) > 65536:
            raise _ScanLimit
        _spend(budget, len(text))
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote(text)))
        if decoded == text:
            break
        text = decoded
    else:
        raise _ScanLimit
    text = re.sub(
        r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))",
        lambda match: chr(int(match[1] or match[2], 16)),
        text,
    )
    text = unicodedata.normalize("NFKC", text)
    if len(text) > 65536:
        raise _ScanLimit
    _spend(budget, len(text))
    normalized = "".join(
        char
        for char in text
        if not char.isspace()
        and unicodedata.category(char) not in {"Cf", "Cc", "Cs", "Mn", "Me"}
        and char not in "\u115f\u1160\u2800"
    ).casefold()
    if len(normalized) > 65536:
        raise _ScanLimit
    _spend(budget, len(normalized))
    return normalized


def _patterns(secret: str):
    prefix = max(secret[:16].rfind("-"), secret[:16].rfind("_")) + 1
    private = secret[prefix:]
    if len(private.encode("utf-8")) < 12:
        for kind, value in _variants(secret).items():
            if "offset" not in kind:
                yield kind, value
        return
    for kind, value in _variants(private).items():
        if kind in {"literal", "reversed"}:
            for start in range(len(value)):
                end = start
                size = 0
                while end < len(value) and size < 12:
                    size += len(value[end].encode("utf-8"))
                    end += 1
                if size >= 12:
                    yield kind, value[start:end]
        else:
            width = 24 if kind.startswith("hex") else 16
            value = value.rstrip("=")
            for start in range(len(value) - width + 1):
                yield kind, value[start : start + width]


def find_leaks(text: str, secrets: list[str], canary: str) -> list[str]:
    # Defense in depth: bounded decoding and one separator comparison cover only known forms.
    budget = [8_000_000]
    try:
        text = _normalize(text, budget)
        compact = re.sub("[^a-z0-9]", "", text)
        hits = set()
        for secret, is_canary in [(secret, False) for secret in secrets] + [(canary, True)]:
            for kind, value in _patterns(secret):
                kind = "canary" if is_canary else kind
                if kind in hits:
                    continue
                value = _normalize(value, budget)
                alphanumeric = re.sub("[^a-z0-9]", "", value)
                _spend(budget, len(text) + len(compact))
                if (value and value in text) or (alphanumeric and alphanumeric in compact):
                    hits.add(kind)
        return sorted(hits)
    except _ScanLimit:
        return ["scan_budget"]
