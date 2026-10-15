from __future__ import annotations

import base64
import io
import json
import threading
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from agent_repo.systemone import (
    _MAX_RESPONSE_BYTES,
    Answer,
    ProviderConfig,
    Question,
    SystemOneError,
    _send,
    ask,
    build_request,
    parse_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "systemone"
PROVIDERS = ("jev", "databricks-openjev", "gateway")
_BASE_URLS = {
    "jev": "https://api.typesafe.ai",
    "databricks-openjev": "https://workspace.example",
    "gateway": "https://gateway.example",
}


def _fixture(provider: str) -> dict[str, Any]:
    return json.loads((FIXTURES / provider / "systemone.json").read_text())


def _questions(request_body: dict[str, Any]) -> list[Question]:
    return [
        Question(
            id=question_id,
            kind=question["type"],
            prompt=question["instructions"],
            options=question.get("criteria"),
        )
        for question_id, question in request_body["questions"].items()
    ]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_build_request_uses_documented_provider_details(provider: str) -> None:
    documented = _fixture(provider)["request"]
    model = "system.ai.other-model" if provider == "databricks-openjev" else documented["model"]
    cfg = ProviderConfig(provider=provider, model=model)
    request = build_request(
        cfg,
        _BASE_URLS[provider],
        "test-key",
        documented["state"],
        _questions(documented),
    )

    assert request.method == "POST"
    assert (
        urlsplit(request.full_url).path
        == {
            "jev": "/v1/systemone",
            "databricks-openjev": "/ai-gateway/typesafe/v1/systemone",
            "gateway": "/v1/systemone",
        }[provider]
    )
    headers = {name.lower(): value for name, value in request.header_items()}
    if provider == "databricks-openjev":
        token = base64.b64encode(b"token:test-key").decode("ascii")
        assert headers["authorization"] == f"Basic {token}"
        assert documented["model"] == "system.ai.openjev-qwen35-4b"
    else:
        assert headers["authorization"] == "Bearer test-key"
    assert headers["content-type"] == "application/json"
    expected = {**documented, "model": model}
    assert json.loads(request.data) == expected
    assert json.loads(request.data)["model"] == cfg.model


@pytest.mark.parametrize("provider", PROVIDERS)
def test_parse_response_uses_documented_answer_shapes(provider: str) -> None:
    documented = _fixture(provider)
    result = parse_response(
        ProviderConfig(provider=provider, model=documented["request"]["model"]),
        json.dumps(documented["response"]).encode(),
        _questions(documented["request"]),
    )

    expected = {
        "jev": Answer(id="billing", value="yes", p=0.98),
        "databricks-openjev": Answer(id="intent", value="duplicate_charge", p=0.965),
        "gateway": Answer(id="urgency", value="high", p=1.0),
    }
    assert result == {expected[provider].id: expected[provider]}
    if provider == "jev":
        question = _questions(documented["request"])
        no_answer = parse_response(
            ProviderConfig(provider="jev", model="jev-latest"),
            b'{"answers":{"billing":{"type":"noul","noul":0.1}}}',
            question,
        )
        assert no_answer == {"billing": Answer(id="billing", value="no", p=0.9)}


@pytest.mark.parametrize(
    ("answer_fields", "expected_p"),
    [
        (
            {
                "answer_confidence": 0.42,
                "confidence": 0.91,
                "probabilities": {"a": 0.8, "b": 0.2},
            },
            0.42,
        ),
        ({"confidence": 0.7, "probabilities": {"a": 0.8, "b": 0.2}}, 0.7),
        ({"probabilities": {"a": 0.88, "b": 0.12}}, 0.88),
    ],
)
def test_choice_probability_uses_the_first_available_source(
    answer_fields: dict[str, Any], expected_p: float
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps(
            {"answers": {"route": {"type": "choice", "choice": "a", **answer_fields}}}
        ).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result["route"] == Answer(id="route", value="a", p=expected_p)


@pytest.mark.parametrize(
    "answer_fields",
    [
        {"confidence": 0.9, "probabilities": {"a": 0.8, "b": 0.1}},
        {"confidence": 0.9, "probabilities": {"a": 0.8, "b": 0.3}},
    ],
)
def test_choice_with_invalid_probability_distribution_is_none(
    answer_fields: dict[str, Any],
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps(
            {"answers": {"route": {"type": "choice", "choice": "a", **answer_fields}}}
        ).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


@pytest.mark.parametrize(
    ("distribution", "expected"),
    [
        ({"a": 0.7, "b": 0.25}, Answer(id="route", value="a", p=0.8)),
        ({"a": 0.8, "b": 0.25}, Answer(id="route", value="a", p=0.8)),
        ({"a": 0.7, "b": 0.249999}, None),
        ({"a": 0.8, "b": 0.250001}, None),
    ],
)
def test_choice_probability_sum_uses_inclusive_robust_bounds(
    distribution: dict[str, float], expected: Answer | None
) -> None:
    answer = {"type": "choice", "choice": "a", "confidence": 0.8}
    answer["probabilities"] = distribution
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps({"answers": {"route": answer}}).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": expected}


@pytest.mark.parametrize(
    "confidence_fields",
    [
        {"answer_confidence": 0.99},
        {"confidence": 0.99},
        {},
    ],
)
def test_choice_pick_must_be_a_probability_maximum(
    confidence_fields: dict[str, float],
) -> None:
    answer = {
        "type": "choice",
        "choice": "a",
        "probabilities": {"a": 0.1, "b": 0.9},
        **confidence_fields,
    }
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps({"answers": {"route": answer}}).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


@pytest.mark.parametrize(
    "distribution",
    [
        {"a": 0.5, "b": 0.5},
        {"a": 0.4999999999999, "b": 0.5000000000001},
    ],
)
def test_choice_pick_accepts_tied_probability_maxima(
    distribution: dict[str, float],
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps(
            {
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": distribution,
                    }
                }
            }
        ).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result["route"] == Answer(id="route", value="a", p=distribution["a"])


def test_choice_pick_outside_tie_tolerance_is_none() -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        b'{"answers":{"route":{"type":"choice","choice":"a","probabilities":{"a":0.5,"b":0.500000001}}}}',
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


@pytest.mark.parametrize(
    "distribution",
    [
        {"a": 1.0},
        {"a": 0.5, "b": 0.5, "c": 0.0},
    ],
)
def test_choice_probability_keys_must_match_options(
    distribution: dict[str, float],
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps(
            {
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": distribution,
                    }
                }
            }
        ).encode(),
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


def test_choice_not_in_question_options_is_none() -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        b'{"answers":{"route":{"type":"choice","choice":"c","confidence":0.9}}}',
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


@pytest.mark.parametrize(
    ("noul", "value", "probability"),
    [(0.9, "yes", 0.9), (0.1, "no", 0.9)],
)
def test_noul_probability_is_the_probability_of_the_chosen_value(
    noul: float, value: str, probability: float
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps({"answers": {"is_urgent": {"type": "noul", "noul": noul}}}).encode(),
        [Question(id="is_urgent", kind="noul", prompt="Is it urgent?", options=None)],
    )

    assert result["is_urgent"] == Answer(id="is_urgent", value=value, p=probability)


def test_noul_midpoint_selects_yes() -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        b'{"answers":{"check":{"type":"noul","noul":0.5}}}',
        [Question(id="check", kind="noul", prompt="Is it true?", options=None)],
    )

    assert result["check"] == Answer(id="check", value="yes", p=0.5)


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            Question(id="check", kind="noul", prompt="Check", options=None),
            b'{"type":"noul","noul":' + b"9" * 500 + b"}",
        ),
        (
            Question(
                id="check",
                kind="choice",
                prompt="Check",
                options={"a": "A", "b": "B"},
            ),
            b'{"type":"choice","choice":"a","confidence":'
            + b"9" * 500
            + b',"probabilities":{"a":0.8,"b":0.2}}',
        ),
    ],
)
def test_ask_returns_none_for_unrepresentable_large_probabilities(
    question: Question, answer: bytes
) -> None:
    config = ProviderConfig(provider="jev", model="jev-latest")
    body = b'{"answers":{"check":' + answer + b"}}"

    result = ask(
        config,
        _BASE_URLS["jev"],
        "test-key",
        "text",
        [question],
        transport=lambda _request, _timeout: body,
    )

    assert result == {"check": None}


