from __future__ import annotations

import base64
import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal
from urllib.parse import urlsplit


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

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _FailureReason(Enum):
    HTTP = auto()
    REQUEST_FAILED = auto()
    REQUEST_TIMED_OUT = auto()
    RESPONSE_TOO_LARGE = auto()


_FAILURE_MESSAGES = {
    _FailureReason.HTTP: "provider request failed",
    _FailureReason.REQUEST_FAILED: "provider request failed",
    _FailureReason.REQUEST_TIMED_OUT: "provider request timed out",
    _FailureReason.RESPONSE_TOO_LARGE: "provider response too large",
}


class _TrustedSystemOneError(SystemOneError):
    """An error message created by this module rather than a transport."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        reason: _FailureReason | None = None,
    ) -> None:
        super().__init__(message, status=status)
        self.reason = reason


# Unverified: TypeSafe's schema sets no maximum for choice criteria.
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
_CHOICE_PICK_TOLERANCE = 1e-12
_MAX_RESPONSE_BYTES = 1024 * 1024


def build_request(
    cfg: ProviderConfig,
    base_url: str,
    key: str,
    input_text: str,
    questions: list[Question],
) -> urllib.request.Request:
    provider = _provider(cfg)
    _validate_base_url(base_url)
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
    except Exception:
        malformed_json = True
    else:
        malformed_json = False
    if malformed_json:
        raise SystemOneError("provider returned malformed JSON")
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
    failure_reason: _FailureReason | None = None
    failure_status: int | None = None
    try:
        response = sender(request, timeout)
    except urllib.error.HTTPError as exc:
        if type(exc.code) is int:
            failure_reason = _FailureReason.HTTP
            failure_status = exc.code
        else:
            failure_reason = _FailureReason.REQUEST_FAILED
    except (TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(exc.reason, TimeoutError):
            failure_reason = _FailureReason.REQUEST_TIMED_OUT
        else:
            failure_reason = _FailureReason.REQUEST_FAILED
    except _TrustedSystemOneError as exc:
        reason = exc.__dict__.get("reason")
        if type(reason) is _FailureReason:
            failure_reason = reason
        else:
            failure_reason = _FailureReason.REQUEST_FAILED
        if type(exc.status) is int:
            failure_status = exc.status
    except Exception as exc:
        failure_reason = _FailureReason.REQUEST_FAILED
        status = exc.__dict__.get("status")
        if type(status) is int:
            failure_status = status
    if failure_reason is not None:
        raise SystemOneError(_FAILURE_MESSAGES[failure_reason], status=failure_status) from None
    return parse_response(cfg, response, questions)


def _provider(cfg: ProviderConfig) -> dict[str, str | int]:
    try:
        return PROVIDERS[cfg.provider]
    except (KeyError, TypeError):
        raise SystemOneError("unsupported provider") from None


def _validate_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
        has_credentials = parsed.username is not None or parsed.password is not None
        has_query_or_fragment = (
            bool(parsed.query or parsed.fragment) or "?" in base_url or "#" in base_url
        )
        valid_port = port is None or 0 < port < 65536
        valid = (
            not has_credentials
            and not has_query_or_fragment
            and valid_port
            and (
                (parsed.scheme == "https" and bool(hostname))
                or (
                    parsed.scheme == "http"
                    and hostname is not None
                    and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
                )
            )
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SystemOneError("base URL must use HTTPS or loopback HTTP")


def _parse_noul(question: Question, answer: dict[str, object]) -> Answer | None:
    if answer.get("type") != "noul":
        return None
    probability = _unit_probability(answer.get("noul"))
    if probability is None:
        return None
    value = "yes" if probability >= 0.5 else "no"
    return Answer(
        id=question.id,
        value=value,
        p=probability if value == "yes" else 1.0 - probability,
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
        valid_values = [value for value in values if value is not None]
        total = math.fsum(valid_values)
        if not 1.0 - _CHOICE_SUM_TOLERANCE <= total <= 1.0 + _CHOICE_SUM_TOLERANCE:
            return None
        picked_probability = _unit_probability(probabilities[pick])
        if picked_probability is None or picked_probability + _CHOICE_PICK_TOLERANCE < max(
            valid_values
        ):
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
    if not 0 <= value <= 1:
        return None
    probability = float(value)
    return probability


def _send(request: urllib.request.Request, timeout: float) -> bytes:
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: object, **_kwargs: object) -> None:
            return None

    handlers: list[urllib.request.BaseHandler] = [NoRedirectHandler()]
    if request.type == "http":
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=timeout) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise _TrustedSystemOneError(
            "provider response too large", reason=_FailureReason.RESPONSE_TOO_LARGE
        )
    return body
