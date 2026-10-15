import copy
import importlib
import json
from pathlib import Path
from urllib.parse import urlunsplit

import pytest
from agent_repo.schema import load_schema, validate

DATABASE_URL = urlunsplit(("postgresql", "alice:s3cr3t@db", "", "", ""))


@pytest.fixture
def decision():
    return json.loads(Path("tests/fixtures/triage/tier1.json").read_text())


def merge(*args, **kwargs):
    kwargs.setdefault("allowed_labels", {"type:bug", "type:question", "area:core", "area:ui"})
    return importlib.import_module("agent_repo.commands.triage_merge").merge(*args, **kwargs)


def test_keeps_tier1_authority_and_copies_only_corrections(decision):
    original = copy.deepcopy(decision)
    tier2 = copy.deepcopy(decision)
    tier2.update(
        tier=2,
        answers={},
        security=True,
        notes=["untrusted"],
        labels=["type:question", "area:ui", "priority:P0", "new"],
        suggestions={"priority": "P0", "labels": []},
    )
    tier2["close"].update(target=99, tier2_agrees=True)
    tier2["rca"] = {
        "hypothesis": "Likely parser.",
        "locations": [{"path": "src.py", "line": 2}],
        "confidence": "low",
        "confirm_with": "Run repro.",
    }
    result = merge(decision, tier2, None)
    assert result["answers"] == original["answers"]
    assert result["labels"] == ["priority:P2", "keep", "type:question", "area:ui"]
    assert result["suggestions"] == original["suggestions"]
    assert result["close"]["target"] == 42
    assert result["close"]["tier2_agrees"] is False
    assert result["security"] is False and result["notes"] == []
    assert result["rca"]["hypothesis"] == "Likely parser."
    assert result["tier"] == original["tier"] == 1
    assert validate(result, load_schema("decision")) == []
    assert decision == original


@pytest.mark.parametrize(
    "bad",
    [
        {"labels": "bad"},
        {"rca": {"hypothesis": "incomplete"}},
        {"run_id": "other"},
        {"base_sha": "b" * 40},
        {"extra": "credential-value"},
    ],
)
def test_invalid_tier2_escalates_and_keeps_tier1(decision, bad):
    tier2 = copy.deepcopy(decision)
    tier2.update(bad)
    result = merge(decision, tier2, None)
    assert result["escalate"]["to"] == "maintainer"
    for key in ["answers", "labels", "rca", "close", "security", "notes"]:
        assert result[key] == decision[key]
    assert "credential-value" not in json.dumps(result)
    assert validate(result, load_schema("decision")) == []


def test_runtime_error_is_sanitized(decision):
    result = merge(decision, None, "credential-value")
    assert result["escalate"]["to"] == "maintainer"
    assert "credential-value" not in json.dumps(result)
    assert result["answers"] == decision["answers"]


@pytest.mark.parametrize("failure", ["runtime", "invalid", "taxonomy"])
def test_failed_tier2_clears_inherited_close_agreement(decision, failure):
    decision["close"]["tier2_agrees"] = True
    original = copy.deepcopy(decision)
    tier2 = copy.deepcopy(decision)
    if failure == "invalid":
        tier2["run_id"] = "other"
    if failure == "taxonomy":
        tier2["labels"] = ["type:invented"]
    result = merge(decision, tier2, "failed" if failure == "runtime" else None)
    assert result["close"]["tier2_agrees"] is False
    assert result["escalate"]["to"] == "maintainer"
    assert decision == original


def test_matching_close_vote_and_info_request(decision):
    tier2 = copy.deepcopy(decision)
    tier2["close"]["tier2_agrees"] = True
    tier2["needs_info"] = {"text": "Which version?"}
    result = merge(decision, tier2, None)
    assert result["close"]["tier2_agrees"] is True
    assert result["needs_info"] == {"text": "Which version?"}
    assert validate(result, load_schema("decision")) == []


