import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from agent_repo.bootstrap import ruleset_body
from agent_repo.charter import lint_charter, parse_charter
from agent_repo.yamljson import load_yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "README.md",
    ROOT / "docs/getting-started.md",
    ROOT / "docs/charter-guide.md",
    ROOT / "docs/gateway.md",
    ROOT / "docs/github-app.md",
    ROOT / "docs/security.md",
    ROOT / "docs/operations.md",
)


def _slug(heading: str) -> str:
    return re.sub(r"[^\w-]", "", re.sub(r"\s+", "-", heading.strip().lower()))


def _code_spans(text: str) -> list[str]:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.findall(r"`([^`]+)`", text)


def _key_paths(value: object, prefix: str = "") -> set[str]:
    paths = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(key)
            paths.add(path)
            paths.update(_key_paths(nested, path))
    elif isinstance(value, list):
        for nested in value:
            paths.update(_key_paths(nested, prefix))
    return paths


def _schema_paths(schema: dict, prefix: str = "") -> set[str]:
    paths = set()
    for key, nested in schema.get("properties", {}).items():
        path = f"{prefix}.{key}" if prefix else key
        paths.update({key, path})
        paths.update(_schema_paths(nested, path))
    return paths


def test_document_links_resolve():
    missing = [path.relative_to(ROOT) for path in DOCS if not path.is_file()]
    assert not missing, f"missing documentation pages: {missing}"

    markdown_files = [path for path in ROOT.rglob("*.md") if ".git" not in path.parts]
    problems = []
    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", text):
            raw_target = match.group(1).split(maxsplit=1)[0].strip("<>")
            if raw_target.startswith(("http://", "https://", "mailto:")):
                continue
            target_text, _, fragment = raw_target.partition("#")
            target = (source.parent / unquote(target_text)).resolve() if target_text else source
            fragment = unquote(fragment)
            if not target.is_file():
                problems.append(f"{source.relative_to(ROOT)}: missing {raw_target}")
                continue
            if fragment and target.suffix == ".md":
                headings = {
                    _slug(title.group(1))
                    for title in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", target.read_text(), re.M)
                }
                if fragment not in headings:
                    problems.append(f"{source.relative_to(ROOT)}: missing anchor {raw_target}")
    assert not problems, "unresolved Markdown links:\n" + "\n".join(problems)


def test_charter_guide_links_and_canonical_charter_are_valid():
    charter_path = ROOT / "evals/charter.md"
    charter_text = charter_path.read_text(encoding="utf-8")
    assert lint_charter(charter_text) == []
    assert len(parse_charter(charter_text).docs_index) >= 1
    assert len(re.findall(r"^##\s+Docs index\s*$", charter_text, re.M)) == 1

    guide = (ROOT / "docs/charter-guide.md").read_text(encoding="utf-8")
    for target in (
        "../evals/charter.md",
        "../consumer/.agents/config.yml",
        "../consumer/.agents/labels.yml",
        "../consumer/.agents/areas.yml",
    ):
        assert f"]({target})" in guide
    assert "last" in guide.lower() and "first" in guide.lower()


