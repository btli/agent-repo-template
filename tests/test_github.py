import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request

import pytest
from agent_repo.github import GitHub, GitHubError, _SafeRedirectHandler

TOKEN = "secret-test-token"


def _credential_is_absent(value: str) -> bool:
    return TOKEN not in value


def test_request_sends_required_headers():
    captured: list[Request] = []

    def transport(request: Request, timeout: float):
        captured.append(request)
        return 200, {}, b'{"ok": true}'

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    assert api.request("GET", "/repos/your-org/project") == {"ok": True}
    headers = {key.lower(): value for key, value in captured[0].header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"


def test_relative_request_path_is_scoped_to_repository():
    captured: list[Request] = []

    def transport(request: Request, timeout: float):
        captured.append(request)
        return 200, {}, b"{}"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    api.request("GET", "issues/12")
    api.request("GET", "/issues/12")
    assert captured[0].full_url.endswith("/repos/your-org/project/issues/12")
    assert captured[1].full_url == "https://api.github.com/issues/12"


def test_api_root_path_is_used_without_repository_prefix():
    captured: list[Request] = []

    def transport(request: Request, timeout: float):
        captured.append(request)
        return 200, {}, b"[]"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    api.request("GET", "/search/issues", params={"q": "repo:your-org/project"})
    assert captured[0].full_url == (
        "https://api.github.com/search/issues?q=repo%3Ayour-org%2Fproject"
    )


def test_paginate_follows_next_link_for_three_pages():
    seen: list[str] = []

    def transport(request: Request, timeout: float):
        seen.append(request.full_url)
        responses = {
            "https://api.github.com/repos/your-org/project/issues?state=open&per_page=100": (
                {"Link": '<https://api.github.com/page/2>; rel="next"'},
                [1],
            ),
            "https://api.github.com/page/2": (
                {"Link": '<https://api.github.com/page/3>; rel="next"'},
                [2],
            ),
            "https://api.github.com/page/3": ({}, [3]),
        }
        headers, payload = responses[request.full_url]
        return 200, headers, json.dumps(payload).encode()

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    assert api.paginate("/repos/your-org/project/issues", {"state": "open", "per_page": 100}) == [
        1,
        2,
        3,
    ]
    assert seen == [
        "https://api.github.com/repos/your-org/project/issues?state=open&per_page=100",
        "https://api.github.com/page/2",
        "https://api.github.com/page/3",
    ]


@pytest.mark.parametrize(
    "link",
    [
        '<https://api.github.com/page/2>; foo=bar; rel="next"',
        '<https://api.github.com/page/2>; rel="next last"',
    ],
)
def test_next_link_parses_independent_parameters_and_relation_tokens(link: str):
    assert GitHub._next_link({"Link": link}) == "https://api.github.com/page/2"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (
            '<https://api.github.com/page/2?labels=a,b>; rel="next"',
            "https://api.github.com/page/2?labels=a,b",
        ),
        ('<https://api.github.com/page/2>; REL="NEXT"', "https://api.github.com/page/2"),
        ('<https://api.github.com/page/2>; rel=""', None),
        ('<https://api.github.com/page/2>; title="x; rel=next fake"; rel="last"', None),
        ('<>; rel="next"', None),
    ],
)
def test_next_link_handles_commas_case_and_quoted_parameters(link: str, expected: str | None):
    assert GitHub._next_link({"Link": link}) == expected


def test_paginate_follows_next_url_with_comma_using_url_dependent_responses():
    seen: list[str] = []
    responses = {
        "https://api.github.com/start": (
            {"Link": '<https://api.github.com/page/2?labels=a,b>; rel="next"'},
            [1],
        ),
        "https://api.github.com/page/2?labels=a,b": ({}, [2]),
    }

    def transport(request: Request, timeout: float):
        seen.append(request.full_url)
        headers, payload = responses[request.full_url]
        return 200, headers, json.dumps(payload).encode()

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    assert api.paginate("/start") == [1, 2]
    assert seen == list(responses)


def test_paginate_rejects_repeated_next_url():
    seen: list[str] = []

    def transport(request: Request, timeout: float):
        seen.append(request.full_url)
        if len(seen) > 2:
            raise AssertionError("pagination should stop on the repeated URL")
        return 200, {"Link": '<https://api.github.com/start>; rel="next"'}, b"[1]"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError, match="pagination loop"):
        api.paginate("/start")
    assert seen == ["https://api.github.com/start"]


def test_paginate_reports_unexpected_response_shape():
    def transport(request: Request, timeout: float):
        return 200, {}, b'{"items": "not a list"}'

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.paginate("/start", key="items")
    assert caught.value.message == "unexpected paginated response shape"
    assert str(caught.value) == (
        "GitHub API request failed with status 200: unexpected paginated response shape"
    )


