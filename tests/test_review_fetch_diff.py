import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest
from agent_repo.commands.fetch_diff import _pull, fetch
from agent_repo.event import load_event
from agent_repo.github import GitHub


class API:
    def __init__(
        self,
        *,
        base_ref="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        changed_files=1,
        files=None,
    ):
        self.calls = []
        self.queries = []
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.changed_files = changed_files
        self.files = (
            files
            if files is not None
            else [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "changes": 2,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]
        )

    def __call__(self, request, timeout):
        path = urlparse(request.full_url).path
        accept = request.get_header("Accept")
        self.calls.append((request.method, path, accept))
        self.queries.append(urlparse(request.full_url).query)
        if path.endswith("/pulls/24/files"):
            return 200, {}, json.dumps(self.files).encode()
        return (
            200,
            {},
            json.dumps(
                {
                    "head": {"sha": self.head_sha},
                    "base": {"ref": self.base_ref, "sha": self.base_sha},
                    "changed_files": self.changed_files,
                }
            ).encode(),
        )


def _event_file(
    tmp_path, name="pull_request_target", *, head="b" * 40, base="a" * 40, base_ref="main"
):
    source = (
        "tests/fixtures/events/issue_comment.created.pr.json"
        if name == "issue_comment"
        else "tests/fixtures/events/pull_request_target.opened.json"
    )
    payload = json.loads(Path(source).read_text())
    payload["repository"]["full_name"] = "your-org/project"
    payload["repository"]["default_branch"] = "main"
    if name == "pull_request_target":
        payload["pull_request"]["base"].update({"ref": base_ref, "sha": base})
        payload["pull_request"]["head"]["sha"] = head
    event_path = tmp_path / f"{name}-event.json"
    event_path.write_text(json.dumps(payload))
    return event_path


def _fetch_env(monkeypatch, event_path, output, *, name, base="a" * 40, expected_head="b" * 40):
    monkeypatch.setenv("GITHUB_EVENT_NAME", name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_SHA", base)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("EXPECTED_HEAD_SHA", expected_head)


def _fetch_setup(tmp_path, monkeypatch, api, *, name="pull_request_target"):
    event_path = _event_file(tmp_path, name)
    output = tmp_path / "github-output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    (config_dir / "areas.yml").write_text("areas: []\n")
    _fetch_env(monkeypatch, event_path, output, name=name)
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=api),
    )
    return config_dir, output


def test_fetch_files_uses_paginated_api_without_checkout_or_raw_diff(tmp_path, monkeypatch):
    api = API()
    config_dir, output = _fetch_setup(tmp_path, monkeypatch, api)
    files_path = tmp_path / "files.json"

    base_sha, head_sha, limited, reason = fetch(files_path, config_dir)

    assert base_sha == "a" * 40
    assert head_sha == "b" * 40
    assert limited is False
    assert reason == ""
    assert json.loads(files_path.read_text())[0]["filename"] == "src/a.py"
    assert any(call[1] == "/repos/your-org/project/pulls/24/files" for call in api.calls)
    assert "per_page=100" in api.queries
    assert not any(call[2] == "application/vnd.github.diff" for call in api.calls)
    assert not any(call[0] == "checkout" for call in api.calls)
    assert "summary_only=false" in output.read_text()


@pytest.mark.parametrize(
    "entry",
    [
        {
            "filename": "src/a.py",
            "status": "unknown",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
            "patch": "@@ -0,0 +1 @@\n+added",
        },
        {
            "filename": "src/a.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
        },
    ],
    ids=["unknown-status", "renamed-without-previous-filename"],
)
def test_fetch_routes_invalid_file_entries_to_summary_only(tmp_path, monkeypatch, entry):
    api = API(files=[entry])
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)

    _, _, limited, reason = fetch(tmp_path / "files.json", config_dir)

    assert limited is True
    assert "needs-maintainer" in reason


@pytest.mark.parametrize(
    "changed_files",
    [2],
    ids=["incomplete-list"],
)
def test_fetch_routes_incomplete_file_list_to_summary_only(tmp_path, monkeypatch, changed_files):
    api = API(changed_files=changed_files)
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)

    _, _, limited, reason = fetch(tmp_path / "files.json", config_dir)

    assert limited is True
    assert "needs-maintainer" in reason


def test_fetch_routes_missing_patch_for_changed_file_to_summary_only(tmp_path, monkeypatch):
    entry = {
        "filename": "src/a.py",
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "changes": 1,
    }
    api = API(files=[entry])
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)

    _, _, limited, reason = fetch(tmp_path / "files.json", config_dir)

    assert limited is True
    assert "needs-maintainer" in reason


