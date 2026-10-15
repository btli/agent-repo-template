import base64
import secrets
from time import perf_counter

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
def test_private_key_fragments_require_twelve_bytes(start):
    from agent_repo.scan import find_leaks

    private = "AbCdEf0123456789GhIjKlMnOpQrStUvWxYz"
    prefix = "sk-ant-api03-"
    key = prefix + private
    assert not find_leaks(prefix, [key], ""), "public prefix matched"
    assert bool(find_leaks(private[start : start + 12], [key], "")), "private fragment missed"
    assert not find_leaks(private[start : start + 11], [key], ""), "short overlap matched"


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


@pytest.mark.parametrize("form", ["literal", "hex", "base64"])
@pytest.mark.parametrize("public", ["prefix", "documentation", "prose", "sha"])
def test_public_material_is_not_a_key_fragment(form, public):
    from agent_repo.scan import find_leaks

    key = "sk-ant-api03-" + "AbGhJkMnPqRsTuVwXyZ1234567890CdEf"
    value = {
        "prefix": "sk-ant-a",
        "documentation": "The provider format begins sk-ant-api03- followed by private material.",
        "prose": "Use the configured gateway credential for a request.",
        "sha": "0123456789abcdef0123456789abcdef01234567",
    }[public]
    if form == "hex":
        value = value.encode().hex()
    elif form == "base64":
        value = base64.b64encode(value.encode()).decode()
    assert not find_leaks(value, [key], ""), "public material matched"


@pytest.mark.parametrize("form", ["hex", "base64"])
def test_encoded_fragments_measure_original_private_bytes(form):
    from agent_repo.scan import find_leaks

    private = "AbCdEf0123456789GhIjKlMnOpQrStUvWxYz"
    key = "sk-ant-api03-" + private
    encoder = (
        (lambda value: value.hex())
        if form == "hex"
        else (lambda value: base64.b64encode(value).decode())
    )
    assert not find_leaks(encoder(b"sk-ant-api03-"), [key], ""), "public bytes matched"
    assert not find_leaks(encoder(private[:11].encode()), [key], ""), "short bytes matched"
    assert bool(find_leaks(encoder(private[:12].encode()), [key], "")), "private bytes missed"


def test_short_private_suffix_needs_the_whole_key():
    from agent_repo.scan import find_leaks

    key = "sk-ant-api03-" + "AbCdE"
    assert not find_leaks("Provider prefix: " + key[:-1], [key], ""), "prefix matched"
    assert bool(find_leaks(key, [key], "")), "whole key missed"


def test_scan_at_length_cap_is_fast_and_accepts_clean_text(record_property):
    from agent_repo.scan import find_leaks

    text = ("ordinaryprose" * 6000)[:65536]
    keys = [secrets.token_urlsafe(512) for _ in range(4)]
    canary = secrets.token_hex(20)
    started = perf_counter()
    hits = find_leaks(text, keys, canary)
    elapsed = perf_counter() - started
    record_property("scan_seconds", elapsed)
    assert not hits, "clean text at the length cap was rejected"
    assert elapsed < 2, "scan at the length cap exceeded bounded runtime"


@pytest.mark.parametrize("cap", ["input", "intermediate", "casefold"])
def test_scan_length_caps_fail_closed_independently(cap):
    from agent_repo.scan import find_leaks

    payload = {
        "input": "%41" * 21846,
        "intermediate": r"\uFDFA" * 4000,
        "casefold": "\u0130" * 32769,
    }[cap]
    assert find_leaks(payload, [], "") == ["scan_budget"], "length cap did not fail closed"


@pytest.mark.parametrize("separator", ["-", "_"])
@pytest.mark.parametrize(
    "prefix",
    [
        "sk-ant-api03-",
        "sk-ant-",
        "sk-proj-",
        "sk-",
        "dapi",
        "ghp_",
        "gho_",
        "ghs_",
        "ghu_",
        "github_pat_",
        "xai-",
        "",
    ],
    ids=[
        "anthropic-api",
        "anthropic",
        "project",
        "openai",
        "databricks",
        "pat",
        "oauth",
        "app",
        "user",
        "fine-grained",
        "xai",
        "unknown",
    ],
)
def test_private_delimiters_do_not_become_public_prefixes(prefix, separator):
    from agent_repo.scan import find_leaks

    private = "Ab" + separator + "CdEfG0123HiJkLmN4567OpQrStUv"
    hits = find_leaks(private[:12], [prefix + private], "")
    assert bool(hits), "private bytes were excluded as a public prefix"


@pytest.mark.parametrize("form", ["percent", "html"])
def test_two_decode_layers_still_detect_keys(form):
    from agent_repo.scan import find_leaks

    key = secrets.token_hex(20)
    value = key
    for _ in range(2):
        value = (
            "".join(f"%{ord(char):02X}" for char in value)
            if form == "percent"
            else "".join(f"&#{ord(char)};" for char in value)
        )
    assert bool(find_leaks(value, [key], "")), "bounded nested decoding missed key"


@pytest.mark.parametrize("remainder", [1, 2])
def test_base64_padding_does_not_count_as_private_bytes(remainder):
    from agent_repo.scan import find_leaks

    private = "AbCdEf0123456789GhIjKlMnOpQrStUvWxYz"
    private += "Z" * ((remainder - len(private)) % 3)
    key = "sk-ant-api03-" + private
    fragment = base64.b64encode(private[-(9 + remainder) :].encode()).decode()
    assert not find_leaks(fragment, [key], ""), "base64 padding counted as private material"
