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


@pytest.mark.parametrize("remainder", [0, 1, 2])
@pytest.mark.parametrize("urlsafe", [False, True], ids=["standard", "urlsafe"])
@pytest.mark.parametrize("padded", [False, True], ids=["unpadded", "padded"])
def test_base64_alignment_and_padding(remainder, urlsafe, padded):
    from agent_repo.scan import find_leaks

    key = "???~~~?~??~?~?~~?~????~???~??~?~~~~~"
    key += "?" * ((remainder - len(key)) % 3)
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    for offset in (0, 1, 2):
        for suffix in (b"", b"suffix"):
            blob = encoder(b"xy"[:offset] + key.encode() + suffix).decode()
            if not padded:
                blob = blob.rstrip("=")
            assert bool(find_leaks(blob, [key], "")), "base64 alignment missed"


@pytest.mark.parametrize("start", [0, 5, 16])
def test_sixteen_character_key_fragments_are_detected(start):
    from agent_repo.scan import find_leaks

    key = "AbCdEf0123456789-?_~XYZabcDEFghiJKL"
    assert bool(find_leaks(key[start : start + 16], [key], "")), "key fragment missed"
    assert not find_leaks(key[start : start + 15], [key], ""), "short overlap matched"


def test_short_key_requires_the_whole_normalized_key():
    from agent_repo.scan import find_leaks

    key = "AbC9xY7"
    assert bool(find_leaks(key.lower(), [key], "")), "short key missed"
    assert not find_leaks(key[:3], [key], ""), "partial short key matched"


@pytest.mark.parametrize("remainder", [0, 1, 2])
@pytest.mark.parametrize("urlsafe", [False, True], ids=["standard", "urlsafe"])
def test_canary_uses_all_base64_alignments(remainder, urlsafe):
    from agent_repo.scan import find_leaks

    canary = "???~~~?~??~?~?~~?~????~???~??~?~~~~~"
    canary += "?" * ((remainder - len(canary)) % 3)
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    for offset in (0, 1, 2):
        value = encoder(b"xy"[:offset] + canary.encode() + b"suffix").decode()
        for blob in (value, value.rstrip("=")):
            assert find_leaks(blob, [], canary) == ["canary"]


@pytest.mark.parametrize("encoding", ["hex", "reversed", "normalized"])
def test_canary_uses_key_encodings_and_normalization(encoding):
    from agent_repo.scan import find_leaks

    canary = "Canary-AbCdEf0123456789-?_~XYZabc"
    values = {
        "hex": canary.encode().hex(),
        "reversed": canary[::-1],
        "normalized": "\u200b".join(canary.upper()),
    }
    assert find_leaks(values[encoding], [], canary) == ["canary"]
