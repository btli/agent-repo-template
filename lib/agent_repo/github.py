import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


class GitHubError(Exception):
    def __init__(
        self,
        status: int,
        message: str | None = None,
        *,
        request_id: str | None = None,
        retry_after: str | None = None,
        ratelimit_remaining: str | None = None,
        token: str = "",
    ):
        self.status = status
        self.message = _redact(message or f"GitHub API request failed with status {status}", token)[
            :200
        ]
        self.request_id = _redact(request_id, token) if request_id else None
        self.retry_after = _redact(retry_after, token) if retry_after else None
        self.ratelimit_remaining = (
            _redact(ratelimit_remaining, token) if ratelimit_remaining else None
        )
        super().__init__(f"GitHub API request failed with status {status}: {self.message}")


def _redact(value: str, token: str) -> str:
    return value.replace(token, "[redacted]") if token else value


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme, 0)
    return scheme, host, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, api_origin: tuple[str, str, int]):
        super().__init__()
        self._api_origin = api_origin

    def redirect_request(
        self,
        request: urllib.request.Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is None:
            return None
        source_origin = _url_origin(request.full_url)
        target_origin = _url_origin(new_url)
        if (
            source_origin != target_origin
            or target_origin != self._api_origin
            or (source_origin[0] == "https" and target_origin[0] == "http")
        ):
            for header_set in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(header_set):
                    if key.lower() == "authorization":
                        del header_set[key]
        return redirected


class GitHub:
    def __init__(
        self,
        token: str,
        repo: str,
        api_url: str = "https://api.github.com",
        transport: Transport | None = None,
    ):
        self._token = token
        self._repo = repo
        self._api_url = api_url.rstrip("/")
        self._api_origin = _url_origin(self._api_url)
        self._opener = urllib.request.build_opener(_SafeRedirectHandler(self._api_origin))
        self._transport = transport or self._urlopen

    def _urlopen(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: object | None = None,
        accept: str | None = None,
    ) -> tuple[object | str, dict[str, str]]:
        parsed_path = urllib.parse.urlsplit(path)
        if parsed_path.scheme or parsed_path.netloc:
            url = path
        elif path.startswith("/"):
            url = f"{self._api_url}{path}"
        else:
            url = f"{self._api_url}/repos/{self._repo}/{path}"
        try:
            request_origin = _url_origin(url)
        except ValueError:
            raise GitHubError(
                0, "refused request to an invalid origin", token=self._token
            ) from None
        if request_origin != self._api_origin:
            raise GitHubError(
                0,
                "refused request to a different origin",
                token=self._token,
            )
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            status, response_headers, response_body = self._transport(request, 30.0)
        except Exception as exc:
            raise GitHubError(0, type(exc).__name__, token=self._token) from None
        if status < 200 or status >= 300:
            raise self._http_error(status, response_headers, response_body)
        decoded = response_body.decode("utf-8", errors="replace")
        if accept == "application/vnd.github.diff":
            value: object | str = decoded
        elif not decoded:
            value = None
        else:
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError:
                raise GitHubError(status, "invalid JSON response", token=self._token) from None
        return value, response_headers

    def _http_error(self, status: int, headers: dict[str, str], body: bytes) -> GitHubError:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        return GitHubError(
            status,
            message
            if isinstance(message, str)
            else f"GitHub API request failed with status {status}",
            request_id=normalized_headers.get("x-github-request-id"),
            retry_after=normalized_headers.get("retry-after"),
            ratelimit_remaining=normalized_headers.get("x-ratelimit-remaining"),
            token=self._token,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: object | None = None,
        accept: str | None = None,
    ) -> object | str:
        value, _ = self._send(method, path, params=params, body=body, accept=accept)
        return value

    @staticmethod
    def _next_link(headers: dict[str, str]) -> str | None:
        link = next((value for key, value in headers.items() if key.lower() == "link"), "")
        for part in link.split(","):
            match = re.match(r"\s*<([^>]+)>(.*)", part)
            if match is None:
                continue
            relation = re.search(
                r"(?:^|;)\s*rel\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^;\s]+))",
                match.group(2),
                re.IGNORECASE,
            )
            if relation and "next" in next(group for group in relation.groups() if group).split():
                return match.group(1)
        return None

    def paginate(self, path: str, params: dict | None = None, key: str | None = None) -> list:
        results: list = []
        next_path = path
        next_params = params
        while next_path is not None:
            value, headers = self._send("GET", next_path, params=next_params)
            next_path = self._next_link(headers)
            next_params = None
            if key is not None:
                if not isinstance(value, dict) or not isinstance(value.get(key), list):
                    raise GitHubError(200)
                results.extend(value[key])
            elif isinstance(value, list):
                results.extend(value)
            else:
                raise GitHubError(200)
        return results
