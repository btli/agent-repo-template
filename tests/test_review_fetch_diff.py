import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest
from agent_repo.commands.fetch_diff import _pull, fetch
from agent_repo.diff import _parse_diff
from agent_repo.event import load_event
from agent_repo.github import GitHub


class API:
    def __init__(
        self,
        *,
        base_ref="main",
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_reads=None,
        diff=None,
    ):
        self.calls = []
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.head_reads = iter(head_reads or ())
        self.diff = diff or (
            b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            b"@@ -1 +1 @@\n-old\n+new\n"
        )

    def __call__(self, request, timeout):
        path = urlparse(request.full_url).path
        accept = request.get_header("Accept")
        self.calls.append((request.method, path, accept))
        if accept == "application/vnd.github.diff":
            return (200, {}, self.diff)
        return (
            200,
            {},
            json.dumps(
                {
                    "head": {"sha": self.head_sha},
                    "base": {"ref": self.base_ref, "sha": self.base_sha},
                }
            ).encode(),
        )


def test_fetch_diff_uses_api_diff_and_never_checks_out_code(tmp_path, monkeypatch):
    event_path = Path("tests/fixtures/events/pull_request_target.opened.json")
    payload = json.loads(event_path.read_text())
    payload["repository"]["full_name"] = "your-org/project"
    payload["repository"]["default_branch"] = "main"
    payload["pull_request"]["base"]["ref"] = "main"
    payload["pull_request"]["base"]["sha"] = "a" * 40
    payload["pull_request"]["head"]["sha"] = "b" * 40
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    output = tmp_path / "github-output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    areas = config_dir / "areas.yml"
    areas.write_text(areas.read_text().replace("sensitive: true", "sensitive: false"))
    transport = API()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("EXPECTED_HEAD_SHA", "b" * 40)
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    diff_path = tmp_path / "diff.patch"
    base_sha, head_sha, summary_only, reason = fetch(diff_path, config_dir)

    assert base_sha == "a" * 40
    assert head_sha == "b" * 40
    assert summary_only is False
    assert reason == ""
    assert diff_path.read_text().startswith("diff --git")
    assert any(call[2] == "application/vnd.github.diff" for call in transport.calls)
    assert not any(call[0] == "checkout" for call in transport.calls)


@pytest.mark.parametrize(
    "diff",
    [
        b"diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        b"--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        b"@@ -1 +1 @@\n-old\n+new\n+extra\n",
        b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        b"@@ -1 +1 @@\n-old\n+new\ngarbage\n",
    ],
    ids=["mismatched-paths", "surplus-hunk-row", "garbage-row"],
)
def test_fetch_routes_ambiguous_diff_to_summary_only(tmp_path, monkeypatch, diff):
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    areas = config_dir / "areas.yml"
    areas.write_text(
        "areas:\n  - name: workflows\n    description: Workflows\n"
        "    paths:\n      - .github/**\n    owners: []\n    sensitive: true\n"
    )
    transport = API(diff=diff)
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    _, _, limited, reason = fetch(tmp_path / "diff.patch", config_dir)

    assert limited is True
    assert "needs-maintainer" in reason


@pytest.mark.parametrize("metadata", ["rename", "copy"])
@pytest.mark.parametrize(
    "scenario",
    ["conflicting-all-headers", "header-conflicts", "file-headers-conflict"],
)
def test_fetch_routes_conflicting_sensitive_source_metadata_to_summary_only(
    tmp_path, monkeypatch, metadata, scenario
):
    if scenario == "conflicting-all-headers":
        header_from = header_to = file_from = file_to = "src/a.py"
    elif scenario == "header-conflicts":
        header_from = header_to = "src/a.py"
        file_from = ".github/workflows/ci.yml"
        file_to = "src/a.py"
    else:
        header_from = ".github/workflows/ci.yml"
        header_to = file_to = "src/a.py"
        file_from = "src/a.py"
    diff = (
        f"diff --git a/{header_from} b/{header_to}\n"
        f"{metadata} from .github/workflows/ci.yml\n"
        f"{metadata} to src/a.py\n"
        f"--- a/{file_from}\n+++ b/{file_to}\n@@ -1 +1 @@\n-old\n+new\n"
    ).encode()
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    (config_dir / "areas.yml").write_text(
        "areas:\n  - name: workflows\n    description: Workflows\n"
        "    paths:\n      - .github/**\n    owners: []\n    sensitive: true\n"
    )
    transport = API(diff=diff)
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    _, _, limited, reason = fetch(tmp_path / "diff.patch", config_dir)

    assert limited is True
    assert "needs-maintainer" in reason