def test_fetch_rejects_head_that_moved_during_files_request(tmp_path, monkeypatch):
    class MovingAPI(API):
        def __init__(self):
            super().__init__()

        def __call__(self, request, timeout):
            response = super().__call__(request, timeout)
            if urlparse(request.full_url).path.endswith("/files"):
                self.head_sha = "c" * 40
            return response

    api = MovingAPI()
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api, name="issue_comment")
    files_path = tmp_path / "files.json"

    with pytest.raises(ValueError, match="head changed"):
        fetch(files_path, config_dir)
    assert not files_path.exists()


def test_issue_comment_fetch_rejects_head_that_moved_after_gate(tmp_path, monkeypatch):
    api = API(head_sha="c" * 40)
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api, name="issue_comment")

    with pytest.raises(ValueError, match="head changed since gate"):
        fetch(tmp_path / "files.json", config_dir)
    assert not any(call[1].endswith("/files") for call in api.calls)


def test_fetch_rejects_event_head_that_differs_from_api(tmp_path):
    event_path = _event_file(tmp_path, head="b" * 40)
    event = load_event("pull_request_target", event_path)
    transport = API(head_sha="c" * 40)

    with pytest.raises(ValueError, match="head changed since event"):
        _pull(GitHub("test-token", event.repo, transport=transport), event)


def test_fetch_rejects_policy_sha_that_differs_from_event_base(tmp_path, monkeypatch):
    api = API()
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)

    with pytest.raises(ValueError, match="event base does not match policy SHA"):
        fetch(tmp_path / "files.json", config_dir)
    assert not any(call[1].endswith("/files") for call in api.calls)


def test_fetch_rejects_event_base_ref_retarget_race(tmp_path, monkeypatch):
    api = API()
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)
    event_path = _event_file(tmp_path, base_ref="release")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    with pytest.raises(ValueError, match="not based on the default branch"):
        fetch(tmp_path / "files.json", config_dir)
    assert not api.calls


def test_fetch_rejects_api_base_sha_that_changed_since_target_event(tmp_path, monkeypatch):
    api = API(base_sha="c" * 40)
    config_dir, _ = _fetch_setup(tmp_path, monkeypatch, api)

    with pytest.raises(ValueError, match="base changed since event"):
        fetch(tmp_path / "files.json", config_dir)
    assert not any(call[1].endswith("/files") for call in api.calls)


class ReviewApplyAPI:
    def __init__(self, *, head_sha="b" * 40, base_ref="main", base_sha="a" * 40):
        self.head_sha = head_sha
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.calls = []
        self.review_posts = []

    def __call__(self, request, timeout):
        path = urlparse(request.full_url).path.removeprefix("/repos/your-org/project/")
        self.calls.append((request.method, path))
        body = json.loads(request.data) if request.data else None
        if path == "pulls/24/files":
            value = [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "changes": 1,
                    "patch": "@@ -0,0 +1 @@\n+new",
                }
            ]
        elif request.method == "GET" and path == "pulls/24":
            value = {
                "head": {"sha": self.head_sha},
                "base": {"ref": self.base_ref, "sha": self.base_sha},
                "changed_files": 1,
            }
        elif request.method == "GET" and path in {"issues/24", "issues/24/comments"}:
            value = {"labels": [], "assignees": []} if path == "issues/24" else []
        elif request.method == "POST" and path == "pulls/24/reviews":
            self.review_posts.append(body)
            value = {}
        else:
            value = {}
        return 200, {}, json.dumps(value).encode()


def _review_apply_setup(
    tmp_path,
    monkeypatch,
    *,
    api,
    base_sha="a" * 40,
    scan_keys="scan-key",
    event_name="pull_request_target",
):
    from agent_repo.commands import review_apply

    event_path = _event_file(tmp_path, event_name, base=base_sha)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": "99",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "summary": "Review summary.",
                "findings": [],
                "scope_ok": True,
                "tests_missing": False,
                "docs_needed": False,
                "escalate": None,
            }
        )
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("EXPECTED_HEAD_SHA", "b" * 40)
    monkeypatch.setenv("AGENT_SCAN_KEYS", scan_keys)
    monkeypatch.setenv("AGENT_CANARY", "canary")
    monkeypatch.setenv("AGENT_BOT_LOGIN", "agent-review[bot]")
    from agent_repo.github import GitHub

    monkeypatch.setattr(
        review_apply, "GitHub", lambda token, repo: GitHub(token, repo, transport=api)
    )
    from agent_repo.config import Area, RepoConfig

    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}, "mode": {"review": "live"}},
        (Area("core", "Core", ("src/**",), (), False),),
        ("needs-maintainer",),
    )
    from agent_repo.charter import parse_charter

    charter = parse_charter(Path("tests/fixtures/charter/example.md").read_text())
    monkeypatch.setattr(
        review_apply,
        "_policy",
        lambda gh, sha, directory: ("a" * 40, config, charter),
    )
    return review_apply, review_path, summary