def test_ask_wraps_integer_digit_limit_as_malformed_json() -> None:
    config, questions = _request()
    body = (
        b'{"credential":"credential-for-test","answers":{"check":{"type":"noul","noul":'
        + b"9" * 5000
        + b"}}}"
    )
    calls = []

    def malformed(request: Any, timeout: float) -> bytes:
        calls.append((request, timeout))
        return body

    with pytest.raises(SystemOneError, match="malformed JSON") as exc_info:
        ask(
            config,
            _BASE_URLS["jev"],
            "credential-for-test",
            "text",
            questions,
            transport=malformed,
            timeout=3.5,
        )

    assert len(calls) == 1
    assert calls[0][1] == 3.5
    assert "credential-for-test" not in str(exc_info.value)


def test_ask_wraps_decoder_recursion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    config, questions = _request()
    calls: list[tuple[Any, float]] = []

    def decode(_body: bytes) -> object:
        raise RecursionError("decoder recursion")

    monkeypatch.setattr("agent_repo.systemone.json.loads", decode)

    with pytest.raises(SystemOneError, match="malformed JSON"):
        parse_response(config, b"{}", questions)

    with pytest.raises(SystemOneError, match="malformed JSON") as exc_info:
        ask(
            config,
            _BASE_URLS["jev"],
            "decoder-test-key",
            "text",
            questions,
            transport=lambda request, timeout: calls.append((request, timeout)) or b"{}",
        )

    assert len(calls) == 1
    assert str(exc_info.value) == "provider returned malformed JSON"