@pytest.mark.parametrize("mode", ["null", "replacement", "runtime", "taxonomy"])
def test_tier1_escalation_is_preserved(decision, mode):
    decision["escalate"] = {"to": "maintainer", "reason": "Tier-1 needs a maintainer."}
    original = copy.deepcopy(decision)
    tier2 = copy.deepcopy(decision)
    tier2["escalate"] = (
        {"to": "maintainer", "reason": "A different reason."} if mode == "replacement" else None
    )
    if mode == "taxonomy":
        tier2["labels"] = ["type:invented"]
    result = merge(decision, tier2, "failed" if mode == "runtime" else None)
    assert result["escalate"] == original["escalate"]
    assert decision == original


@pytest.mark.parametrize(
    "labels,expected",
    [
        ([], ["type:bug", "area:core", "priority:P2", "keep"]),
        (["type:question"], ["area:core", "priority:P2", "keep", "type:question"]),
        (["area:ui"], ["type:bug", "priority:P2", "keep", "area:ui"]),
    ],
)
def test_omitted_label_groups_are_preserved(decision, labels, expected):
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = labels
    result = merge(decision, tier2, None)
    assert result["labels"] == expected
    assert result["escalate"] is None


def test_merge_cli_is_idempotent(tmp_path, decision):
    import os
    import subprocess
    import sys

    source = Path("tests/fixtures/triage/tier1.json")
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = ["type:feature", "area:unknown"]
    tier2["needs_info"] = {"text": "Which version?"}
    tier2["close"]["tier2_agrees"] = True
    incoming = tmp_path / "tier2.json"
    incoming.write_text(json.dumps(tier2))
    output = tmp_path / "decision.json"
    args = [str(source), str(incoming), str(output), "tests/fixtures/agents"]
    command = [sys.executable, "-m", "agent_repo.commands.triage_merge", *args]
    env = dict(os.environ, PYTHONPATH=str(Path("lib").resolve()))
    assert subprocess.run(command, env=env, capture_output=True).returncode == 0
    before = output.stat().st_mtime_ns
    assert subprocess.run(command, env=env, capture_output=True).returncode == 0
    assert output.stat().st_mtime_ns == before
    result = json.loads(output.read_text())
    assert result["answers"] == decision["answers"]
    assert result["labels"] == ["priority:P2", "keep", "type:feature", "area:unknown"]
    assert result["needs_info"] == tier2["needs_info"]
    assert result["close"]["tier2_agrees"] is True
    assert result["escalate"] is None


@pytest.mark.parametrize("taxonomy", [None, "missing"])
def test_merge_cli_refuses_missing_taxonomy(tmp_path, taxonomy):
    command = importlib.import_module("agent_repo.commands.triage_merge")
    source = "tests/fixtures/triage/tier1.json"
    output = tmp_path / "decision.json"
    args = [source, source, str(output)]
    if taxonomy is not None:
        args.append(str(tmp_path / taxonomy))
    assert command.main(args) != 0
    assert not output.exists()


@pytest.mark.parametrize(
    "labels", [["type:invented"], ["area:invented"], ["type:bug", "type:question"]]
)
def test_unknown_or_multiple_types_escalate(decision, labels):
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = labels
    result = merge(decision, tier2, None)
    assert result["escalate"]["to"] == "maintainer"
    assert result["labels"] == decision["labels"]


def test_label_corrections_are_deduplicated(decision):
    tier2 = copy.deepcopy(decision)
    tier2["labels"] = ["type:question", "type:question", "area:ui", "area:ui"]
    assert merge(decision, tier2, None)["labels"] == [
        "priority:P2",
        "keep",
        "type:question",
        "area:ui",
    ]


def test_missing_taxonomy_escalates(decision):
    assert merge(decision, decision, None, allowed_labels=None)["escalate"]["to"] == "maintainer"


