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
    assert "- Decided issues: 13" in system_one
    # An issue carrying two type: labels is not compared.
    assert "- Type agreement: 67% (3 maintainer-labelled issues)" in system_one
    assert "- Area agreement: 100% (3 maintainer-labelled issues)" in system_one
    # Tier-2-disagreed and below-close_min closes are not candidates; the duplicate of an
    # open target is.
    # Issue 29's eligible close was left open by the maintainer, so agreement drops to 75%.
    # Issues 30 (security) and 31 (P1 suggestion) go to a maintainer before any close.
    assert "- Close candidates: 4, maintainer agreement 75%; close_enable_ready: no" in system_one

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
    # Each bot close followed by the bot's agent:closed label (apply's order) counts: 4, 5, 6,
    # 21, 26, 27, 30 and both closes of 28, whose first close was reopened. Issue 5 reopened then
    # reclosed; 21 reopened before its close; 22's label came from a person; 23 was never
    # closed; 24's label preceded the close; 25's followed a reopen.
    assert _lines(sections["Reopen rate for agent:closed"]) == [
        "- 2 of 9 bot closes with agent:closed in the window were reopened (22%)"
    ]
    # The bot's P1, its P0 on a missing-record issue, type:unauthorized, its LLM-only close,
    # its close under a Tier-2-disagreed decision, and issue 27's close, whose newer run had
    # no close (issue 26's newer run allowed its close), and issue 30's close under a security
    # decision.
    assert "- 7 actions by example-agent[bot] across 28 issues" in sections["Out-of-policy actions"]
    assert _lines(sections["Out-of-policy actions"])[1:] == [
        "- #3: closed 2026-10-12T12:00:00Z; its run 104's decision does not allow a close",
        "- #13: closed 2026-10-15T12:00:00Z; its run 111's decision does not allow a close",
        "- #27: closed 2026-10-19T12:00:00Z; its run 122's decision does not allow a close",
        "- #30: closed 2026-10-17T12:00:00Z; its run 124's decision does not allow a close",
        "- Not evaluated: 8 bot closes are unverifiable (listed below).",
    ]
    assert _lines(sections["Unverifiable bot closes"]) == [
        "- #5: closed 2026-10-01T12:00:00Z with no readable decision record",
        "- #6: closed 2026-10-05T12:00:00Z with no readable decision record",
        "- #21: closed 2026-10-09T12:00:00Z with no readable decision record",
        "- #22: closed 2026-10-08T12:00:00Z with no readable decision record",
        "- #24: closed 2026-10-08T12:00:01Z with no readable decision record",
        "- #25: closed 2026-10-08T12:00:00Z with no readable decision record",
        "- #28: closed 2026-10-10T12:00:00Z with no readable decision record",
        "- #28: closed 2026-10-12T12:00:00Z with no readable decision record",
    ]
    assert _lines(sections["Abort criteria"]) == [
        "- ABORT: any out-of-policy action",
        "- ABORT: a reopen rate above 5% for `agent:closed`",
        "- ABORT: a Tier-2 key reaching 80% of its budget twice in one week. "
        "The response is to set `triage.rca_authors: known`.",
        "- NOT EVALUATED: any out-of-policy action (8 unverifiable bot closes)",
    ]

    calls = [json.loads(line) for line in Path(os.environ["FAKE_GH_LOG"]).read_text().splitlines()]
    assert all(call[0] == "api" for call in calls)
    downloads = {call[-1] for call in calls if call[-1].endswith("/zip")}
    assert downloads == {
        f"{REPO}/actions/artifacts/{artifact_id}/zip"
        for artifact_id in (
            2,
            5,
            7,
            9,
            13,
            22,
            24,
            26,
            28,
            30,
            32,
            34,
            36,
            40,
            42,
            44,
            46,
            48,
            50,
            52,
        )
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
    # Issue 4's close was eligible under the policy at its run's base_sha, so it stays in policy.
    assert "- 7 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert "- #5: closed 2026-10-01T12:00:00Z" in sections["Unverifiable bot closes"]


def _set_policy_file(responses, name: str, text: str, ref: str = "") -> None:
    key = f"{REPO}/contents/.agents/{name}" + (f"?ref={ref}" if ref else "")
    responses[key] = {
        "json": {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
    }


def test_closes_are_judged_by_the_policy_at_their_base_sha(fake_gh, capsys):
    config = (ROOT / "evals" / "config.yml").read_text()
    raised = config.replace("close_min: 0.92", "close_min: 0.99")
    _edit(fake_gh, lambda responses: _set_policy_file(responses, "config.yml", raised))
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    # A later, stricter close_min does not turn issue 4's p=0.95 close into a violation.
    assert "- 7 actions by example-agent[bot]" in sections["Out-of-policy actions"]

    def unloadable_history(responses):
        responses[f"{REPO}/contents/.agents/config.yml?ref={'a' * 40}"] = {"json": {}}

    _edit(fake_gh, unloadable_history)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    # Without the historical policy, the closes of 3, 4, 13, 26, 27 and 30 cannot be judged.
    assert "- 3 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert (
        f"- #4: closed 2026-10-13T12:00:00Z with no loadable policy at {'a' * 40}"
        in (sections["Unverifiable bot closes"])
    )


def test_unverifiable_close_keeps_an_otherwise_clean_report_not_evaluated(fake_gh, capsys):
    def only_issue_4_closed_without_history(responses):
        for key in list(responses):
            if key.endswith("/events") and key != f"{REPO}/issues/4/events":
                responses[key] = {"json": []}
        responses[f"{REPO}/contents/.agents/config.yml?ref={'a' * 40}"] = {"json": {}}

    _edit(fake_gh, only_issue_4_closed_without_history)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    assert _lines(sections["Out-of-policy actions"])[1:] == [
        "- Not evaluated: 1 bot closes are unverifiable (listed below)."
    ]
    assert "- 0 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert _lines(sections["Abort criteria"]) == [
        "- NOT EVALUATED: any out-of-policy action (1 unverifiable bot closes)",
        "- No evaluated criterion triggered.",
    ]
    assert "None triggered" not in sections["Abort criteria"]