def test_fetch_accepts_spaced_path_rename_and_indexes_both_paths(tmp_path, monkeypatch):
    diff = (
        b"diff --git a/docs/a b.md b/docs/c d.md\n"
        b"similarity index 100%\n"
        b"rename from docs/a b.md\n"
        b"rename to docs/c d.md\n"
    )
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    (config_dir / "areas.yml").write_text("areas: []\n")
    transport = API(diff=diff)
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    _, _, limited, reason = fetch(tmp_path / "diff.patch", config_dir)
    _, paths, invalid = _parse_diff(diff.decode())

    assert limited is False
    assert reason == ""
    assert invalid is False
    assert paths == {"docs/a b.md", "docs/c d.md"}


@pytest.mark.parametrize(
    "metadata",
    [
        "old mode 100644\nnew mode 100755\n",
        "Binary files a/img/a b.png and b/img/a b.png differ\n",
    ],
    ids=["mode-only", "binary"],
)
def test_fetch_accepts_header_only_spaced_paths(tmp_path, monkeypatch, metadata):
    diff = f"diff --git a/img/a b.png b/img/a b.png\n{metadata}".encode()
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    (config_dir / "areas.yml").write_text("areas: []\n")
    transport = API(diff=diff)
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    _, _, limited, reason = fetch(tmp_path / "diff.patch", config_dir)

    assert limited is False
    assert reason == ""


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


