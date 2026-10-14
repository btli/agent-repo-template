from __future__ import annotations

import base64
import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Question:
    id: str
    kind: Literal["choice", "noul"]
    prompt: str
    options: dict[str, str] | None


@dataclass(frozen=True)
class Answer:
    id: str
    value: str
    p: float


@dataclass(frozen=True)
class ProviderConfig:
    provider: Literal["jev", "databricks-openjev", "gateway"]
    model: str


class SystemOneError(Exception):
    """A safe, credential-free System One client error."""


# TypeSafe's documented endpoint accepts up to 255 choice criteria.
PROVIDERS: dict[str, dict[str, str | int]] = {
    # TypeSafe API reference: https://api.typesafe.ai/redoc
    "jev": {
        "path": "/v1/systemone",
        "auth": "bearer",
        "min_options": 1,
        "max_options": 255,
    },
    # Databricks System One API: https://docs.databricks.com/aws/en/machine-learning/model-serving/query-systemone-models
    "databricks-openjev": {
        "path": "/ai-gateway/typesafe/v1/systemone",
        "auth": "basic-token",
        "min_options": 1,
        "max_options": 255,
    },
    # LiteLLM decisions API: https://docs.litellm.ai/docs/decisions
    # Unverified: apply a two-option floor to gateway routes that translate to OpenAI.
    "gateway": {
        "path": "/v1/systemone",
        "auth": "bearer",
        "min_options": 2,
        "max_options": 255,
    },
}

_CHOICE_SUM_TOLERANCE = 0.05


def build_request(
    cfg: ProviderConfig,
    base_url: str,
    key: str,
    input_text: str,
    questions: list[Question],
) -> urllib.request.Request:
    provider = _provider(cfg)
    if not questions:
        raise SystemOneError("at least one question is required")

    encoded_questions: dict[str, dict[str, object]] = {}
    for question in questions:
        if question.id in encoded_questions:
            raise SystemOneError("question ids must be unique")
        if question.kind not in ("choice", "noul"):
            raise SystemOneError("unsupported question type")

        encoded: dict[str, object] = {
            "type": question.kind,
            "instructions": question.prompt,
        }
        if question.kind == "choice":
            options = question.options
            minimum = int(provider["min_options"])
            maximum = int(provider["max_options"])
            if options is None or not minimum <= len(options) <= maximum:
                raise SystemOneError("choice option count is outside the provider bounds")
            encoded["criteria"] = options
        encoded_questions[question.id] = encoded

    body = {
        "model": cfg.model,
        "state": input_text,
        "questions": encoded_questions,
    }
    headers = {"Content-Type": "application/json"}
    auth = provider["auth"]
    if auth == "basic-token":
        credential = base64.b64encode(f"token:{key}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {credential}"
    else:
        headers["Authorization"] = f"Bearer {key}"

    url = f"{base_url.rstrip('/')}{provider['path']}"
    return urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def parse_response(
    cfg: ProviderConfig,
    body: bytes,
    questions: list[Question],
) -> dict[str, Answer | None]:
    _provider(cfg)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise SystemOneError("provider returned malformed JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
        raise SystemOneError("provider returned a malformed response")

    answers: dict[str, Answer | None] = {}
    for question in questions:
        raw_answer = payload["answers"].get(question.id)
        if not isinstance(raw_answer, dict):
            answers[question.id] = None
        elif question.kind == "noul":
            answers[question.id] = _parse_noul(question, raw_answer)
        else:
            answers[question.id] = _parse_choice(question, raw_answer)
    return answers


def ask(
    cfg: ProviderConfig,
    base_url: str,
    key: str,
    input_text: str,
    questions: list[Question],
    transport: Callable[[urllib.request.Request, float], bytes] | None = None,
    timeout: float = 20.0,
) -> dict[str, Answer | None]:
    request = build_request(cfg, base_url, key, input_text, questions)
    sender = transport or _send
    try:
        response = sender(request, timeout)
    except urllib.error.HTTPError as exc:
        raise SystemOneError(f"provider returned HTTP {exc.code}") from None
    except (TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
            raise SystemOneError("provider request timed out") from None
        raise SystemOneError("provider request failed") from None
    except Exception:
        raise SystemOneError("provider request failed") from None
    return parse_response(cfg, response, questions)


def _provider(cfg: ProviderConfig) -> dict[str, str | int]:
    try:
        return PROVIDERS[cfg.provider]
    except (KeyError, TypeError):
        raise SystemOneError("unsupported provider") from None


def _parse_noul(question: Question, answer: dict[str, object]) -> Answer | None:
    if answer.get("type") != "noul":
        return None
    probability = _unit_probability(answer.get("noul"))
    if probability is None:
        return None
    return Answer(
        id=question.id,
        value="yes" if probability >= 0.5 else "no",
        p=probability,
    )


def _parse_choice(question: Question, answer: dict[str, object]) -> Answer | None:
    options = question.options
    pick = answer.get("choice")
    if answer.get("type") != "choice" or options is None:
        return None
    if not isinstance(pick, str) or pick not in options:
        return None

    probabilities = answer.get("probabilities")
    if "probabilities" in answer:
        if not isinstance(probabilities, dict) or set(probabilities) != set(options):
            return None
        values = [_unit_probability(value) for value in probabilities.values()]
        if any(value is None for value in values):
            return None
        if abs(sum(value for value in values if value is not None) - 1.0) > _CHOICE_SUM_TOLERANCE:
            return None

    # Some decision backends expose calibrated answer confidence separately.
    if "answer_confidence" in answer:
        probability = _unit_probability(answer["answer_confidence"])
    elif "confidence" in answer:
        probability = _unit_probability(answer["confidence"])
    elif isinstance(probabilities, dict):
        probability = _unit_probability(probabilities.get(pick))
    else:
        probability = None
    if probability is None:
        return None
    return Answer(id=question.id, value=pick, p=probability)


def _unit_probability(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return probability


def _send(request: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError from None
        raise
