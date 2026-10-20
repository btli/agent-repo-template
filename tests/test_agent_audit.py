import importlib.util
import os
from pathlib import Path

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


def test_audit_lists_exactly_the_marked_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", f"{FIXTURES / 'fake-gh'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_API", str(FIXTURES / "audit" / "api.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    assert audit.main(["--repo", "your-org/example", "--bot-login", "example-agent"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("- ")]
    url = "https://github.com/your-org/example"
    # The bot's unmarked comment, a forged marker from a person and the unlabelled issue
    # are not listed.
    assert lines == [
        f"- #7 triage run 1001: {url}/issues/7#issuecomment-1",
        f"- #8 off run 1002: {url}/issues/8#issuecomment-2",
        f"- #7 issue (closed), agent:closed; reopen if the close was wrong: {url}/issues/7",
        f"- #8 issue (open), agent:triage-capped; remove the label to revert: {url}/issues/8",
        f"- #10 pull request (open), agent:fix; remove the label to revert: {url}/pull/10",
    ]


def test_audit_reports_gh_failure(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setenv("PATH", f"{FIXTURES / 'fake-gh'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_API", str(empty))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    assert audit.main(["--repo", "your-org/example", "--bot-login", "example-agent"]) == 1
    assert "gh api failed" in capsys.readouterr().err
