import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Transport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]
MAX_PAGINATION_PAGES = 1000


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
    def __init__(self, api_origin: tuple[str, str, int], token: str = ""):
        super().__init__()
        self._api_origin = api_origin
        self._token = token

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
        try:
            source_origin = _url_origin(request.full_url)
            target_origin = _url_origin(new_url)
        except ValueError:
            raise GitHubError(0, "invalid redirect URL", token=self._token) from None
        if source_origin != target_origin or target_origin != self._api_origin:
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
        try:
            self._api_url = api_url.rstrip("/")
            self._api_origin = _url_origin(self._api_url)
        except Exception:
            raise GitHubError(0, "invalid API URL", token=token) from None
        self._opener = urllib.request.build_opener(_SafeRedirectHandler(self._api_origin, token))
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
        url = self._request_url(path, params)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
        except Exception:
            raise GitHubError(0, "invalid API URL", token=self._token) from None
        transport_error: GitHubError | None = None
        try:
            status, response_headers, response_body = self._transport(request, 30.0)
        except GitHubError as error:
            transport_error = error
        except Exception as exc:
            raise GitHubError(0, type(exc).__name__, token=self._token) from None
        if transport_error is not None:
            raise GitHubError(
                transport_error.status,
                transport_error.message,
                request_id=transport_error.request_id,
                retry_after=transport_error.retry_after,
                ratelimit_remaining=transport_error.ratelimit_remaining,
                token=self._token,
            ) from None
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
            except (json.JSONDecodeError, RecursionError):
                raise GitHubError(status, "invalid JSON response", token=self._token) from None
        return value, response_headers

    def _request_url(self, path: str, params: dict | None = None) -> str:
        try:
            parsed_path = urllib.parse.urlsplit(path)
            if parsed_path.scheme or parsed_path.netloc:
                url = path
            elif path.startswith("/"):
                url = f"{self._api_url}{path}"
            else:
                url = f"{self._api_url}/repos/{self._repo}/{path}"
            if params:
                query = urllib.parse.urlencode(params, doseq=True)
                url = f"{url}{'&' if '?' in url else '?'}{query}"
            request_origin = _url_origin(url)
        except Exception:
            raise GitHubError(0, "invalid API URL", token=self._token) from None
        if request_origin != self._api_origin:
            raise GitHubError(
                0,
                "refused request to a different origin",
                token=self._token,
            ) from None
        return url

    def _http_error(self, status: int, headers: dict[str, str], body: bytes) -> GitHubError:
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, RecursionError):
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
    def _split_link_values(value: str, separator: str) -> list[str]:
        parts: list[str] = []
        start = 0
        in_angle = False
        in_quote = False
        escaped = False
        for index, character in enumerate(value):
            if in_angle:
                if character == ">":
                    in_angle = False
                continue
            if escaped:
                escaped = False
            elif in_quote and character == "\\":
                escaped = True
            elif character == '"':
                in_quote = not in_quote
            elif not in_quote and character == "<":
                in_angle = True
            elif not in_quote and character == separator:
                parts.append(value[start:index])
                start = index + 1
        parts.append(value[start:])
        return parts

    @staticmethod
    def _next_link(headers: dict[str, str]) -> str | None:
        link = next((value for key, value in headers.items() if key.lower() == "link"), "")
        for part in GitHub._split_link_values(link, ","):
            match = re.match(r"\s*<([^>]+)>(.*)", part)
            if match is None:
                continue
            parameters = GitHub._split_link_values(match.group(2), ";")
            for parameter in parameters:
                name, separator, value = parameter.strip().partition("=")
                if not separator or name.strip().lower() != "rel":
                    continue
                relation = value.strip()
                if len(relation) >= 2 and relation[0] == relation[-1] == '"':
                    relation = relation[1:-1]
                elif len(relation) >= 2 and relation[0] == relation[-1] == "'":
                    relation = relation[1:-1]
                if any(token.lower() == "next" for token in relation.split()):
                    return match.group(1)
        return None

    def paginate(self, path: str, params: dict | None = None, key: str | None = None) -> list:
        results: list = []
        next_path = path
        next_params = params
        visited_urls: set[str] = set()
        page_count = 0
        while next_path is not None:
            if page_count >= MAX_PAGINATION_PAGES:
                raise GitHubError(0, "pagination limit", token=self._token) from None
            page_count += 1
            request_url = self._request_url(next_path, next_params)
            if request_url in visited_urls:
                raise GitHubError(0, "pagination loop", token=self._token) from None
            visited_urls.add(request_url)
            value, headers = self._send("GET", next_path, params=next_params)
            next_path = self._next_link(headers)
            next_params = None
            if key is not None:
                if not isinstance(value, dict) or not isinstance(value.get(key), list):
                    raise GitHubError(200, "unexpected paginated response shape")
                results.extend(value[key])
            elif isinstance(value, list):
                results.extend(value)
            else:
                raise GitHubError(200, "unexpected paginated response shape")
        return results