def test_http_error_excludes_token():
    def transport(request: Request, timeout: float):
        return (
            404,
            {
                "X-GitHub-Request-Id": f"request-{TOKEN}",
                "Retry-After": f"{TOKEN}-30",
                "X-RateLimit-Remaining": f"{TOKEN}-0",
            },
            json.dumps({"message": f"{TOKEN} Not Found"}).encode(),
        )

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert caught.value.status == 404
    assert _credential_is_absent(caught.value.message)
    assert _credential_is_absent(caught.value.request_id or "")
    assert _credential_is_absent(caught.value.retry_after or "")
    assert _credential_is_absent(caught.value.ratelimit_remaining or "")
    assert _credential_is_absent(str(caught.value))


def test_http_error_preserves_safe_diagnostics():
    def transport(request: Request, timeout: float):
        return (
            404,
            {
                "X-GitHub-Request-Id": "request-123",
                "Retry-After": "30",
                "X-RateLimit-Remaining": "0",
            },
            b'{"message":"Not Found"}',
        )

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert caught.value.message == "Not Found"
    assert caught.value.request_id == "request-123"
    assert caught.value.retry_after == "30"
    assert caught.value.ratelimit_remaining == "0"


@pytest.mark.parametrize(
    ("status", "expected_message"),
    [(200, "invalid JSON response"), (500, "GitHub API request failed with status 500")],
)
def test_deeply_nested_response_body_raises_github_error(status: int, expected_message: str):
    def transport(request: Request, timeout: float):
        return status, {}, b"[" * 200000

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/deep")
    assert caught.value.status == status
    assert caught.value.message == expected_message


def test_paginate_caps_fresh_next_links():
    requested: list[str] = []

    def transport(request: Request, timeout: float):
        requested.append(request.full_url)
        if len(requested) > 1001:
            raise AssertionError("pagination should stop at the page limit")
        next_page = len(requested) + 1
        headers = {"Link": f'<https://api.github.com/page/{next_page}>; rel="next"'}
        return 200, headers, b"[1]"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError, match="pagination limit") as caught:
        api.paginate("/start")
    assert caught.value.status == 0
    assert len(requested) == 1000


def test_github_error_message_is_capped_and_token_free():
    def transport(request: Request, timeout: float):
        return 403, {}, json.dumps({"message": TOKEN + " invalid " + "x" * 250}).encode()

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert len(caught.value.message) <= 200
    assert _credential_is_absent(caught.value.message)
    assert _credential_is_absent(str(caught.value))


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (403, {"X-RateLimit-Remaining": "0"}, "Resource not accessible by integration"),
        (429, {"Retry-After": "60"}, "API rate limit exceeded"),
    ],
)
def test_permission_and_rate_limit_errors_expose_safe_metadata(
    status: int, headers: dict[str, str], message: str
):
    def transport(request: Request, timeout: float):
        return status, headers, json.dumps({"message": message}).encode()

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert caught.value.status == status
    assert caught.value.message == message
    credential_absent = _credential_is_absent(str(caught.value))
    assert credential_absent, "credential leaked in error text"


def test_transport_exception_excludes_token():
    def transport(request: Request, timeout: float):
        raise RuntimeError(TOKEN)

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert caught.value.status == 0
    assert caught.value.message == "RuntimeError"
    credential_absent = _credential_is_absent(str(caught.value))
    assert credential_absent, "credential leaked in error text"


def test_sanitized_github_error_from_transport_is_preserved():
    def transport(request: Request, timeout: float):
        raise GitHubError(0, "invalid redirect URL", token=TOKEN)

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/issues")
    assert caught.value.status == 0
    assert caught.value.message == "invalid redirect URL"
    assert _credential_is_absent(str(caught.value))


def test_transport_github_error_is_redacted_without_exception_chain():
    def transport(request: Request, timeout: float):
        raise GitHubError(
            403,
            f"message contains {TOKEN}",
            request_id=f"request-{TOKEN}",
            retry_after=f"retry-{TOKEN}",
            ratelimit_remaining=f"remaining-{TOKEN}",
        )

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/issues")
    error = caught.value
    assert error.status == 403
    assert _credential_is_absent(error.message)
    assert _credential_is_absent(error.request_id or "")
    assert _credential_is_absent(error.retry_after or "")
    assert _credential_is_absent(error.ratelimit_remaining or "")
    assert _credential_is_absent(str(error))
    assert _credential_is_absent(repr(error))
    assert all(_credential_is_absent(repr(value)) for value in vars(error).values())
    assert _credential_is_absent("".join(traceback.format_exception(error)))
    assert error.__cause__ is None
    assert error.__context__ is None