def test_gateway_rejects_a_choice_with_one_option() -> None:
    key = "test-key"
    with pytest.raises(SystemOneError) as exc_info:
        build_request(
            ProviderConfig(provider="gateway", model="jev"),
            _BASE_URLS["gateway"],
            key,
            "text",
            [Question(id="route", kind="choice", prompt="Choose", options={"a": "A"})],
        )
    assert key not in str(exc_info.value)


def _request() -> tuple[ProviderConfig, list[Question]]:
    return (
        ProviderConfig(provider="jev", model="jev-latest"),
        [Question(id="check", kind="noul", prompt="Is it true?", options=None)],
    )


def _assert_canary_absent(error: SystemOneError, canary: str) -> None:
    values = [str(error), repr(error.args), repr(vars(error))]
    values.extend(repr(getattr(error, name)) for name in dir(error))
    assert all(canary not in value for value in values)


def test_timeout_is_wrapped_without_leaking_the_key() -> None:
    key = "credential-for-test"
    config, questions = _request()
    calls = []

    def timeout(request: Any, timeout_value: float) -> bytes:
        calls.append((request, timeout_value))
        raise TimeoutError(f"timed out for {key}")

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=timeout, timeout=7.25)

    assert len(calls) == 1
    assert calls[0][1] == 7.25
    assert calls[0][0].full_url.endswith("/v1/systemone")
    assert str(exc_info.value) == "provider request timed out"
    assert key not in str(exc_info.value)


@pytest.mark.parametrize("status", [429, 503])
def test_http_errors_are_not_retried_or_logged_with_the_key(status: int) -> None:
    key = "credential-for-test"
    config, questions = _request()
    calls = 0

    def fail_once(request: Any, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            f"unavailable for {key}",
            {"X-Debug": key},
            io.BytesIO(key.encode()),
        )

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=fail_once)

    assert calls == 1
    assert key not in str(exc_info.value)
    assert exc_info.value.status == status


