import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from agent_repo.charter import _sections, lint_charter
from agent_repo.preflight import PreflightContext, analyze
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import load_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_rendered_consumer_policy_is_valid_and_stays_in_temporary_directory(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("b" * 40, "v0.0.0", temporary_parent=tmp_path)
    consumer = rendered / "consumer"
    config = load_yaml(consumer / ".agents/config.yml")
    charter = (consumer / ".agents/charter.md").read_text()

    assert rendered.is_relative_to(tmp_path)
    assert not rendered.is_relative_to(Path.cwd() / ".github")
    assert validate(config, load_schema("config")) == []
    assert lint_charter(charter) == []


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "setup_key",
        "job_key",
        "workflow_key",
        "second_model",
        "key_input",
        "dynamic_setup_key",
    ],
)
def test_rendered_consumer_preflight_and_role_key_scope(mutation, tmp_path):
    from tools.render_consumer import render_consumer

    template_ref = "43c2d3b58ce3ebccd38d9ee2a05464966b6a94da"
    rendered = render_consumer(template_ref, "v0.0.0", temporary_parent=tmp_path)
    workflows = {
        path.name: load_yaml(path)
        for path in (rendered / "consumer/.github/workflows").glob("*.yml")
    }
    context = PreflightContext("main", template_ref=template_ref)
    canonical = load_yaml(Path("tests/fixtures/preflight/template_fix_propose.yml"))
    fixture = workflows["agent-fix.yml"]
    assert fixture["jobs"]["gate"] == canonical["jobs"]["gate"]
    assert fixture["jobs"]["propose"] == canonical["jobs"]["propose"]
    result = analyze(workflows, context)
    assert [(item.rule, item.job, item.secrets) for item in result] == [
        ("template-fix-propose", "propose", ("AGENT_FIX_KEY",))
    ]
    propose = fixture["jobs"]["propose"]
    assert propose["steps"][4]["with"]["agent-timeout"] < propose["timeout-minutes"] * 60 - 240
    if mutation:
        job = workflows["agent-review.yml"]["jobs"]["agent-propose-review"]
        key = {"LLM_API_KEY": "${{ secrets.AGENT_REVIEW_KEY }}"}
        if mutation == "setup_key":
            job["steps"][1]["env"] = key
        elif mutation == "dynamic_setup_key":
            job["steps"][1]["env"] = {"LLM_API_KEY": "${{ secrets[vars.ROLE_KEY] }}"}
        elif mutation == "job_key":
            job["env"] = key
        elif mutation == "workflow_key":
            workflows["agent-review.yml"]["env"] = key
        elif mutation == "second_model":
            job["steps"].append(dict(job["steps"][2]))
        else:
            job["steps"][2]["with"]["extra"] = "${{ secrets.AGENT_REVIEW_KEY }}"
        result = analyze(workflows, context)
        assert any(
            item.workflow == "agent-review.yml" and item.rule == "pull_request_target"
            for item in result
        )