def test_transport_github_error_discards_token_bearing_cause():
    def transport(request: Request, timeout: float):
        try:
            raise RuntimeError(f"cause contains {TOKEN}")
        except RuntimeError as cause:
            raise GitHubError(
                401,
                "safe message",
                request_id="safe-id",
                retry_after="7",
                ratelimit_remaining="0",
                token=TOKEN,
            ) from cause

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/issues")
    error = caught.value
    assert error.status == 401
    assert error.message == "safe message"
    assert error.request_id == "safe-id"
    assert error.retry_after == "7"
    assert error.ratelimit_remaining == "0"
    assert _credential_is_absent(str(error))
    assert _credential_is_absent(repr(error))
    assert all(_credential_is_absent(repr(value)) for value in vars(error).values())
    assert _credential_is_absent("".join(traceback.format_exception(error)))
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    ("path", "link"),
    [
        ("https://attacker.invalid/path", False),
        ("http://attacker.invalid/path", False),
        ("https://api.github.com:444/path", False),
        ("//attacker.invalid/path", False),
        ("http://attacker.invalid/page", True),
        ("http://api.github.com/page", True),
    ],
)
def test_cross_origin_request_and_link_urls_are_rejected_before_transport(path: str, link: bool):
    calls: list[str] = []

    def transport(request: Request, timeout: float):
        calls.append(request.full_url)
        headers = {"Link": f'<{path}>; rel="next"'} if link and len(calls) == 1 else {}
        return 200, headers, b"[]"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError):
        if link:
            api.paginate("/start")
        else:
            api.request("GET", path)
    assert calls == ([] if not link else ["https://api.github.com/start"])


def _refusal_is_sanitized(call) -> bool:
    try:
        call()
    except Exception as error:
        import traceback

        return (
            isinstance(error, GitHubError)
            and _credential_is_absent(str(error))
            and _credential_is_absent(repr(error))
            and _credential_is_absent("".join(traceback.format_exception(error)))
            and error.__cause__ is None
        )
    return False


def test_invalid_constructor_api_url_is_sanitized():
    assert _refusal_is_sanitized(
        lambda: GitHub(TOKEN, "your-org/project", api_url=f"https://api.github.com:{TOKEN}")
    )


def test_refused_request_url_with_userinfo_is_sanitized():
    calls: list[str] = []

    def transport(request: Request, timeout: float):
        calls.append(request.full_url)
        return 200, {}, b"{}"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    path = f"https://{TOKEN}@api.github.com\uff0fevil/path"
    refused = _refusal_is_sanitized(lambda: api.request("GET", path))
    assert refused
    assert not calls


def test_invalid_redirect_url_is_sanitized():
    api = GitHub(TOKEN, "your-org/project")
    handler = next(
        handler for handler in api._opener.handlers if isinstance(handler, _SafeRedirectHandler)
    )
    request = Request("https://api.github.com/start", headers={"Authorization": f"Bearer {TOKEN}"})
    target = f"https://{TOKEN}@[::1/secret"
    assert _refusal_is_sanitized(
        lambda: handler.redirect_request(request, None, 302, "Found", {}, target)
    )


def test_redirect_to_another_origin_never_receives_token():
    received: list[bool] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization") is not None)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            return

    destination = HTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever)
    destination_thread.start()
    target_url = f"http://127.0.0.1:{destination.server_port}/received"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    source = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    source_thread = threading.Thread(target=source.serve_forever)
    source_thread.start()
    try:
        api = GitHub(
            TOKEN,
            "your-org/project",
            api_url=f"http://127.0.0.1:{source.server_port}",
        )
        assert api.request("GET", "/start") == {}
    finally:
        source.shutdown()
        source.server_close()
        source_thread.join()
        destination.shutdown()
        destination.server_close()
        destination_thread.join()
    assert received == [False], "credential reached a different origin"


def test_post_redirect_error_preserves_status_and_hides_token():
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/renamed/issues")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        api = GitHub(
            TOKEN,
            "your-org/project",
            api_url=f"http://127.0.0.1:{server.server_port}",
        )
        with pytest.raises(GitHubError) as caught:
            api.request("POST", "issues", body={"title": "example"})
        assert caught.value.status == 307
        assert _credential_is_absent(str(caught.value))
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
    assert received == ["/repos/your-org/project/issues"]


def test_same_origin_redirect_keeps_authorization():
    from agent_repo.github import _SafeRedirectHandler

    received: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/finish")
                self.end_headers()
                return
            received.append(self.headers.get("Authorization") == f"Bearer {TOKEN}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        api = GitHub(TOKEN, "your-org/project", api_url=f"http://127.0.0.1:{server.server_port}")
        assert any(isinstance(handler, _SafeRedirectHandler) for handler in api._opener.handlers)
        assert api.request("GET", "/start") == {}
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
    assert received == [True], "same-origin redirect lost authorization"


def test_https_downgrade_redirect_drops_authorization():
    from agent_repo.github import _SafeRedirectHandler

    original = Request("https://api.github.com/start", headers={"Authorization": f"Bearer {TOKEN}"})
    redirected = _SafeRedirectHandler(("https", "api.github.com", 443)).redirect_request(
        original, None, 302, "Found", {}, "http://api.github.com/next"
    )
    assert redirected is not None
    redirected_headers = {key.lower(): value for key, value in redirected.header_items()}
    assert "authorization" not in redirected_headers, "authorization survived downgrade"


def test_diff_accept_returns_text():
    def transport(request: Request, timeout: float):
        return 200, {}, b"diff --git a/a b/a\n"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    result = api.request(
        "GET", "/repos/your-org/project/pull/1", accept="application/vnd.github.diff"
    )
    assert isinstance(result, str)
    assert result.startswith("diff --git")