@pytest.mark.parametrize(
    ("failure", "expected_message"),
    [
        ("urlerror", "provider request failed"),
        ("generic", "provider request failed"),
    ],
)
def test_other_transport_errors_are_wrapped_without_leaking_key(
    failure: str, expected_message: str
) -> None:
    key = "credential-for-test"
    config, questions = _request()
    calls = []

    def fail(request: Any, timeout: float) -> bytes:
        calls.append((request, timeout))
        if failure == "urlerror":
            raise urllib.error.URLError(f"connection failed for {key}")
        raise RuntimeError(f"transport failed for {key}")

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=fail, timeout=4.0)

    assert len(calls) == 1
    assert calls[0][1] == 4.0
    assert str(exc_info.value) == expected_message
    assert key not in str(exc_info.value)


@pytest.mark.parametrize("error_kind", ["systemone", "generic"])
def test_transport_exception_text_and_chain_are_not_retained(error_kind: str) -> None:
    key = "transport-canary"
    url = "https://private.example/path"
    error_text = f"transport failed for {key} at {url}"
    cause = RuntimeError(error_text)
    config, questions = _request()

    def fail(_request: Any, _timeout: float) -> bytes:
        if error_kind == "systemone":
            raise SystemOneError(error_text, status=503) from cause
        error = Exception(error_text)
        error.status = 503
        raise error from cause

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=fail)

    error = exc_info.value
    details = [str(error), "".join(traceback.format_exception(error))]
    details.extend(repr(getattr(error, name)) for name in dir(error))
    assert all(value not in detail for value in (key, url) for detail in details)
    assert error.status == 503
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(error) == "provider request failed"


def test_urlerror_timeout_reason_uses_timeout_message() -> None:
    config, questions = _request()
    key = "credential-for-test"
    calls = []

    def timeout(request: Any, timeout_value: float) -> bytes:
        calls.append((request, timeout_value))
        raise urllib.error.URLError(TimeoutError(f"timed out for {key}"))

    with pytest.raises(SystemOneError, match="provider request timed out") as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=timeout)

    assert len(calls) == 1
    assert key not in str(exc_info.value)


def test_successful_ask_uses_transport_and_normalizes_response() -> None:
    config, questions = _request()
    calls = []

    def respond(request: Any, timeout_value: float) -> bytes:
        calls.append((request, timeout_value))
        return b'{"answers":{"check":{"type":"noul","noul":0.9}}}'

    result = ask(
        config,
        _BASE_URLS["jev"],
        "key",
        "source text",
        questions,
        transport=respond,
        timeout=6.0,
    )

    assert result == {"check": Answer(id="check", value="yes", p=0.9)}
    assert len(calls) == 1
    assert calls[0][1] == 6.0
    assert json.loads(calls[0][0].data)["state"] == "source text"


def test_malformed_json_is_wrapped_without_leaking_the_key() -> None:
    key = "credential-for-test"
    config, questions = _request()
    calls = []

    def malformed(request: Any, timeout: float) -> bytes:
        calls.append((request, timeout))
        return f'{{"credential":"{key}",invalid'.encode()

    with pytest.raises(SystemOneError, match="malformed JSON") as exc_info:
        ask(
            config,
            _BASE_URLS["jev"],
            key,
            "text",
            questions,
            transport=malformed,
            timeout=2.5,
        )

    assert len(calls) == 1
    assert calls[0][1] == 2.5
    assert key not in str(exc_info.value)


