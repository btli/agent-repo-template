import base64
import html
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


def _normalize(text: str) -> str:
    while True:
        decoded = unicodedata.normalize("NFKC", html.unescape(unquote(text)))
        normalized = "".join(
            char
            for char in decoded
            if not char.isspace()
            and unicodedata.category(char) not in {"Cf", "Cc", "Cs", "Mn", "Me"}
            and char not in "\u115f\u1160\u2800"
        ).casefold()
        if normalized == text:
            return normalized
        text = normalized


def _matches(text: str, variant: str) -> bool:
    variant = _normalize(variant)
    width = min(16, len(variant))
    return bool(width) and any(
        variant[start : start + width] in text for start in range(len(variant) - width + 1)
    )


def find_leaks(text: str, secrets: list[str], canary: str) -> list[str]:
    text = _normalize(text)
    hits = {
        kind
        for secret in secrets
        for kind, value in _variants(secret).items()
        if _matches(text, value)
    }
    if any(_matches(text, value) for value in _variants(canary).values()):
        hits.add("canary")
    return sorted(hits)
