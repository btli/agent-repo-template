import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _load_report():
    module_spec = importlib.util.spec_from_file_location(
        "dry_run_report", ROOT / "scripts/dry-run-report.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


report = _load_report()


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Serve recorded API payloads through an offline `gh` on PATH and return its call log."""
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{FIXTURES / 'fake-gh'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_API", str(FIXTURES / "dry-run" / "api.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    return log


def _sections(text: str) -> dict[str, str]:
    parts = text.split("\n## ")
    return {part.split("\n", 1)[0]: part for part in parts[1:]}


def test_report_shows_agreement_close_readiness_and_abort_flags_per_mode(fake_gh, capsys):
    argv = ["--repo", "your-org/example", "--days", "30", "--bot-login", "example-agent"]
    assert report.main([*argv, "--tier2-budget-alerts", "2"], now=NOW) == 0
    sections = _sections(capsys.readouterr().out)

    # The fork run, the expired artifact and the run older than 30 days are ignored;
    # issue 1 uses its newest decision.
    system_one = sections["Mode: system-one"]
    assert "- Decided issues: 3" in system_one
    assert "- Type agreement: 67% (3 maintainer-labelled issues)" in system_one
    assert "- Area agreement: 100% (2 maintainer-labelled issues)" in system_one
    assert "- Close candidates: 1, maintainer agreement 100%; close_enable_ready: no" in system_one

    llm_only = sections["Mode: llm-only"]
    assert "- Decided issues: 1" in llm_only
    assert "- Type agreement: 100% (1 maintainer-labelled issues)" in llm_only
    assert "Close readiness: not applicable; LLM-only mode never auto-closes." in llm_only
    assert "close_enable_ready" not in llm_only

    assert "- 1 of 2 reopened (50%)" in sections["Reopen rate for agent:closed"]
    # Only the bot's in-window P1 label counts; a human agent:fix label does not.
    assert "added by example-agent[bot]: 1" in sections["Out-of-policy actions"]
    assert sections["Abort criteria"].splitlines()[2:] == [
        "- ABORT: any out-of-policy action",
        "- ABORT: a reopen rate above 5% for `agent:closed`",
        "- ABORT: a Tier-2 key reaching 80% of its budget twice in one week. "
        "The response is to set `triage.rca_authors: known`.",
    ]

    calls = [json.loads(line) for line in fake_gh.read_text().splitlines()]
    assert all(call[0] == "api" for call in calls)
    assert ["api", "repos/your-org/example/actions/artifacts/11/zip"] not in calls


def test_report_without_optional_inputs_skips_those_criteria(fake_gh, capsys):
    assert report.main(["--repo", "your-org/example"], now=NOW) == 0
    sections = _sections(capsys.readouterr().out)
    assert "Not checked; pass --bot-login." in sections["Out-of-policy actions"]
    assert "Not checked" in sections["Tier-2 budget"]
    assert sections["Abort criteria"].splitlines()[2:] == [
        "- ABORT: a reopen rate above 5% for `agent:closed`"
    ]


def test_report_exits_zero_when_gh_fails(fake_gh, monkeypatch, tmp_path, capsys):
    missing = tmp_path / "empty.json"
    missing.write_text("{}")
    monkeypatch.setenv("FAKE_GH_API", str(missing))
    assert report.main(["--repo", "your-org/example"], now=NOW) == 0
    assert "Report incomplete: gh api failed: repos/your-org/example" in capsys.readouterr().out
    assert report.main(["--days", "30"], now=NOW) == 0