def test_issue_comment_fetch_rejects_head_that_moved_after_gate(tmp_path, monkeypatch):
    event_path = _event_file(tmp_path, "issue_comment")
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    transport = API(head_sha="c" * 40)
    _fetch_env(monkeypatch, event_path, output, name="issue_comment")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    try:
        fetch(tmp_path / "diff.patch", config_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("head moved after gate was accepted")
    assert not any(call[2] == "application/vnd.github.diff" for call in transport.calls)
    assert not (tmp_path / "diff.patch").exists()


def test_fetch_rejects_event_head_that_differs_from_api(tmp_path):
    event_path = _event_file(tmp_path, head="b" * 40)
    event = load_event("pull_request_target", event_path)
    transport = API(head_sha="c" * 40)

    try:
        _pull(GitHub("test-token", event.repo, transport=transport), event)
    except ValueError:
        pass
    else:
        raise AssertionError("event and API heads differed")


def test_fetch_rejects_policy_sha_that_differs_from_event_base(tmp_path, monkeypatch):
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    transport = API()
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target", base="c" * 40)
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    try:
        fetch(tmp_path / "diff.patch", config_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("event base and policy SHA differed")
    assert not any(call[2] == "application/vnd.github.diff" for call in transport.calls)
    assert not (tmp_path / "diff.patch").exists()


def test_fetch_rejects_event_base_ref_retarget_race(tmp_path, monkeypatch):
    event_path = _event_file(tmp_path, base_ref="release")
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    transport = API(base_ref="main")
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    try:
        fetch(tmp_path / "diff.patch", config_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("retargeted pull request was accepted")
    assert not transport.calls


def test_fetch_rejects_api_base_sha_that_changed_since_target_event(tmp_path, monkeypatch):
    event_path = _event_file(tmp_path)
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)
    transport = API(base_sha="c" * 40)
    _fetch_env(monkeypatch, event_path, output, name="pull_request_target")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    try:
        fetch(tmp_path / "diff.patch", config_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("changed default branch base SHA was accepted")
    assert not any(call[2] == "application/vnd.github.diff" for call in transport.calls)
    assert not (tmp_path / "diff.patch").exists()


def test_fetch_rechecks_head_after_diff_read(tmp_path, monkeypatch):
    event_path = _event_file(tmp_path, "issue_comment")
    output = tmp_path / "output"
    output.write_text("")
    config_dir = tmp_path / "agents"
    shutil.copytree("tests/fixtures/agents", config_dir)

    class ChangingAPI(API):
        def __init__(self):
            super().__init__()
            self.pull_reads = 0

        def __call__(self, request, timeout):
            if request.get_header("Accept") != "application/vnd.github.diff":
                self.pull_reads += 1
                if self.pull_reads == 2:
                    self.head_sha = "c" * 40
            return super().__call__(request, timeout)

    transport = ChangingAPI()
    _fetch_env(monkeypatch, event_path, output, name="issue_comment")
    monkeypatch.setattr(
        "agent_repo.commands.fetch_diff.GitHub",
        lambda token, repo: GitHub(token, repo, transport=transport),
    )

    try:
        fetch(tmp_path / "diff.patch", config_dir)
    except ValueError:
        pass
    else:
        raise AssertionError("head change while reading diff was accepted")
    assert not (tmp_path / "diff.patch").exists()


class ReviewApplyAPI:
    def __init__(self, *, head_sha="b" * 40, base_ref="main", base_sha="a" * 40):
        self.head_sha = head_sha
        self.base_ref = base_ref
        self.base_sha = base_sha
        self.calls = []

    def __call__(self, request, timeout):
        path = urlparse(request.full_url).path.removeprefix("/repos/your-org/project/")
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "pulls/24":
            value = {
                "head": {"sha": self.head_sha},
                "base": {"ref": self.base_ref, "sha": self.base_sha},
            }
        elif request.method == "GET" and path in {"issues/24", "issues/24/comments"}:
            value = {"labels": [], "assignees": []} if path == "issues/24" else []
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
    from agent_repo.github import GitHub

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
    diff_path = tmp_path / "diff.patch"
    diff_path.write_text("")
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
    return review_apply, review_path, diff_path, summary


def test_review_apply_main_rejects_head_moved_since_gate(tmp_path, monkeypatch):
    api = ReviewApplyAPI(head_sha="c" * 40)
    command, review_path, diff_path, summary = _review_apply_setup(
        tmp_path, monkeypatch, api=api, event_name="issue_comment"
    )
    policy_calls = []
    policy = command._policy
    monkeypatch.setattr(
        command, "_policy", lambda *args: (policy_calls.append(args[1]), policy(*args))[1]
    )

    assert command.main([str(review_path), str(diff_path)]) == 0
    assert not any(method == "POST" for method, _ in api.calls)
    assert "head moved" in summary.read_text()
    assert not policy_calls


def test_review_apply_main_rejects_base_mismatch_before_policy_or_write(tmp_path, monkeypatch):
    api = ReviewApplyAPI(base_sha="c" * 40)
    command, review_path, diff_path, summary = _review_apply_setup(
        tmp_path, monkeypatch, api=api, base_sha="c" * 40
    )
    policy_calls = []
    policy = command._policy
    monkeypatch.setattr(
        command, "_policy", lambda *args: (policy_calls.append(args[1]), policy(*args))[1]
    )

    assert command.main([str(review_path), str(diff_path)]) == 1
    assert not any(method == "POST" for method, _ in api.calls)
    assert "trusted review inputs unavailable" in summary.read_text()
    assert not policy_calls


def test_review_apply_main_requires_scan_keys(tmp_path, monkeypatch):
    api = ReviewApplyAPI()
    command, review_path, diff_path, _ = _review_apply_setup(
        tmp_path, monkeypatch, api=api, scan_keys="\n"
    )

    assert command.main([str(review_path), str(diff_path)]) == 1
    assert not any(method == "POST" for method, _ in api.calls)


def test_review_apply_drops_finding_that_contains_github_token(tmp_path, monkeypatch):
    api = ReviewApplyAPI()
    command, review_path, diff_path, summary = _review_apply_setup(tmp_path, monkeypatch, api=api)
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

    assert command.main([str(review_path), str(diff_path)]) == 0
    assert not any(method == "POST" and path == "pulls/24/reviews" for method, path in api.calls)
    assert "test-token" not in summary.read_text()


def test_pull_refuses_nondefault_base_before_diff_request(tmp_path):
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

    try:
        _pull(gh, event)
    except ValueError as error:
        assert str(error) == "pull request is not based on the default branch"
    else:
        raise AssertionError("nondefault base was accepted")
    assert not any(call[2] == "application/vnd.github.diff" for call in transport.calls)
