import json
from urllib.request import Request

import pytest
from agent_repo.github import GitHub, GitHubError

TOKEN = "secret-test-token"


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
    api.request("GET", "/issues/12")
    assert captured[0].full_url.endswith("/repos/your-org/project/issues/12")


def test_paginate_follows_next_link_for_three_pages():
    seen: list[str] = []

    def transport(request: Request, timeout: float):
        seen.append(request.full_url)
        page = len(seen)
        headers = {"Link": '<https://api.github.com/page/2>; rel="next"'} if page == 1 else {}
        if page == 2:
            headers = {"Link": '<https://api.github.com/page/3>; rel="next"'}
        return 200, headers, json.dumps([page]).encode()

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    assert api.paginate("/repos/your-org/project/issues") == [1, 2, 3]
    assert len(seen) == 3


def test_http_error_excludes_token():
    def transport(request: Request, timeout: float):
        return 404, {}, b'{"message":"Not Found"}'

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert caught.value.status == 404
    assert TOKEN not in str(caught.value)


def test_transport_exception_excludes_token():
    def transport(request: Request, timeout: float):
        raise RuntimeError(TOKEN)

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    with pytest.raises(GitHubError) as caught:
        api.request("GET", "/missing")
    assert TOKEN not in str(caught.value)


def test_diff_accept_returns_text():
    def transport(request: Request, timeout: float):
        return 200, {}, b"diff --git a/a b/a\n"

    api = GitHub(TOKEN, "your-org/project", transport=transport)
    result = api.request(
        "GET", "/repos/your-org/project/pull/1", accept="application/vnd.github.diff"
    )
    assert isinstance(result, str)
    assert result.startswith("diff --git")