def test_documented_bootstrap_options_and_config_keys_exist():
    documented_docs = "\n".join(path.read_text(encoding="utf-8") for path in DOCS)
    bootstrap_help = subprocess.run(
        [sys.executable, "lib/agent_repo_cli.py", "bootstrap", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    app_link_help = subprocess.run(
        [sys.executable, "lib/agent_repo_cli.py", "bootstrap", "app-link", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    documented_commands = re.findall(r"^\s*(\./bootstrap\.sh[^\n]*)$", documented_docs, re.M)
    documented_commands += re.findall(r"`(\./bootstrap\.sh[^`]+)`", documented_docs)
    assert documented_commands
    source_paths = [
        path.read_text(encoding="utf-8", errors="replace")
        for base in (ROOT / "lib", ROOT / "actions", ROOT / "consumer")
        for path in base.rglob("*")
        if path.is_file() and path.suffix in {".py", ".yml", ".yaml", ".sh"}
    ]
    source_text = "\n".join(source_paths)
    code_spans = _code_spans(documented_docs)
    documented_flags = {
        f"--{flag}" for span in code_spans for flag in re.findall(r"--([a-z][a-z0-9-]*)", span)
    }
    supported_flags = set(re.findall(r"--[a-z][a-z0-9-]*", bootstrap_help))
    app_link_flags = set(re.findall(r"--[a-z][a-z0-9-]*", app_link_help))
    assert documented_flags <= supported_flags | app_link_flags
    for command in documented_commands:
        for option in re.findall(r"--[a-z][a-z0-9-]*", command):
            help_text = app_link_help if " app-link " in f" {command} " else bootstrap_help
            assert option in help_text, f"undocumented bootstrap option: {option}"
        if " app-link " in f" {command} ":
            assert "bootstrap.sh app-link" in app_link_help

    mentioned_keys = set(re.findall(r"\bAGENT_[A-Z0-9_]+\b", documented_docs))
    missing_keys = sorted(
        key for key in mentioned_keys if not re.search(rf"\b{key}\b", source_text)
    )
    assert not missing_keys, f"documented config keys missing from merged source: {missing_keys}"

    schema = json.loads((ROOT / "schemas/config.schema.json").read_text())
    config = load_yaml(ROOT / "consumer/.agents/config.yml")
    labels = load_yaml(ROOT / "consumer/.agents/labels.yml")
    areas = load_yaml(ROOT / "consumer/.agents/areas.yml")
    known_keys = _schema_paths(schema)
    known_keys.update(_key_paths(config))
    known_keys.update(_key_paths(labels))
    known_keys.update(_key_paths(areas))
    key_references = {
        span
        for span in code_spans
        if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", span)
        and not span.endswith((".yml", ".yaml", ".json"))
    }
    config_roots = set(schema["properties"]) | set(config) | set(labels) | set(areas)
    documented_keys = {
        span for span in key_references if "." in span and span.split(".", 1)[0] in config_roots
    }
    for line in documented_docs.splitlines():
        lower = line.lower()
        if "config.yml has" in lower or (".yml" in lower and " key" in lower):
            documented_keys.update(
                span
                for span in _code_spans(line)
                if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", span)
                and not span.endswith((".yml", ".yaml", ".json", ".py"))
            )
        if "config's" in lower or "config flag" in lower:
            documented_keys.update(
                span for span in _code_spans(line) if "." in span and span[0].islower()
            )
    assert documented_keys <= known_keys, (
        f"unknown documented config keys: {documented_keys - known_keys}"
    )
    environments = set()
    for path in (ROOT / "consumer/.github/workflows").glob("*.yml"):
        workflow = load_yaml(path)
        for job in workflow.get("jobs", {}).values():
            if isinstance(job, dict) and isinstance(job.get("environment"), str):
                environments.add(job["environment"])
    documented_environments = {
        span
        for span in code_spans
        if re.fullmatch(r"agent-[a-z0-9-]+", span) and span not in _gate_reasons()
    }
    assert documented_environments <= environments

    known_labels = set(labels["labels"])
    documented_labels = {
        span
        for span in code_spans
        if re.fullmatch(
            r"(?:type|area):[a-z][a-z-]*|P[0-3]|needs-(?:info|maintainer)|agent:[a-z-]+", span
        )
    }
    assert documented_labels <= known_labels, (
        f"unknown documented labels: {documented_labels - known_labels}"
    )

    bootstrap = ast.parse((ROOT / "lib/agent_repo/bootstrap.py").read_text())
    gateway_kinds = next(
        ast.literal_eval(node.value)
        for node in bootstrap.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "GATEWAY_KINDS" for target in node.targets
        )
    )
    gateway_doc = (ROOT / "docs/gateway.md").read_text()
    kind_line = next(line for line in gateway_doc.splitlines() if "AGENT_GATEWAY_KIND" in line)
    documented_kinds = {
        span for span in _code_spans(kind_line) if re.fullmatch(r"[a-z][a-z-]*", span)
    }
    assert documented_kinds == set(gateway_kinds)
    provider_enum = set(schema["properties"]["system_one"]["properties"]["provider"]["enum"])
    provider_section = gateway_doc.split("## System One and LLM-only", 1)[1].split(
        "Removing `system_one`", 1
    )[0]
    documented_providers = {
        span for span in _code_spans(provider_section) if re.fullmatch(r"[a-z][a-z-]*", span)
    }
    assert documented_providers == provider_enum

    apparmor_command = "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    preflight = (ROOT / "actions/fix-agent/preflight.sh").read_text(encoding="utf-8")
    assert apparmor_command in preflight
    assert f"`{apparmor_command}`" in documented_docs


def _gate_reasons() -> set[str]:
    paths = (
        ROOT / "lib/agent_repo/gate.py",
        ROOT / "lib/agent_repo/commands/gate.py",
        ROOT / "lib/agent_repo/decide.py",
    )
    reasons = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in {"result", "GateResult"} and len(node.args) > 1:
                reason = node.args[1]
                if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                    reasons.add(reason.value)
            if node.func.id == "GateResult":
                for keyword in node.keywords:
                    if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
                        reasons.add(keyword.value.value)
        if path.name == "gate.py":
            reasons.update(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"tier2-(?:global|author)-cap", node.value)
            )
        if path.name == "gate.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "_DISTINCT_API_ERRORS"
                    for target in node.targets
                ):
                    reasons.update(f"api-error-{status}" for status in ast.literal_eval(node.value))
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.IfExp)
                    and any(
                        isinstance(target, ast.Name) and target.id == "reason"
                        for target in node.targets
                    )
                ):
                    reasons.update(
                        item.value
                        for item in ast.walk(node.value)
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and item.value == "api-error"
                    )
        if path.name == "decide.py":
            reasons.update(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"tier2-[a-z0-9]+(?:-[a-z0-9]+)*", node.value)
            )
    return reasons