@pytest.fixture
def action(monkeypatch, tmp_path, decision):
    import importlib.util
    import os
    import subprocess

    monkeypatch.setattr(os, "environ", {"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    root = tmp_path / "repo"
    root.mkdir()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    for key, value in {
        "DECISION_FILE": json.dumps(decision),
        "CONTEXT_FILE": json.dumps(
            {
                "config": Path("tests/fixtures/agents/config.yml").read_text(),
                "charter": "report",
                "contributing": "tests",
                "docs_index": "README.md",
                "labels": json.dumps({"labels": ["type:bug", "type:question", "area:core"]}),
                "areas": Path("tests/fixtures/agents/areas.yml").read_text(),
            }
        ),
        "ISSUE_FILE": "Parser bug",
    }.items():
        path = runner_temp / key
        path.write_text(value)
        monkeypatch.setenv(key, str(path))
    output = runner_temp / "result.json"
    for key, value in {
        "GITHUB_WORKSPACE": str(root),
        "RUNNER_TEMP": str(runner_temp),
        "TRIAGE_AGENT": str(Path("agents/triage").resolve()),
        "OUTPUT_FILE": str(output),
        "API_KEY_ENV": "ROLE_KEY",
        "ROLE_KEY": "credential-value",
        "SENTINEL_JOB_SECRET": "unrelated-job-secret",
        "GITHUB_TOKEN": "github-sentinel",
        "ACTIONS_RUNTIME_TOKEN": "actions-sentinel",
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("triage_action", "actions/triage-agent/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {
        "mode": "success",
        "calls": [],
        "output": output,
        "decision": decision,
        "module": module,
        "real_runner": getattr(module, "run_bounded", None),
    }
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if Path(command[0]).name == "yq":
            return real_run(command, **kwargs)
        if command[0] == "grep":
            state["calls"].append((command, kwargs))
            return subprocess.CompletedProcess(
                command, int(b"credential-value" not in kwargs["input"]), b"", b""
            )
        state["calls"].append((command, kwargs))
        prompt = command[-1]
        import re

        canary = re.search(r"Private canary: (\S+)", prompt)[1]
        data = copy.deepcopy(decision)
        data["notes"] = []
        mode = state["mode"]
        prefix = ""
        if mode == "key":
            prefix = "credential-value\n"
        if mode == "short_key":
            import os

            prefix = os.environ["GATEWAY_CREDENTIAL"] + "\n"
        if mode == "canary":
            prefix = canary + "\n"
        if mode == "other_key":
            prefix = state.get("credential", "unrelated-job-secret") + "\n"
        if mode == "other_escaped_key":
            data["rca"] = {
                "hypothesis": state["credential"],
                "locations": [],
                "confidence": "low",
                "confirm_with": "check",
            }
        if "leak" in state:
            leak = state["leak"](canary)
            if state["source"] == "raw":
                prefix = leak + "\n"
            else:
                data["rca"] = {
                    "hypothesis": leak,
                    "locations": [],
                    "confidence": "low",
                    "confirm_with": "check",
                }
        if mode == "run_id":
            data["run_id"] = "other"
        if mode == "base_sha":
            data["base_sha"] = "b" * 40
        if mode == "taxonomy":
            data["labels"] = ["type:invented"]
        if mode == "escapedkey":
            data["rca"] = {
                "hypothesis": "credential-value",
                "locations": [],
                "confidence": "low",
                "confirm_with": "check",
            }
        if mode == "environment":
            script = (
                "import os; "
                "assert all(k not in os.environ for k in "
                "['SENTINEL_JOB_SECRET','GITHUB_TOKEN','ACTIONS_RUNTIME_TOKEN','ROLE_KEY']); "
                "assert os.environ['LLM_API_KEY']=='credential-value'"
            )
            import sys

            child = real_run([sys.executable, "-c", script], env=kwargs["env"], capture_output=True)
            assert child.returncode == 0, "job secret inherited by child"
        encoded = json.dumps(data)
        if mode == "escapedkey":
            encoded = encoded.replace(
                "credential-value", "".join(chr(92) + f"u{ord(c):04x}" for c in "credential-value")
            )
        if mode == "other_escaped_key":
            encoded = encoded.replace(
                state["credential"],
                "".join(chr(92) + f"u{ord(c):04x}" for c in state["credential"]),
            )
        if state.get("source") == "decoded":
            encoded = encoded.replace(leak, "".join(chr(92) + f"u{ord(c):04x}" for c in leak))
        stdout = (prefix + "```json\n" + encoded + "\n```\n").encode()
        # Every adverse mode has otherwise schema-valid JSON.
        assert validate(data, load_schema("decision")) == []
        return subprocess.CompletedProcess(command, 7 if mode == "nonzero" else 0, stdout, b"")

    monkeypatch.setattr(module, "run_bounded", fake_run, raising=False)
    return state


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "key",
        "canary",
        "nonzero",
        "run_id",
        "base_sha",
        "escapedkey",
        "other_key",
        "taxonomy",
    ],
)
def test_action_targeted_failures_with_valid_json(action, mode, capsys):
    action["mode"] = mode
    rc = action["module"].main()
    assert rc == (0 if mode == "success" else 1)
    data = json.loads(action["output"].read_text())
    assert bool(data["escalate"]) == (mode != "success")
    assert data["answers"] == action["decision"]["answers"]
    assert validate(data, load_schema("decision")) == []
    captured = capsys.readouterr()
    assert "credential-value" not in captured.out + captured.err + action["output"].read_text()


@pytest.mark.parametrize("credential", ["credential-value", "unrelated-job-secret"])
@pytest.mark.parametrize("failure", [False, True])
def test_final_artifact_scans_preserved_tier1_credential(
    action, monkeypatch, failure, capsys, credential
):
    import os

    decision = copy.deepcopy(action["decision"])
    decision["notes"] = [credential]
    Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    action["output"].write_text("stale artifact")
    action["mode"] = "nonzero" if failure else "success"
    assert action["module"].main() == 1
    assert action["calls"]
    assert not action["output"].exists()
    captured = capsys.readouterr()
    assert "credential-value" not in captured.out + captured.err


def test_final_artifact_scans_preserved_canary(action, monkeypatch):
    import os

    monkeypatch.setattr(action["module"].secrets, "token_hex", lambda _: "private-canary")
    decision = copy.deepcopy(action["decision"])
    decision["notes"] = ["private-canary"]
    Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    assert action["module"].main() == 1
    assert not action["output"].exists()


def test_agent_child_has_only_allowed_environment(action):
    action["mode"] = "environment"
    assert action["module"].main() == 0


def test_explicit_short_key_with_nonmatching_name_is_scanned(action, monkeypatch, capsys):
    monkeypatch.setenv("API_KEY_ENV", "GATEWAY_CREDENTIAL")
    monkeypatch.setenv("GATEWAY_CREDENTIAL", "short-test")
    action["mode"] = "short_key"
    assert action["module"].main() == 1
    assert action["calls"]
    data = action["output"].read_text()
    assert json.loads(data)["escalate"]["to"] == "maintainer"
    captured = capsys.readouterr()
    assert "short-test" not in data + captured.out + captured.err


def test_timeout_has_kill_grace_and_no_key_in_argv(action):
    assert action["module"].main() == 0
    command = action["calls"][0][0]
    assert command[:4] == ["timeout", "-k", "15", "900"]
    assert all(
        "credential-value" not in argument for command, _ in action["calls"] for argument in command
    )


def test_oversize_prompt_escalates_without_spawning(action):
    import os

    Path(os.environ["ISSUE_FILE"]).write_text("x" * 140000)
    assert action["module"].main() == 1
    assert not action["calls"]
    assert json.loads(action["output"].read_text())["escalate"]["to"] == "maintainer"


@pytest.mark.parametrize(
    "context", [{"labels": "labels: [42]"}, {"areas": "areas: broken"}, {"labels": "labels: ["}]
)
def test_invalid_taxonomy_fails_closed(action, context):
    import os

    path = Path(os.environ["CONTEXT_FILE"])
    data = json.loads(path.read_text())
    data.update(context)
    path.write_text(json.dumps(data))
    assert action["module"].main() == 1
    assert not action["calls"]
    assert json.loads(action["output"].read_text())["escalate"]["to"] == "maintainer"


@pytest.mark.parametrize(
    "name",
    ["TOKENIZERS_PARALLELISM", "SSH_AUTH_SOCK", "KEYBOARD_LAYOUT", "SECRETARY", "PASSWORDLESS"],
)
def test_noncredential_names_do_not_trigger_scan(action, monkeypatch, name):
    monkeypatch.setenv(name, "false")
    assert action["module"].main() == 0
    assert json.loads(action["output"].read_text())["escalate"] is None


def test_action_fixture_excludes_ambient_credentials(monkeypatch, request):
    import os

    monkeypatch.setenv("AMBIENT_TOKEN", "false")
    action = request.getfixturevalue("action")
    assert "AMBIENT_TOKEN" not in os.environ
    assert action["module"].main() == 0


def test_runner_metadata_does_not_escalate(action, monkeypatch):
    import os

    for name, value in {
        "PATH": os.environ["PATH"],
        "SSH_AUTH_SOCK": "false",
        "SOME_RUNNER_AUTH": "1",
        "CACHE_KEY": "v1",
        "FOO_TOKEN": "x",
    }.items():
        monkeypatch.setenv(name, value)
    assert action["module"].main() == 0
    assert action["calls"]
    assert json.loads(action["output"].read_text())["escalate"] is None


@pytest.mark.parametrize(
    "name,credential",
    [
        ("GITHUB_TOKEN", "github-sentinel"),
        ("ACTIONS_RUNTIME_TOKEN", "run-sentinel"),
        ("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "oidc-sentinel"),
        ("OTHER_TOKEN", "other-token"),
        ("OTHER_KEY", "other-key"),
        ("OTHER_SECRET", "other-secret"),
        ("OTHER_PASSWORD", "other-password"),
    ],
)
@pytest.mark.parametrize("source", ["tier1", "raw", "decoded"])
def test_unrelated_short_credentials_cannot_escape(
    action, monkeypatch, capsys, name, credential, source
):
    import os

    monkeypatch.setenv(name, credential)
    action["output"].write_text("stale artifact")
    if source == "tier1":
        decision = copy.deepcopy(action["decision"])
        decision["notes"] = [credential]
        Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    else:
        action["credential"] = credential
        action["mode"] = "other_key" if source == "raw" else "other_escaped_key"
    assert action["module"].main() == 1
    assert action["calls"]
    text = action["output"].read_text() if action["output"].exists() else ""
    if text:
        assert json.loads(text)["escalate"]["to"] == "maintainer"
    captured = capsys.readouterr()
    assert credential not in text + captured.out + captured.err


def test_context_taxonomy_uses_shared_loader_and_unknown(action, tmp_path):
    context = {
        key: Path(f"tests/fixtures/agents/{key}.yml").read_text()
        for key in ["config", "labels", "areas"]
    }
    labels = action["module"].context_taxonomy(context, tmp_path)
    assert labels == {"type:bug", "type:feature", "area:core", "area:unknown"}
    context["areas"] = "areas: [{name: core}]"
    from agent_repo.config import ConfigError

    with pytest.raises(ConfigError):
        action["module"].context_taxonomy(context, tmp_path)


@pytest.mark.parametrize("source", ["raw", "decoded", "tier1"])
@pytest.mark.parametrize("subject", ["key", "canary"])
@pytest.mark.parametrize(
    "encoding",
    [
        "base64",
        "base64_unpadded",
        "urlsafe",
        "urlsafe_unpadded",
        "hex",
        "HEX",
        "percent",
        "reverse",
    ],
)
def test_derived_credentials_and_canary_cannot_escape(
    action, monkeypatch, capsys, source, subject, encoding
):
    import base64
    import os
    from urllib.parse import quote

    credential = "ab?ÿ" if subject == "key" else "canary?/ÿ"
    if subject == "key":
        monkeypatch.setenv("ROLE_KEY", credential)
    else:
        token_hex = action["module"].secrets.token_hex
        monkeypatch.setattr(
            action["module"].secrets, "token_hex", lambda n: credential if n == 24 else token_hex(n)
        )
    raw = credential.encode()
    forms = {
        "base64": base64.b64encode(raw).decode(),
        "base64_unpadded": base64.b64encode(raw).decode().rstrip("="),
        "urlsafe": base64.urlsafe_b64encode(raw).decode(),
        "urlsafe_unpadded": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "hex": raw.hex(),
        "HEX": raw.hex().upper(),
        "percent": quote(credential, safe=""),
        "reverse": credential[::-1],
    }
    leak = forms[encoding]
    if source == "tier1":
        decision = copy.deepcopy(action["decision"])
        decision["notes"] = [leak]
        Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    else:
        action.update(source=source, leak=lambda _: leak)
    assert action["module"].main() == 1
    assert action["calls"]
    text = action["output"].read_text() if action["output"].exists() else ""
    if text:
        assert json.loads(text)["escalate"]["to"] == "maintainer"
    captured = capsys.readouterr()
    assert leak not in text + captured.out + captured.err


@pytest.mark.parametrize("source", ["raw", "decoded", "tier1"])
@pytest.mark.parametrize(
    "name,value,leak",
    [
        ("GH_PAT", "pat-sentinel", "pat-sentinel"),
        ("NPM_AUTH", "auth-sentinel", "auth-sentinel"),
        ("SERVICE_CREDENTIALS", "credentials-sentinel", "credentials-sentinel"),
        ("PASSWD", "passwd-sentinel", "passwd-sentinel"),
        ("DB_PASSWD", "db-passwd-sentinel", "db-passwd-sentinel"),
        ("SERVICE_PASS", "pass-sentinel", "pass-sentinel"),
        ("SERVICE_PRIVATE", "private-sentinel", "private-sentinel"),
        ("SERVICE_DSN", "dsn-sentinel", "dsn-sentinel"),
        ("DATABASE_URL", DATABASE_URL, DATABASE_URL),
        ("DATABASE_URL", DATABASE_URL, "s3cr3t"),
        ("DATABASE_URL", urlunsplit(("postgresql", "alice:s3%63r3t@db", "", "", "")), "s3cr3t"),
    ],
)
def test_additional_credentials_are_redacted_and_scanned(
    action, monkeypatch, capsys, source, name, value, leak
):
    import os

    monkeypatch.setenv(name, value)
    Path(os.environ["ISSUE_FILE"]).write_text(leak)
    if source == "tier1":
        decision = copy.deepcopy(action["decision"])
        decision["notes"] = [leak]
        Path(os.environ["DECISION_FILE"]).write_text(json.dumps(decision))
    else:
        action.update(source=source, leak=lambda _: leak)
    assert action["module"].main() == 1
    assert action["calls"]
    assert leak not in action["calls"][0][0][-1]
    text = action["output"].read_text() if action["output"].exists() else ""
    if text:
        assert json.loads(text)["escalate"]["to"] == "maintainer"
    captured = capsys.readouterr()
    assert leak not in text + captured.out + captured.err


@pytest.mark.parametrize(
    "name,value",
    [("GH_PAT", "x"), ("DATABASE_URL", urlunsplit(("postgres", "a:x@db", "", "", "")))],
)
def test_incidental_short_credentials_do_not_escalate(action, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert action["module"].main() == 0
    assert action["calls"]
    assert json.loads(action["output"].read_text())["escalate"] is None


@pytest.mark.parametrize(
    "name",
    [
        "ROLE_KEY",
        "LLM_API_KEY",
        "GITHUB_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ],
)
def test_explicit_short_credentials_still_fail_closed(action, monkeypatch, name):
    monkeypatch.setenv(name, "x")
    assert action["module"].main() == 1
    assert not action["calls"]
    assert not action["output"].exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr", "timeout"])
def test_runaway_output_is_bounded_and_child_killed(action, monkeypatch, capsys, stream):
    import os
    import subprocess
    import sys
    import time

    module = action["module"]
    runner = action["real_runner"]
    monkeypatch.setattr(
        module,
        "run_bounded",
        lambda command, *, env, timeout: runner(
            command, env=env, timeout=0.2 if stream == "timeout" else 10
        ),
    )
    real_popen = subprocess.Popen
    children = []

    def flood(*args, **kwargs):
        if Path(args[0][0]).name == "yq":
            return real_popen(*args, **kwargs)
        fd = 1 if stream == "stdout" else 2
        code = f"import os,time; os.write({fd}, b'x' * (4 * 1024 * 1024)); time.sleep(10)"
        if stream == "timeout":
            code = "import time; time.sleep(10)"
        child = real_popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", flood)
    start = time.monotonic()
    assert module.main() == 1
    assert time.monotonic() - start < 5
    assert len(children) == 1 and children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(children[0].pid, 0)
    data = json.loads(action["output"].read_text())
    assert data["escalate"]["to"] == "maintainer"
    assert data["rca"] is None
    captured = capsys.readouterr()
    assert "x" * 100 not in captured.out + captured.err
