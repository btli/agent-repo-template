import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from agent_repo.commands.fetch_diff import _pull, fetch
from agent_repo.event import load_event
from agent_repo.github import GitHub


class API:
    def __init__(self, *, base_ref="main", head_sha="b" * 40):
        self.calls = []
        self.base_ref = base_ref
        self.head_sha = head_sha

    def __call__(self, request, timeout):
        path = urlparse(request.full_url).path
        accept = request.get_header("Accept")
        self.calls.append((request.method, path, accept))
        if accept == "application/vnd.github.diff":
            return (
                200,
                {},
                (
                    b"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
                    b"@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        return (
            200,
            {},
            json.dumps(
                {
                    "head": {"sha": self.head_sha},
                    "base": {"ref": self.base_ref, "sha": "a" * 40},
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
