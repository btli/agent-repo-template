import base64
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 20, tzinfo=UTC)
REPO = "repos/your-org/example"


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
    """Put a `gh` wrapper that runs the offline fake first on PATH; return the API map path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake-gh" / "gh"}" "$@"\n')
    wrapper.chmod(0o755)
    api = tmp_path / "api.json"
    api.write_text((FIXTURES / "dry-run" / "api.json").read_text())
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_API", str(api))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    yield api
    assert log.read_text().strip(), "the offline gh was never called"


def _edit(api: Path, change) -> None:
    responses = json.loads(api.read_text())
    change(responses)
    api.write_text(json.dumps(responses))


def _run(argv: list[str], capsys) -> dict[str, str]:
    assert report.main(["--repo", "your-org/example", *argv], now=NOW) == 0
    parts = capsys.readouterr().out.split("\n## ")
    return {part.split("\n", 1)[0]: part for part in parts[1:]}


def _lines(section: str) -> list[str]:
    return section.splitlines()[2:]


def test_report_shows_agreement_close_readiness_and_abort_flags_per_mode(fake_gh, capsys):
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "2"], capsys)

    # Only exact agent-decision-<run_id> records count, never hand-off artifacts or copies;
    # the fork run and the run older than 30 days are ignored; issue 1 uses its newest record.
    system_one = sections["Mode: system-one"]
    assert "- Decided issues: 8" in system_one
    # An issue carrying two type: labels is not compared.
    assert "- Type agreement: 67% (3 maintainer-labelled issues)" in system_one
    assert "- Area agreement: 100% (3 maintainer-labelled issues)" in system_one
    # Tier-2-disagreed and below-close_min closes are not candidates; the duplicate of an
    # open target is.
    assert "- Close candidates: 2, maintainer agreement 100%; close_enable_ready: no" in system_one

    llm_only = sections["Mode: llm-only"]
    assert "- Decided issues: 1" in llm_only
    assert "- Type agreement: 100% (1 maintainer-labelled issues)" in llm_only
    assert "Close readiness: not applicable; LLM-only mode never auto-closes." in llm_only
    assert "close_enable_ready" not in llm_only

    # Expired, absent, schema-invalid and undownloadable records are listed, not fatal.
    assert _lines(sections["Missing decision records"]) == [
        "- #8: run 107 has no readable agent-decision-107",
        "- #11: run 109 has no readable agent-decision-109",
        "- #13: run 118 has no readable agent-decision-118",
        "- #15: run 113 has no readable agent-decision-113",
        "- #16: run 114 has no readable agent-decision-114",
    ]
    # A failed duplicate-target lookup is listed rather than dropping the candidate silently.
    assert _lines(sections["Skipped issues"]) == [
        "- #17: issue lookup failed",
        "- #18: duplicate target lookup failed",
    ]
    # Bot closes of 4, 5, 6 and 21 count. Issue 5 reopened then reclosed; 21 reopened before
    # its close; 22's agent:closed came from a person; 23 was labelled but never closed.
    assert _lines(sections["Reopen rate for agent:closed"]) == [
        "- 1 of 4 bot closes with agent:closed in the window were reopened (25%)"
    ]
    # The bot's P1, its P0 on a missing-record issue, type:unauthorized, its LLM-only close
    # and its close under a Tier-2-disagreed decision.
    assert "- 5 actions by example-agent[bot] across 20 issues" in sections["Out-of-policy actions"]
    assert _lines(sections["Unverifiable bot closes"]) == [
        "- #5: closed 2026-10-01T12:00:00Z with no readable decision record",
        "- #6: closed 2026-10-05T12:00:00Z with no readable decision record",
        "- #21: closed 2026-10-09T12:00:00Z with no readable decision record",
        "- #22: closed 2026-10-08T12:00:00Z with no readable decision record",
    ]
    assert _lines(sections["Abort criteria"]) == [
        "- ABORT: any out-of-policy action",
        "- ABORT: a reopen rate above 5% for `agent:closed`",
        "- ABORT: a Tier-2 key reaching 80% of its budget twice in one week. "
        "The response is to set `triage.rca_authors: known`.",
    ]

    calls = [json.loads(line) for line in Path(os.environ["FAKE_GH_LOG"]).read_text().splitlines()]
    assert all(call[0] == "api" for call in calls)
    downloads = {call[-1] for call in calls if call[-1].endswith("/zip")}
    assert downloads == {
        f"{REPO}/actions/artifacts/{artifact_id}/zip"
        for artifact_id in (2, 5, 7, 9, 13, 22, 24, 26, 28, 30, 32, 34, 36)
    }


def test_closes_are_judged_by_their_own_run_after_close_is_disabled(fake_gh, capsys):
    config = (ROOT / "evals" / "config.yml").read_text().replace("enabled: true", "enabled: false")

    def disable_close(responses):
        content = base64.b64encode(config.encode()).decode()
        responses[f"{REPO}/contents/.agents/config.yml"]["json"]["content"] = content

    _edit(fake_gh, disable_close)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    assert (
        "candidates are only collected while `close.enabled: true`"
        in (sections["Mode: system-one"])
    )
    # Issue 4's close was eligible under its own run's decision, so it stays in policy.
    assert "- 5 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert "- #5: closed 2026-10-01T12:00:00Z" in sections["Unverifiable bot closes"]


def test_unloadable_policy_marks_taxonomy_and_close_checks_not_evaluated(fake_gh, capsys):
    _edit(fake_gh, lambda responses: responses.pop(f"{REPO}/contents/.agents/labels.yml"))
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    # The P1 and P0 labels need no policy; type:bug and type:unauthorized cannot be checked.
    assert _lines(sections["Out-of-policy actions"])[1:] == [
        "- Not evaluated: 2 type:/area: labels and every bot close, because `.agents/` could "
        "not be loaded."
    ]
    assert "- 2 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert (
        "- NOT EVALUATED: any out-of-policy action (taxonomy labels and closes: "
        in (sections["Abort criteria"])
    )


def test_label_without_a_bot_close_is_not_in_the_reopen_denominator(fake_gh, capsys):
    bot = {"login": "example-agent[bot]", "type": "Bot"}
    person = {"login": "maintainer", "type": "User"}

    def label_only_cohort(responses):
        responses[f"{REPO}/actions/artifacts"]["json"]["artifacts"] = []
        responses[f"{REPO}/issues"]["json"] = [
            {"number": number, "state": "open", "labels": [{"name": "agent:closed"}]}
            for number in range(30, 50)
        ]
        label = {"event": "labeled", "actor": bot, "label": {"name": "agent:closed"}}
        for number in range(30, 49):
            responses[f"{REPO}/issues/{number}/events"] = {
                "json": [{**label, "created_at": "2026-10-10T12:00:00Z"}]
            }
        responses[f"{REPO}/issues/49/events"] = {
            "json": [
                {**label, "created_at": "2026-10-10T12:00:00Z"},
                {"event": "closed", "actor": bot, "created_at": "2026-10-10T12:00:01Z"},
                {"event": "reopened", "actor": person, "created_at": "2026-10-11T12:00:00Z"},
            ]
        }

    _edit(fake_gh, label_only_cohort)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    assert _lines(sections["Reopen rate for agent:closed"]) == [
        "- 1 of 1 bot closes with agent:closed in the window were reopened (100%)"
    ]
    assert "- ABORT: a reopen rate above 5% for `agent:closed`" in sections["Abort criteria"]


def test_default_invocation_reports_skipped_criteria_as_not_evaluated(fake_gh, capsys):
    _edit(fake_gh, lambda responses: responses[f"{REPO}/issues"]["json"].clear())
    sections = _run(["--days", "30"], capsys)
    assert "Not evaluated; pass --bot-login." in sections["Out-of-policy actions"]
    assert "Not evaluated" in sections["Tier-2 budget"]
    # No evaluated criterion fires here, yet the result must not read as clean.
    assert _lines(sections["Abort criteria"]) == [
        "- NOT EVALUATED: a reopen rate above 5% for `agent:closed` (pass --bot-login)",
        "- NOT EVALUATED: any out-of-policy action (pass --bot-login)",
        "- NOT EVALUATED: a Tier-2 key reaching 80% of its budget twice in one week. "
        "The response is to set `triage.rca_authors: known`. (pass --tier2-budget-alerts)",
        "- No evaluated criterion triggered.",
    ]
    assert "None triggered" not in sections["Abort criteria"]


def test_report_exits_zero_when_gh_fails(fake_gh, capsys):
    fake_gh.write_text("{}")
    assert report.main(["--repo", "your-org/example"], now=NOW) == 0
    assert "Report incomplete: gh api failed: repos/your-org/example" in capsys.readouterr().out
    assert report.main(["--days", "30"], now=NOW) == 0
