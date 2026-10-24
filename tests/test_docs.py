import ast
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

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


def test_document_links_resolve():
    missing = [path.relative_to(ROOT) for path in DOCS if not path.is_file()]
    assert not missing, f"missing documentation pages: {missing}"

    markdown_files = [path for path in ROOT.rglob("*.md") if ".git" not in path.parts]
    problems = []
    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", text):
            raw_target = match.group(1).split(maxsplit=1)[0].strip("<>")
            if raw_target.startswith(("http://", "https://", "mailto:", "#")):
                if raw_target.startswith("#"):
                    target, fragment = source, unquote(raw_target[1:])
                else:
                    continue
            else:
                target_text, separator, fragment = raw_target.partition("#")
                target = (source.parent / unquote(target_text)).resolve()
                fragment = unquote(fragment) if separator else ""
            if not target.is_file():
                problems.append(f"{source.relative_to(ROOT)}: missing {raw_target}")
                continue
            if fragment and target.suffix == ".md":
                headings = {
                    _slug(match.group(1))
                    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", target.read_text(), re.M)
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
    source_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for base in (ROOT / "lib", ROOT / "actions", ROOT / "consumer")
        for path in base.rglob("*")
        if path.is_file() and path.suffix in {".py", ".yml", ".yaml", ".sh"}
    )
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

    config = load_yaml(ROOT / "consumer/.agents/config.yml")
    labels = load_yaml(ROOT / "consumer/.agents/labels.yml")
    areas = load_yaml(ROOT / "consumer/.agents/areas.yml")
    documented_paths = {
        "mode",
        "thresholds",
        "limits",
        "commands",
        "labels",
        "areas",
        "models.triage",
        "models.review",
        "models.fix",
        "system_one.provider",
        "system_one.model",
        "close.enabled",
        "fix.accept_ci_exposure",
        "commands.setup",
        "commands.test",
        "commands.lint",
    }
    for path in documented_paths:
        assert f"`{path}`" in documented_docs, f"config key not documented: {path}"
        if path.startswith("labels"):
            value = labels
        elif path.startswith("areas"):
            value = areas
        else:
            value = config
        for part in path.split("."):
            assert isinstance(value, dict) and part in value, f"missing config path: {path}"
            value = value[part]
    assert "`sensitive`" in documented_docs
    assert any("sensitive" in item for item in areas["areas"])

    apparmor_command = "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    preflight = (ROOT / "actions/fix-agent/preflight.sh").read_text(encoding="utf-8")
    assert apparmor_command in preflight
    assert f"`{apparmor_command}`" in documented_docs


def _gate_reasons() -> set[str]:
    paths = (ROOT / "lib/agent_repo/gate.py", ROOT / "lib/agent_repo/commands/gate.py")
    reasons = {f"api-error-{status}" for status in (401, 403, 429)} | {"api-error"}
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
        for match in re.finditer(r"\"(tier2-(?:global|author)-cap)\"", path.read_text()):
            reasons.add(match.group(1))
    return reasons


def test_operations_catalog_covers_gate_reasons_and_fixed_messages():
    operations = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    missing_reasons = sorted(
        reason for reason in _gate_reasons() if f"`{reason}`" not in operations
    )
    assert not missing_reasons, f"gate reasons missing from operations catalog: {missing_reasons}"

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