def test_redirect_is_rejected_without_forwarding_authorization() -> None:
    key = "redirect-secret"
    received_authorization: list[str | None] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"answers":{"check":{"type":"noul","noul":0.99}}}')

        do_GET = do_POST

        def log_message(self, _format: str, *_args: object) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    target_url = f"http://127.0.0.1:{target.server_port}/capture"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302, f"Redirect canary {key}")
            self.send_header("Location", f"{target_url}?{key}")
            self.send_header("X-Reason", key)
            self.send_header("Content-Length", str(len(key)))
            self.end_headers()
            self.wfile.write(key.encode())

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    try:
        config, questions = _request()
        with pytest.raises(SystemOneError) as exc_info:
            ask(config, f"http://127.0.0.1:{source.server_port}", key, "text", questions)
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        source_thread.join(timeout=2)
        target_thread.join(timeout=2)

    assert exc_info.value.status == 302
    assert received_authorization == []
    _assert_canary_absent(exc_info.value, key)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/url-canary",
        "ftp://localhost/url-canary",
        "https:///url-canary?url-canary",
        "http://localhost.evil.example/url-canary",
        "http://127.0.0.1.nip.io/url-canary",
        "http://localhost:0",
        "https://h?",
        "http://url-canary@localhost/path",
        "http://url-canary:password@localhost/path",
        "https://provider.example/?url-canary",
        "https://provider.example/#url-canary",
    ],
)
def test_nonsecure_nonloopback_base_urls_are_rejected_before_transport(
    base_url: str,
) -> None:
    config, questions = _request()
    calls = []

    def transport(request: Any, timeout: float) -> bytes:
        calls.append((request, timeout))
        return b'{"answers":{"check":{"type":"noul","noul":0.9}}}'

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, base_url, "url-canary", "text", questions, transport=transport)

    assert calls == []
    _assert_canary_absent(exc_info.value, "url-canary")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "HTTPS://provider.example",
    ],
)
def test_base_url_allows_https_and_exact_loopback_hosts(base_url: str) -> None:
    config, questions = _request()

    request = build_request(config, base_url, "key", "text", questions)

    assert urlsplit(request.full_url).path == "/v1/systemone"


def test_loopback_http_request_does_not_use_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    proxy_requests: list[str] = []
    provider_requests: list[str] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            provider_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"answers":{"check":{"type":"noul","noul":0.9}}}')

        def log_message(self, _format: str, *_args: object) -> None:
            return

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            proxy_requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"answers":{"check":{"type":"noul","noul":0.9}}}')

        def log_message(self, _format: str, *_args: object) -> None:
            return

    provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    provider_thread.start()
    proxy_thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    try:
        config, questions = _request()
        result = ask(
            config,
            f"http://127.0.0.1:{provider.server_port}",
            "proxy-canary",
            "text",
            questions,
        )
    finally:
        provider.shutdown()
        proxy.shutdown()
        provider.server_close()
        proxy.server_close()
        provider_thread.join(timeout=2)
        proxy_thread.join(timeout=2)

    assert result == {"check": Answer(id="check", value="yes", p=0.9)}
    assert proxy_requests == []


def test_oversized_provider_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = b"x" * (1024 * 1024 + 32)

    class LargeResponseHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(oversized)))
            self.end_headers()
            self.wfile.write(oversized)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), LargeResponseHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        config, questions = _request()
        with pytest.raises(SystemOneError, match="response too large"):
            ask(config, f"http://127.0.0.1:{server.server_port}", "key", "text", questions)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_send_bounds_response_read_and_rejects_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    read_sizes: list[int] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * (size + 1 if size >= 0 else _MAX_RESPONSE_BYTES + 1)

    class Opener:
        def open(self, _request: Any, timeout: float) -> Response:
            assert timeout == 1.0
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: Opener())
    config, questions = _request()
    request = build_request(config, _BASE_URLS["jev"], "test-key", "text", questions)

    with pytest.raises(SystemOneError, match="provider response too large") as exc_info:
        _send(request, timeout=1.0)

    assert str(exc_info.value) == "provider response too large"
    assert read_sizes == [_MAX_RESPONSE_BYTES + 1]