def test_operations_catalog_covers_gate_reasons_and_fixed_messages():
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    catalog = operations.split("## Gate reasons and fixed messages", 1)[1].split(
        "Fixed command-wrapper messages:", 1
    )[0]
    documented_reasons = {
        reason
        for row in re.findall(r"^\| ([^|]+) \|", catalog, re.M)
        for reason in _code_spans(row)
    }
    unknown_reasons = sorted(documented_reasons - _gate_reasons())
    assert not unknown_reasons, f"unknown gate or decide reasons: {unknown_reasons}"
    missing_reasons = sorted(_gate_reasons() - documented_reasons)
    assert not missing_reasons, f"gate or decide reasons missing from catalog: {missing_reasons}"
    assert {"ready", "agent-off", "tier2-global-cap", "tier2-author-cap"} <= documented_reasons

    gate_source = (ROOT / "lib/agent_repo/commands/gate.py").read_text(encoding="utf-8")
    for message in (
        "Gate input could not be loaded.",
        "Gate outputs could not be written.",
        "Gate summary could not be written.",
    ):
        assert message in gate_source
        assert f"`{message}`" in operations
    assert "Fix was not run: {reason}." in gate_source
    assert "`Fix was not run: {reason}.`" in operations
    fix_source = (ROOT / "lib/agent_repo/apply/fix.py").read_text(encoding="utf-8")
    shared_app_message = "UNSAFE ON SHARED APP: patch retained; no push."
    assert shared_app_message in fix_source
    assert f"`{shared_app_message}`" in operations


def test_gateway_budget_and_llm_only_docs_match_merged_behavior():
    gateway = " ".join((ROOT / "docs/gateway.md").read_text(encoding="utf-8").lower().split())
    assert "per-key token and rate limits" in gateway
    assert "80%" in gateway
    assert "does not send a workflow run id" in gateway
    assert "per-run gateway limits" in gateway
    assert "does not ingest gateway budget alerts" in gateway
    assert "never automatically closes" in gateway
    assert "auto-close stays off" in gateway
    assert "security-uncertain" in gateway
    assert "maintainer" in gateway


def _run_true_gate_reasons() -> set[str]:
    tree = ast.parse((ROOT / "lib/agent_repo/gate.py").read_text(encoding="utf-8"))
    dynamic_reasons = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Tuple):
            continue
        names = [item.id if isinstance(item, ast.Name) else None for item in node.target.elts]
        if "reason" not in names or not isinstance(node.iter, ast.Tuple):
            continue
        reason_index = names.index("reason")
        for item in node.iter.elts:
            if isinstance(item, ast.Tuple) and len(item.elts) > reason_index:
                value = item.elts[reason_index]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    dynamic_reasons.add(value.value)

    reasons = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "result" or len(node.args) < 2:
            continue
        if not isinstance(node.args[0], ast.Constant) or node.args[0].value is not True:
            continue
        reason = node.args[1]
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            reasons.add(reason.value)
        elif isinstance(reason, ast.Name) and reason.id == "reason":
            reasons.update(dynamic_reasons)
    return reasons


