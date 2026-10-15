import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_OWNERS = {
    "your-org",
    "actions",
    "astral-sh",
    "step-security",
    "zizmorcore",
    "rhysd",
    "mikefarah",
    "omnigent-ai",
}
ALLOWED_BTLI_REPOSITORIES = {"agent-repo-template", "agent-repo-template-sandbox"}
GITHUB_REPOSITORY_PATTERN = r"(?<!api\.)github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"


def _unexpected_github_repositories(text: str) -> list[tuple[str, str]]:
    repositories = re.findall(GITHUB_REPOSITORY_PATTERN, text)
    return [
        (owner, repo)
        for owner, repo in repositories
        if owner not in ALLOWED_OWNERS
        and not (owner == "btli" and repo in ALLOWED_BTLI_REPOSITORIES)
    ]


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
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".venv" in path.parts
            or "__pycache__" in path.parts
        ):
            continue
        text = path.read_text(errors="ignore")
        unexpected = _unexpected_github_repositories(text)
        assert not unexpected, (path, unexpected)


def test_btli_repository_allowlist_is_limited():
    assert _unexpected_github_repositories("https://github.com/btli/agent-repo-template") == []
    sandbox_repository = "/".join(("https://github.com", "btli", "agent-repo-template-sandbox"))
    assert _unexpected_github_repositories(sandbox_repository) == []
    other_repository = "/".join(("https://github.com", "btli", "other-repo"))
    assert _unexpected_github_repositories(other_repository) == [("btli", "other-repo")]


def test_actions_pinned_to_sha():
    assert _valid_action_reference("uses: ./actions/check-config")
    assert not _valid_action_reference("uses: external/action@v1")
    for folder in (ROOT / ".github", ROOT / "actions"):
        if not folder.exists():
            continue
        for pattern in ("*.yml", "*.yaml"):
            for path in folder.rglob(pattern):
                for line in path.read_text().splitlines():
                    if "uses:" not in line:
                        continue
                    assert _valid_action_reference(line), (path, line)


def _valid_action_reference(line: str) -> bool:
    match = re.search(r"uses:\s+(\S+)", line)
    if match is None:
        return False
    reference = match.group(1)
    if reference.startswith("./"):
        return "@" not in reference
    return re.search(r"uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\d+\.\d+\.\d+", line) is not None