def test_failed_event_lookups_make_event_criteria_not_evaluated(fake_gh, capsys):
    def no_events(responses):
        for key in [key for key in responses if key.endswith("/events")]:
            del responses[key]

    _edit(fake_gh, no_events)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    assert "- #4: issue events lookup failed" in sections["Skipped issues"]
    assert (
        "- Not evaluated: 28 issue event lookups failed (listed under Skipped issues)."
        in (sections["Reopen rate for agent:closed"])
    )
    assert _lines(sections["Abort criteria"]) == [
        "- NOT EVALUATED: a reopen rate above 5% for `agent:closed` "
        "(28 issue event lookups failed)",
        "- NOT EVALUATED: any out-of-policy action (28 issue event lookups failed)",
        "- No evaluated criterion triggered.",
    ]
    assert "None triggered" not in sections["Abort criteria"]


def test_agreement_below_80_percent_over_50_issues_reaches_the_abort_criteria(fake_gh, capsys):
    def fifty_disagreeing_issues(responses):
        record = responses[f"{REPO}/actions/artifacts/2/zip"]["zip"]["decision.json"]
        artifacts = responses[f"{REPO}/actions/artifacts"]["json"]["artifacts"]
        for number in range(200, 250):
            run = {"id": 1000 + number, "repository_id": 1, "head_repository_id": 1}
            for offset, name in ((0, f"agent-triage-{number}"), (1, f"agent-decision-{run['id']}")):
                artifacts.append(
                    {
                        "id": 10 * number + offset,
                        "name": name,
                        "expired": False,
                        "created_at": "2026-10-16T10:00:00Z",
                        "workflow_run": run,
                    }
                )
            decision = {**record, "run_id": str(run["id"])}
            responses[f"{REPO}/actions/artifacts/{10 * number + 1}/zip"] = {
                "zip": {"decision.json": decision}
            }
            # The decision says type:bug; the maintainer labelled every issue type:feature.
            labels = [{"name": "type:feature"}, {"name": "area:cli"}]
            responses[f"{REPO}/issues/{number}"] = {
                "json": {"number": number, "state": "open", "labels": labels}
            }
            responses[f"{REPO}/issues/{number}/events"] = {"json": []}

    _edit(fake_gh, fifty_disagreeing_issues)
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    flag = "- ABORT: dry-run agreement on type or area under 80% after 50 issues"
    assert "- Type agreement: 4% (53 maintainer-labelled issues)" in sections["Mode: system-one"]
    assert flag in sections["Mode: system-one"]
    assert flag in _lines(sections["Abort criteria"])


def test_area_label_needs_labels_yml_as_well_as_areas_yml(fake_gh, capsys):
    labels = (ROOT / "evals" / "labels.yml").read_text().replace("  - area:cli\n", "")
    _edit(fake_gh, lambda responses: _set_policy_file(responses, "labels.yml", labels))
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    # cli stays in areas.yml, but apply cannot add area:cli without it in labels.yml.
    assert "- 8 actions by example-agent[bot]" in sections["Out-of-policy actions"]


def test_unloadable_policy_marks_taxonomy_and_close_checks_not_evaluated(fake_gh, capsys):
    _edit(fake_gh, lambda responses: responses.pop(f"{REPO}/contents/.agents/labels.yml"))
    sections = _run(["--bot-login", "example-agent", "--tier2-budget-alerts", "0"], capsys)
    # The P1 and P0 labels need no policy; type:bug, type:unauthorized and area:cli cannot be
    # checked. Closes are still judged by the policy at their own base_sha.
    assert _lines(sections["Out-of-policy actions"])[-2] == (
        "- Not evaluated: 3 type:/area: labels, because the default branch's `.agents/` could "
        "not be loaded."
    )
    assert "- 6 actions by example-agent[bot]" in sections["Out-of-policy actions"]
    assert (
        "- NOT EVALUATED: any out-of-policy action (taxonomy labels: "
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
                {"event": "closed", "actor": bot, "created_at": "2026-10-10T12:00:00Z"},
                {**label, "created_at": "2026-10-10T12:00:01Z"},
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
