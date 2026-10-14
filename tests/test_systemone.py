from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from agent_repo.systemone import (
    Answer,
    ProviderConfig,
    Question,
    SystemOneError,
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
    cfg = ProviderConfig(provider=provider, model=documented["model"])
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
    assert json.loads(request.data) == documented


@pytest.mark.parametrize("provider", PROVIDERS)
def test_parse_response_uses_documented_answer_shapes(provider: str) -> None:
    documented = _fixture(provider)
    result = parse_response(
        ProviderConfig(provider=provider, model=documented["request"]["model"]),
        json.dumps(documented["response"]).encode(),
        _questions(documented["request"]),
    )

    expected = {
        "jev": Answer(id="is_urgent", value="yes", p=0.95),
        "databricks-openjev": Answer(id="intent", value="duplicate_charge", p=0.965),
        "gateway": Answer(id="urgency", value="high", p=1.0),
    }
    assert result == {expected[provider].id: expected[provider]}


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


def test_choice_not_in_question_options_is_none() -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        b'{"answers":{"route":{"type":"choice","choice":"c","confidence":0.9}}}',
        [Question(id="route", kind="choice", prompt="Choose", options={"a": "A", "b": "B"})],
    )

    assert result == {"route": None}


@pytest.mark.parametrize(
    ("noul", "value", "probability"),
    [(0.9, "yes", 0.9), (0.1, "no", 0.1)],
)
def test_noul_probability_is_the_probability_of_true(
    noul: float, value: str, probability: float
) -> None:
    result = parse_response(
        ProviderConfig(provider="jev", model="jev-latest"),
        json.dumps({"answers": {"is_urgent": {"type": "noul", "noul": noul}}}).encode(),
        [Question(id="is_urgent", kind="noul", prompt="Is it urgent?", options=None)],
    )

    assert result["is_urgent"] == Answer(id="is_urgent", value=value, p=probability)


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


def test_timeout_is_wrapped_without_leaking_the_key() -> None:
    key = "credential-for-test"
    config, questions = _request()

    def timeout(_request: Any, _timeout: float) -> bytes:
        raise TimeoutError(f"timed out for {key}")

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=timeout)

    assert key not in str(exc_info.value)


@pytest.mark.parametrize("status", [429, 503])
def test_http_errors_are_not_retried_or_logged_with_the_key(status: int) -> None:
    key = "credential-for-test"
    config, questions = _request()
    calls = 0

    def fail_once(request: Any, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(request.full_url, status, "unavailable", None, None)

    with pytest.raises(SystemOneError) as exc_info:
        ask(config, _BASE_URLS["jev"], key, "text", questions, transport=fail_once)

    assert calls == 1
    assert key not in str(exc_info.value)


def test_malformed_json_is_wrapped_without_leaking_the_key() -> None:
    key = "credential-for-test"
    config, questions = _request()

    with pytest.raises(SystemOneError) as exc_info:
        ask(
            config,
            _BASE_URLS["jev"],
            key,
            "text",
            questions,
            transport=lambda _request, _timeout: b"{invalid",
        )

    assert key not in str(exc_info.value)