def test_documented_main_contracts_match_merged_sources():
    operations = " ".join((ROOT / "docs/operations.md").read_text(encoding="utf-8").split())
    true_reason_sentence = operations.split("Gate's `run` output is true for ", 1)[1].split(
        ". On a Tier-2 cap", 1
    )[0]
    documented_true_reasons = set(_code_spans(true_reason_sentence))
    assert documented_true_reasons == _run_true_gate_reasons()

    ruleset_doc = (
        (ROOT / "docs/github-app.md")
        .read_text(encoding="utf-8")
        .split("## Branch and tag rulesets", 1)[1]
    )
    branch_doc, tag_and_rest = ruleset_doc.split("The tag ruleset", 1)
    tag_doc = tag_and_rest.split("\n\n", 1)[0]
    rule_words = {
        "creation": "creation",
        "update": "updates",
        "deletion": "deletion",
        "non_fast_forward": "non-fast-forward",
    }
    for target, section in (("branch", branch_doc), ("tag", tag_doc)):
        actual = {item["type"] for item in ruleset_body(target)["rules"]}
        documented = {rule for rule, word in rule_words.items() if word in section}
        assert documented == actual

    versioned_artifacts = {
        f"{path.name.removesuffix('.schema.json')}.json"
        for path in (ROOT / "schemas").glob("*.schema.json")
        if "schema_version" in json.loads(path.read_text(encoding="utf-8")).get("required", [])
    }
    gate_command = (ROOT / "lib/agent_repo/commands/gate.py").read_text(encoding="utf-8")
    if '"canary.json"' in gate_command and '"schema_version"' in gate_command:
        versioned_artifacts.add("canary.json")
    artifact_sentence = operations.split("The versioned JSON artifacts are ", 1)[1].split(
        "; each carries", 1
    )[0]
    assert set(_code_spans(artifact_sentence)) == versioned_artifacts
    assert "`triage-context.json` is unversioned" in operations

    gateway = " ".join((ROOT / "docs/gateway.md").read_text(encoding="utf-8").split())
    apply_source = (ROOT / "lib/agent_repo/apply/triage.py").read_text(encoding="utf-8")
    apply_tree = ast.parse(apply_source)
    close_guard = next(
        node
        for node in apply_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_can_close"
    )
    close_guard_source = ast.get_source_segment(apply_source, close_guard)
    assert "is_llm_only(ctx.config)" in close_guard_source
    assert "LLM-only mode never automatically closes an issue" in gateway
    assert "`close.enabled` cannot enable an automatic close" in gateway

    doctor_source = (ROOT / "lib/agent_repo/doctor.py").read_text(encoding="utf-8")
    doctor_tree = ast.parse(doctor_source)
    doctor_constants = {}
    for node in doctor_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if names and names[0] in {"UNSAFE_SHARED_APP_CONFIRMED", "UNSAFE_SHARED_APP_ASSUMED"}:
            doctor_constants[names[0]] = "".join(
                item.value
                for item in ast.walk(node.value)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    security_raw = (ROOT / "docs/security.md").read_text(encoding="utf-8")
    security = " ".join(security_raw.split())
    shared_app = security.split("## Shared-App warning", 1)[1].split("## ", 1)[0]
    assert "repository_selection=all" in doctor_constants["UNSAFE_SHARED_APP_CONFIRMED"]
    assert "repository_selection=all" in shared_app and "**confirmed** only" in shared_app
    assert "assumed to reach other repositories" in doctor_constants["UNSAFE_SHARED_APP_ASSUMED"]
    assert "**assumed** in every other case" in shared_app

    system_one_row = next(
        line for line in security_raw.splitlines() if line.startswith("| `AGENT_SYSTEM_ONE_KEY`")
    )
    assert "add its value to `AGENT_SCAN_KEYS`" in system_one_row
    assert "Rotate the key and scanner entry together." in system_one_row

    fix_notes = operations.split("## Fix workflow notes", 1)[1].split("## Dependency updates", 1)[0]
    assert "fix-agent propose step refuses persisted credential settings" in fix_notes
    assert "fix applier protects `.gitmodules`" in fix_notes
    assert fix_notes.count("persist-credentials: false") == 1
    assert fix_notes.count("commands.setup") == 1
    assert "rerun bootstrap with `--ref" not in operations
    assert "update each installed action SHA and version comment together" in operations


def test_security_role_key_detection_does_not_claim_run_id():
    security = (ROOT / "docs/security.md").read_text(encoding="utf-8")
    role_key_row = next(
        line
        for line in security.splitlines()
        if line.startswith("| `AGENT_TRIAGE_KEY`, `AGENT_REVIEW_KEY`")
    )
    assert "Gateway usage by role key" in role_key_row
    assert "matching role's Actions runs because requests carry no run ID" in role_key_row
    assert "by role and run id" not in role_key_row.lower()
    gateway = " ".join((ROOT / "docs/gateway.md").read_text(encoding="utf-8").split())
    assert "does not send a workflow run id to the gateway" in gateway
