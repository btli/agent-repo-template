import base64
import secrets

import pytest


@pytest.mark.parametrize(
    "kind",
    [
        "literal",
        "base64",
        "base64_unpadded",
        "urlsafe",
        "urlsafe_unpadded",
        "hex",
        "HEX",
        "reversed",
    ],
)
def test_every_encoding_is_detected(kind):
    from agent_repo.scan import encodings, find_leaks

    key = secrets.token_urlsafe(25) + "?~"
    variants = {
        "literal": key,
        "base64": base64.b64encode(key.encode()).decode(),
        "base64_unpadded": base64.b64encode(key.encode()).decode().rstrip("="),
        "urlsafe": base64.urlsafe_b64encode(key.encode()).decode(),
        "urlsafe_unpadded": base64.urlsafe_b64encode(key.encode()).decode().rstrip("="),
        "hex": key.encode().hex(),
        "HEX": key.encode().hex().upper(),
        "reversed": key[::-1],
    }
    assert variants[kind] in encodings(key), "encoding missing"
    assert bool(find_leaks("prefix " + variants[kind] + " suffix", [key], "")), "leak missed"


def test_canary_detected_without_echo():
    from agent_repo.scan import find_leaks

    canary = secrets.token_hex(20)
    hits = find_leaks(canary, [], canary)
    assert hits == ["canary"]


def test_three_character_overlap_is_not_a_leak():
    from agent_repo.scan import find_leaks

    assert find_leaks("abc", ["abc" + secrets.token_hex(20)], "") == []


def test_hit_kinds_never_echo_key():
    from agent_repo.scan import find_leaks

    key = secrets.token_hex(20)
    hits = find_leaks(key, [key], "")
    assert bool(hits), "leak missed"
    assert all(key not in hit for hit in hits), "credential echoed"


def test_empty_values_do_not_match_everything():
    from agent_repo.scan import encodings, find_leaks

    assert encodings("") == []
    assert find_leaks("ordinary text", [""], "") == []