def test_rendered_workflow_has_no_template_tokens(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("c" * 40, "v0.0.0", temporary_parent=tmp_path)
    workflow = (rendered / "consumer/.github/workflows/agent-triage.yml").read_text()
    workflow_data = load_yaml(rendered / "consumer/.github/workflows/agent-triage.yml")
    assert "concurrency" not in workflow_data["jobs"]["apply"]
    assert not _secret_references(workflow_data.get("env", {}))
    assert not any(
        _secret_references(job.get("env", {})) or _secret_references(job.get("secrets", {}))
        for job in workflow_data["jobs"].values()
    )
    apply_steps = workflow_data["jobs"]["apply"]["steps"]
    apply_index = next(
        i for i, step in enumerate(apply_steps) if step.get("name") == "Apply triage decision"
    )
    record_index = next(
        i
        for i, step in enumerate(apply_steps)
        if step.get("name") == "Share applied decision record"
    )
    record_step = apply_steps[record_index]

    assert "__TEMPLATE_SHA__" not in workflow
    assert "__TEMPLATE_VERSION__" not in workflow
    assert "@" + "c" * 40 + " # v0.0.0" in workflow
    assert "name: agent-decision-${{ github.run_id }}" in workflow
    assert "retention-days: 30" in workflow
    assert "name: agent-handoff-tier1-${{ github.run_id }}" in workflow
    assert record_index > apply_index
    assert record_step["if"] == "success()"
    assert record_step["uses"].startswith("actions/upload-artifact@")
    assert record_step["with"]["name"] == "agent-decision-${{ github.run_id }}"
    assert record_step["with"]["path"].endswith("/decision.json")


def test_rendered_triage_workflow_pins_security_contract(tmp_path: Path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("d" * 40, "v0.0.0", temporary_parent=tmp_path)
    policy = rendered / "consumer"
    charter_path = policy / ".agents/charter.md"
    charter_text = charter_path.read_text().replace(
        "Add links to maintained user and contributor documentation here.",
        "- [User guide](https://example.com/user-guide)\n"
        "- [Contributing](https://example.com/contributing)",
    )
    charter_path.write_text(charter_text)
    contributing_text = "# Contributor guidance\n\nUse focused changes and run the checks.\n"
    (policy / "CONTRIBUTING.md").write_text(contributing_text)
    workflow = load_yaml(rendered / "consumer/.github/workflows/agent-triage.yml")
    jobs = workflow["jobs"]
    gate_steps = jobs["gate"]["steps"]
    gate_index = next(i for i, step in enumerate(gate_steps) if step.get("id") == "gate")
    marker_step = gate_steps[gate_index + 1]
    propose_steps = jobs["propose"]["steps"]
    tier2_steps = jobs["tier2"]["steps"]
    apply_steps = jobs["apply"]["steps"]

    assert jobs["propose"]["if"] == (
        "${{ needs.gate.outputs.reason != 'agent-off' && "
        "(needs.gate.outputs.run == 'true' || needs.gate.outputs.capped == 'true') }}"
    )
    assert jobs["propose"]["environment"] == "agent-propose-triage"
    assert jobs["propose"]["concurrency"] == {
        "group": "agent-triage-${{ github.event.issue.number }}",
        "cancel-in-progress": True,
    }
    assert jobs["tier2"]["if"] == (
        "${{ needs.gate.outputs.reason != 'agent-off' && needs.propose.outputs.tier2 == 'true' }}"
    )
    assert jobs["tier2"]["environment"] == "agent-propose-triage"
    assert jobs["tier2"]["concurrency"] == {
        "group": "agent-triage-tier2",
        "cancel-in-progress": False,
    }
    assert jobs["tier2"]["outputs"]["merged"] == (
        "${{ steps.triage.outcome == 'success' && "
        "steps.share-merged-decision.outcome == 'success' }}"
    )
    assert jobs["apply"]["if"] == (
        "${{ always() && needs.gate.result == 'success' && "
        "needs.propose.result != 'cancelled' && "
        "(needs.propose.result == 'success' || "
        "needs.gate.outputs.reason == 'agent-off' || "
        "needs.gate.outputs.capped == 'true') }}"
    )
    assert jobs["apply"]["environment"] == "agent-apply"
    for job_name in ("propose", "tier2"):
        assert "needs.gate.outputs.reason != 'agent-off'" in jobs[job_name]["if"]

    context_guard = (
        "${{ steps.gate.outputs.run == 'true' && "
        "steps.gate.outputs.tier2_allowed == 'true' && "
        "steps.gate.outputs.reason != 'agent-off' }}"
    )
    context_steps = {
        step["name"]: step
        for step in gate_steps
        if step.get("name")
        in {
            "Read default-branch triage policy",
            "Build triage hand-off files",
            "Share triage context",
        }
    }
    assert set(context_steps) == {
        "Read default-branch triage policy",
        "Build triage hand-off files",
        "Share triage context",
    }
    assert all(step["if"] == context_guard for step in context_steps.values())
    assert "ISSUE_NUMBER" not in context_steps["Build triage hand-off files"].get("env", {})

    context_output = tmp_path / "built-context"
    context_builder = context_steps["Build triage hand-off files"]["run"]
    subprocess.run(
        ["bash", "-e", "-u", "-o", "pipefail", "-c", context_builder],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "POLICY_DIR": str(rendered / "consumer"),
            "OUTPUT_DIR": str(context_output),
        },
    )
    context_data = json.loads((context_output / "triage-context.json").read_text())
    triage_run = ast.parse(Path("actions/triage-agent/run.py").read_text())
    required_context = next(
        ast.literal_eval(node.value)
        for node in ast.walk(triage_run)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "required" for target in node.targets)
    )
    assert set(context_data) == required_context
    assert all(isinstance(value, str) for value in context_data.values())
    assert context_data["charter"] == charter_text
    assert context_data["contributing"] == contributing_text
    assert context_data["docs_index"] == _sections(charter_text)["Docs index"]

    assert jobs["apply"].get("concurrency") is None

    uploaded_names = [
        (job_name, step.get("with", {}).get("name"))
        for job_name, job in jobs.items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    downloaded_names = [
        (job_name, step.get("with", {}).get("name"))
        for job_name, job in jobs.items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    handoff_uploads = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        and str(step.get("with", {}).get("name", "")).startswith("agent-handoff-")
    ]
    assert handoff_uploads
    assert all(step["with"]["retention-days"] == 1 for step in handoff_uploads)
    assert all(
        (upload_job == "gate" and name == "agent-triage-${{ github.event.issue.number }}")
        or (upload_job == "apply" and name == "agent-decision-${{ github.run_id }}")
        or any(
            downloaded_name == name
            and list(jobs).index(download_job) > list(jobs).index(upload_job)
            for download_job, downloaded_name in downloaded_names
        )
        for upload_job, name in uploaded_names
    )

    assert not any(step.get("name") == "Share marker source" for step in gate_steps)

    assert gate_steps[0]["name"] == "Prepare triage marker source"
    assert marker_step["name"] == "Record triage cycle marker"
    assert marker_step["if"] == (
        "${{ steps.gate.outputs.run == 'true' && steps.gate.outputs.reason != 'agent-off' }}"
    )
    assert marker_step["with"]["name"] == "agent-triage-${{ github.event.issue.number }}"
    assert marker_step["with"]["retention-days"] == 90
    policy_checkout = context_steps["Read default-branch triage policy"]
    assert policy_checkout["with"]["persist-credentials"] is False
    # The hand-off must read the same policy commit the gate decided on.
    assert policy_checkout["with"]["ref"] == "${{ github.sha }}"
    assert (
        next(
            step
            for step in gate_steps
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        == marker_step
    )
    assert jobs["propose"]["needs"] == "gate"
    assert not any("marker" in str(step.get("with", {}).get("name", "")) for step in propose_steps)
    assert all(
        step.get("name") not in {"Checkout triage policy", "Download triage context"}
        for step in propose_steps
    )
    assert jobs["propose"]["concurrency"]["cancel-in-progress"] is True
    assert "needs.propose.result != 'cancelled'" in jobs["apply"]["if"]

    expected_permissions = {
        "gate": {"actions": "read", "contents": "read", "issues": "read"},
        "propose": {"actions": "read", "contents": "read", "issues": "read"},
        "tier2": {},
        "apply": {},
    }
    assert {name: job.get("permissions") for name, job in jobs.items()} == expected_permissions
    assert all(
        "run" not in step
        for job_name in ("propose", "tier2", "apply")
        for step in jobs[job_name]["steps"]
    )

    secret_uses = {}
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            for value in [*step.get("env", {}).values(), *step.get("with", {}).values()]:
                for secret in re.findall(r"\$\{\{\s*secrets\.([A-Z0-9_]+)", str(value)):
                    secret_uses.setdefault(secret, []).append((job_name, step.get("name")))
    assert secret_uses == {
        "AGENT_SYSTEM_ONE_KEY": [("propose", "Build Tier-1 decision")],
        "AGENT_TRIAGE_KEY": [("tier2", "Run Tier-2 triage")],
        "AGENT_APP_PRIVATE_KEY": [("apply", "Create issue token")],
        "AGENT_SCAN_KEYS": [("apply", "Apply triage decision")],
    }

    token_step = next(step for step in apply_steps if step.get("id") == "app-token")
    assert token_step["with"]["permission-issues"] == "write"
    assert token_step["with"]["permission-contents"] == "read"
    decision_step = next(step for step in propose_steps if step.get("id") == "decide")
    assert decision_step["with"]["canary"] == "${{ needs.gate.outputs.canary }}"
    apply_decision = next(
        step for step in apply_steps if step.get("name") == "Apply triage decision"
    )
    assert apply_decision["with"]["capped"] == "${{ needs.gate.outputs.capped }}"
    assert apply_decision["with"]["off-authorized"] == (
        "${{ needs.gate.outputs.reason == 'agent-off' }}"
    )
    assert apply_decision["with"]["canary"] == "${{ needs.gate.outputs.canary }}"
    assert apply_decision["with"]["scan-keys"] == "${{ secrets.AGENT_SCAN_KEYS }}"
    decision_record = next(
        step for step in apply_steps if step.get("name") == "Share applied decision record"
    )
    assert apply_steps.index(decision_record) > apply_steps.index(apply_decision)
    assert decision_record["if"] == "success()"
    assert decision_record["with"]["name"] == "agent-decision-${{ github.run_id }}"
    assert decision_record["with"]["retention-days"] == 30
    assert any(step.get("id") == "triage" for step in tier2_steps)
    tier2_step = next(step for step in tier2_steps if step.get("id") == "triage")
    merged_upload = next(
        step for step in tier2_steps if step.get("name") == "Share merged decision"
    )
    assert tier2_step["continue-on-error"] is True
    assert merged_upload["id"] == "share-merged-decision"
    assert merged_upload["if"] == "steps.triage.outcome == 'success'"

    merged_download = next(
        step for step in apply_steps if step.get("name") == "Download merged decision"
    )
    tier1_download = next(
        step for step in apply_steps if step.get("name") == "Download Tier-1 decision"
    )
    assert merged_download["if"] == "needs.tier2.outputs.merged == 'true'"
    assert tier1_download["if"] == (
        "${{ needs.propose.result == 'success' && needs.tier2.outputs.merged != 'true' }}"
    )


def _secret_references(value: object) -> list[str]:
    if isinstance(value, dict):
        return [secret for child in value.values() for secret in _secret_references(child)]
    if isinstance(value, list):
        return [secret for child in value for secret in _secret_references(child)]
    return re.findall(r"\$\{\{\s*secrets\.[A-Z0-9_]+", str(value))


def test_render_consumer_rejects_destination_inside_repository_root(monkeypatch):
    import tools.render_consumer as render_module
    from tools.render_consumer import ROOT, render_consumer

    rendered = None
    try:
        monkeypatch.setattr(render_module.tempfile, "gettempdir", lambda: str(ROOT))
        with pytest.raises(ValueError, match="repository root"):
            rendered = render_consumer("e" * 40, "v0.0.0", temporary_parent=ROOT / ".github")
    finally:
        if rendered is not None:
            shutil.rmtree(rendered)


def test_render_consumer_rejects_destination_outside_system_temporary_directory(
    tmp_path: Path, monkeypatch
):
    import tools.render_consumer as render_module
    from tools.render_consumer import render_consumer

    monkeypatch.setattr(render_module.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))
    with pytest.raises(ValueError, match="system temporary directory"):
        render_consumer("f" * 40, "v0.0.0", temporary_parent=tmp_path)


def test_rendered_fix_apply_and_verify_boundaries(tmp_path):
    from tools.render_consumer import render_consumer

    rendered = render_consumer("b" * 40, "v0.0.0", temporary_parent=tmp_path)
    fix = load_yaml(rendered / "consumer/.github/workflows/agent-fix.yml")
    assert fix["on"] == {"issue_comment": {"types": ["created"]}, "issues": {"types": ["labeled"]}}
    apply = fix["jobs"]["apply"]
    assert apply["needs"] == ["gate", "propose"]
    assert apply["if"] == "needs.propose.result == 'success'"
    assert apply["environment"] == "agent-apply" and "concurrency" not in apply
    checkouts = [
        step for step in apply["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert {step["with"]["path"] for step in checkouts} == {"work", "policy"}
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)
    token = next(step for step in apply["steps"] if step.get("id") == "app-token")
    assert {
        key: value for key, value in token["with"].items() if key.startswith("permission-")
    } == {
        "permission-contents": "write",
        "permission-pull-requests": "write",
        "permission-issues": "write",
    }
    context = next(step for step in apply["steps"] if step.get("name") == "Download gate context")
    assert context["with"]["name"] == "agent-fix-context-${{ github.run_id }}"
    action = load_yaml(Path("actions/apply/action.yml"))
    command = next(step for step in action["runs"]["steps"] if step.get("name") == "Apply fix")
    assert command["if"] == "inputs.mode == 'fix'"
    assert 'agent_repo_cli.py" apply_fix' in command["run"]
    assert command["env"]["FIX_CONTEXT"] == "${{ inputs.context-file }}"
    verify = load_yaml(rendered / "consumer/.github/workflows/agent-verify.yml")
    assert set(verify["on"]) == {"pull_request"} and not (verify["on"]["pull_request"] or {}).get(
        "branches"
    )
    job = verify["jobs"]["verify"]
    assert job["permissions"] == {"contents": "read"}
    for expression in [
        "startsWith(github.head_ref, 'agent/fix-')",
        "github.event.pull_request.head.repo.full_name == github.repository",
        "github.event.pull_request.base.ref == github.event.repository.default_branch",
        "github.event.pull_request.user.type == 'Bot'",
    ]:
        assert expression in job["if"]
    assert "secrets." not in json.dumps(verify)
    assert "setup" not in json.dumps(job["steps"])
