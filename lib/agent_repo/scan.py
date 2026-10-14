import base64


def _variants(secret: str) -> dict[str, str]:
    if not secret:
        return {}
    data = secret.encode("utf-8")
    standard = base64.b64encode(data).decode("ascii")
    urlsafe = base64.urlsafe_b64encode(data).decode("ascii")
    hexadecimal = data.hex()
    return {
        "literal": secret,
        "base64": standard,
        "base64_unpadded": standard.rstrip("="),
        "base64_urlsafe": urlsafe,
        "base64_urlsafe_unpadded": urlsafe.rstrip("="),
        "hex_lower": hexadecimal,
        "hex_upper": hexadecimal.upper(),
        "reversed": secret[::-1],
    }


def encodings(secret: str) -> list[str]:
    return list(dict.fromkeys(_variants(secret).values()))


def find_leaks(text: str, secrets: list[str], canary: str) -> list[str]:
    hits = {
        kind for secret in secrets for kind, value in _variants(secret).items() if value in text
    }
    if canary and canary in text:
        hits.add("canary")
    return sorted(hits)
