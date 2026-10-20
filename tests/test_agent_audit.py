import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _load_audit():
    module_spec = importlib.util.spec_from_file_location(
        "agent_audit", ROOT / "scripts/agent-audit.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


audit = _load_audit()


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Put a `gh` wrapper that runs the offline fake first on PATH and check it ran."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake-gh" / "gh"}" "$@"\n')
    wrapper.chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_API", str(FIXTURES / "audit" / "api.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    yield
    assert log.read_text().strip(), "the offline gh was never called"


def test_audit_lists_exactly_the_marked_items(fake_gh, capsys):
    assert audit.main(["--repo", "your-org/example", "--bot-login", "example-agent"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("- ")]
    url = "https://github.com/your-org/example"
    # The bot's unmarked comment, a forged marker from a person, a marker from a User account
    # whose login matches the bot's, and the unlabelled issue are not listed.
    assert lines == [
        f"- #7 triage run 1001: {url}/issues/7#issuecomment-1",
        f"- #8 off run 1002: {url}/issues/8#issuecomment-2",
        f"- #7 issue (closed), agent:closed; reopen if the close was wrong: {url}/issues/7",
        f"- #8 issue (open), agent:triage-capped; remove the label to revert: {url}/issues/8",
        f"- #11 issue (open), agent:closed; remove the label to revert: {url}/issues/11",
        f"- #10 pull request (open), agent:fix; remove the label to revert: {url}/pull/10",
    ]


def test_audit_reports_gh_failure(fake_gh, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setenv("FAKE_GH_API", str(empty))
    assert audit.main(["--repo", "your-org/example", "--bot-login", "example-agent"]) == 1
    assert "gh api failed" in capsys.readouterr().err
