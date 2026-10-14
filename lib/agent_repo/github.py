import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

Transport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


class GitHubError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub API request failed with status {status}")


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
        self._transport = transport or self._urlopen

    @staticmethod
    def _urlopen(
        request: urllib.request.Request, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
        if path.startswith(("https://", "http://")):
            url = path
        else:
            relative_path = path.lstrip("/")
            if not relative_path.startswith("repos/"):
                relative_path = f"repos/{self._repo}/{relative_path}"
            url = f"{self._api_url}/{relative_path}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            status, response_headers, response_body = self._transport(request, 30.0)
        except Exception:
            raise GitHubError(0) from None
        if status < 200 or status >= 300:
            raise GitHubError(status)
        decoded = response_body.decode("utf-8", errors="replace")
        if accept == "application/vnd.github.diff":
            value: object | str = decoded
        elif not decoded:
            value = None
        else:
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError:
                raise GitHubError(status) from None
        return value, response_headers

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
            match = re.match(r"\s*<([^>]+)>\s*;\s*rel=[\"']next[\"']", part)
            if match:
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
