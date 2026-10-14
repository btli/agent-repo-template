import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_OWNERS = {
    "btli",
    "your-org",
    "actions",
    "astral-sh",
    "step-security",
    "zizmorcore",
    "rhysd",
    "mikefarah",
    "omnigent-ai",
}


def test_lib_imports_stdlib_only():
    package = ROOT / "lib" / "agent_repo"
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            assert set(names) <= sys.stdlib_module_names | {"agent_repo"}, (path, names)


def test_no_unexpected_github_owners():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        for owner in re.findall(r"(?<!api\.)github\.com/([A-Za-z0-9_.-]+)", text):
            assert owner in ALLOWED_OWNERS, (path, owner)


def test_actions_pinned_to_sha():
    for folder in (ROOT / ".github", ROOT / "actions"):
        if not folder.exists():
            continue
        for pattern in ("*.yml", "*.yaml"):
            for path in folder.rglob(pattern):
                for line in path.read_text().splitlines():
                    if "uses:" not in line:
                        continue
                    assert re.search(r"uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+", line), (
                        path,
                        line,
                    )