def test_review_apply_fetches_its_own_file_list_at_bound_head(tmp_path, monkeypatch):
    api = ReviewApplyAPI()
    command, review_path, summary = _review_apply_setup(tmp_path, monkeypatch, api=api)
    artifact = json.loads(review_path.read_text())
    artifact["findings"] = [{"severity": "low", "path": "src/a.py", "line": 1, "body": "New line."}]
    review_path.write_text(json.dumps(artifact))

    assert command.main([str(review_path)]) == 0
    assert ("GET", "pulls/24/files") in api.calls
    assert ("POST", "pulls/24/reviews") in api.calls
    assert api.review_posts[0]["comments"][0]["path"] == "src/a.py"
    assert api.review_posts[0]["comments"][0]["line"] == 1
    assert "COMMENT review posted" in summary.read_text()


def test_review_apply_main_rejects_head_moved_since_gate(tmp_path, monkeypatch):
    api = ReviewApplyAPI(head_sha="c" * 40)
    command, review_path, summary = _review_apply_setup(
        tmp_path, monkeypatch, api=api, event_name="issue_comment"
    )
    policy_calls = []
    policy = command._policy
    monkeypatch.setattr(
        command, "_policy", lambda *args: (policy_calls.append(args[1]), policy(*args))[1]
    )

    assert command.main([str(review_path)]) == 0
    assert not any(method == "POST" for method, _ in api.calls)
    assert "head moved" in summary.read_text()
    assert not policy_calls


def test_review_apply_main_rejects_base_mismatch_before_policy_or_write(tmp_path, monkeypatch):
    api = ReviewApplyAPI(base_sha="c" * 40)
    command, review_path, summary = _review_apply_setup(
        tmp_path, monkeypatch, api=api, base_sha="c" * 40
    )
    policy_calls = []
    policy = command._policy
    monkeypatch.setattr(
        command, "_policy", lambda *args: (policy_calls.append(args[1]), policy(*args))[1]
    )

    assert command.main([str(review_path)]) == 1
    assert not any(method == "POST" for method, _ in api.calls)
    assert "trusted review inputs unavailable" in summary.read_text()
    assert not policy_calls


def test_review_apply_main_requires_scan_keys(tmp_path, monkeypatch):
    api = ReviewApplyAPI()
    command, review_path, _ = _review_apply_setup(tmp_path, monkeypatch, api=api, scan_keys="\n")

    assert command.main([str(review_path)]) == 1
    assert not any(method == "POST" for method, _ in api.calls)


def test_review_apply_drops_finding_that_contains_github_token(tmp_path, monkeypatch):
    api = ReviewApplyAPI()
    command, review_path, summary = _review_apply_setup(tmp_path, monkeypatch, api=api)
    review_value = json.loads(review_path.read_text())
    review_value["findings"] = [
        {
            "severity": "high",
            "path": "src/app.py",
            "line": 1,
            "body": "Leaked test-token",
        }
    ]
    review_path.write_text(json.dumps(review_value))

    assert command.main([str(review_path)]) == 0
    assert not any(method == "POST" and path == "pulls/24/reviews" for method, path in api.calls)
    assert "test-token" not in summary.read_text()


def test_pull_refuses_nondefault_base_before_files_request(tmp_path):
    event_path = Path("tests/fixtures/events/pull_request_target.opened.json")
    payload = json.loads(event_path.read_text())
    payload["repository"]["full_name"] = "your-org/project"
    payload["repository"]["default_branch"] = "main"
    payload["pull_request"]["head"]["sha"] = "b" * 40
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    event = load_event("pull_request_target", event_path)
    transport = API(base_ref="release")
    gh = GitHub("test-token", "your-org/project", transport=transport)

    with pytest.raises(ValueError, match="not based on the default branch"):
        _pull(gh, event)
    assert not any(call[1].endswith("/files") for call in transport.calls)
