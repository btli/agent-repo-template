import ast
import re
import sys
from pathlib import Path

import pytest
from agent_repo.yamljson import load_yaml

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
GITHUB_REPOSITORY_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_.-])(?:www\.)?github\.com[:/]"
        r"([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.-])api\.github\.com/repos/"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.-])raw\.githubusercontent\.com/"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    ),
)


def _unexpected_github_repositories(text: str) -> list[tuple[str, str]]:
    repositories = [
        pair for pattern in GITHUB_REPOSITORY_PATTERNS for pair in pattern.findall(text)
    ]
    return [
        (owner, repo)
        for owner, repo in repositories
        if owner.lower() not in ALLOWED_OWNERS
        and not (owner.lower() == "btli" and repo.removesuffix(".git") in ALLOWED_BTLI_REPOSITORIES)
    ]


def _action_files(root: Path) -> list[Path]:
    workflow_dir = root / ".github" / "workflows"
    workflows = [path for pattern in ("*.yml", "*.yaml") for path in workflow_dir.glob(pattern)]
    composites = list(root.rglob("action.yml")) + list(root.rglob("action.yaml"))
    return workflows + composites


def _uses_values(document: object, is_composite: bool) -> list[object]:
    if not isinstance(document, dict):
        return []
    references = []
    if is_composite:
        runs = document.get("runs")
        jobs = (
            {"action": runs} if isinstance(runs, dict) and runs.get("using") == "composite" else {}
        )
    else:
        jobs = document.get("jobs")
        jobs = jobs if isinstance(jobs, dict) else {}
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if "uses" in job:
            references.append(job["uses"])
        steps = job.get("steps")
        if isinstance(steps, list):
            references.extend(
                step["uses"] for step in steps if isinstance(step, dict) and "uses" in step
            )
    return references


def _parsed_action_references(root: Path) -> list[tuple[Path, object, str]]:
    references = []
    for path in _action_files(root):
        document = load_yaml(path)
        text = path.read_text()
        references.extend(
            (path, reference, text)
            for reference in _uses_values(document, path.name.startswith("action."))
        )
    return references


def _action_owner(reference: object) -> tuple[str, str] | None:
    if not isinstance(reference, str) or reference.startswith(("./", "docker://")):
        return None
    match = re.match(
        r"([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)(?:/|@)",
        reference,
    )
    return (match.group(1), match.group(2)) if match else None


def _has_version_comment(text: str, reference: str) -> bool:
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = re.search(
            r"""(?:^|[\s,{-])["']?uses["']?\s*:\s*["']?([^,'"}\s]+)""",
            line,
        )
        if match is None or match.group(1) != reference:
            continue
        suffix = line[match.end() :].lstrip()
        if suffix[:1] in {"'", '"'}:
            suffix = suffix[1:].lstrip()
        if suffix.startswith("}"):
            suffix = suffix[1:].lstrip()
        if re.match(r"#\s+v\d+\.\d+\.\d+\b", suffix):
            return True
    return False


def _valid_action_reference(reference: object, text: str) -> bool:
    if not isinstance(reference, str):
        return False
    if reference.startswith("./"):
        return "@" not in reference
    if reference.startswith("docker://"):
        return False
    return re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[^@]+)?@[0-9a-fA-F]{40}",
        reference,
    ) is not None and _has_version_comment(text, reference)


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
        if not path.is_file():
            continue
        relative_parts = path.relative_to(ROOT).parts
        parent_parts = relative_parts[:-1]
        if path.suffix in {".pyc", ".pyo"} or any(
            part in {".git", ".venv", ".tox", "__pycache__", ".cache"} or part.endswith("_cache")
            for part in parent_parts
        ):
            continue
        text = path.read_text(errors="ignore")
        unexpected = _unexpected_github_repositories(text)
        assert not unexpected, (path, unexpected)
    for path, reference, _ in _parsed_action_references(ROOT):
        owner = _action_owner(reference)
        if owner is None:
            continue
        name, repository = owner
        if name.lower() in ALLOWED_OWNERS or (
            name.lower() == "btli" and repository in ALLOWED_BTLI_REPOSITORIES
        ):
            continue
        raise AssertionError((path, [(name, repository)]))


def test_btli_repository_allowlist_is_limited():
    assert _unexpected_github_repositories("https://github.com/btli/agent-repo-template") == []
    sandbox_repository = "/".join(("https://github.com", "btli", "agent-repo-template-sandbox"))
    assert _unexpected_github_repositories(sandbox_repository) == []
    other_repository = "/".join(("https://github.com", "btli", "other-repo"))
    assert _unexpected_github_repositories(other_repository) == [("btli", "other-repo")]


def test_github_owner_scanner_ignores_documentation_host_paths():
    assert _unexpected_github_repositories("https://docs.github.com/en/actions/learn") == []


def test_github_owner_scanner_catches_bare_owner_links():
    owner_link = "https://github.com/" + "acme-corp"
    assert _unexpected_github_repositories(owner_link) == [("acme-corp", "")]


@pytest.mark.parametrize(
    "repository",
    [
        "https://" + "www.github.com/" + "evil/repo",
        "https://GitHub.com/" + "evil/repo",
        "git@github.com:" + "evil/repo",
        "https://api.github.com/repos/" + "evil/repo",
        "https://raw.githubusercontent.com/" + "evil/repo/main",
    ],
)
def test_github_owner_scanner_catches_host_forms(repository: str):
    assert _unexpected_github_repositories(repository) == [("evil", "repo")]


@pytest.mark.parametrize(
    "reference",
    [
        '      - "uses": untrusted/action@0123456789abcdef0123456789abcdef01234567',
        "      - 'uses': untrusted/action@0123456789abcdef0123456789abcdef01234567",
        "      - uses : untrusted/action@0123456789abcdef0123456789abcdef01234567",
        "      - {uses: untrusted/action@0123456789abcdef0123456789abcdef01234567}",
    ],
)
def test_github_owner_scanner_checks_uses_references(tmp_path: Path, monkeypatch, reference: str):
    root = tmp_path / "workflow"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(f"jobs:\n  checks:\n    steps:\n{reference}\n")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(AssertionError):
        test_no_unexpected_github_owners()
    allowed = reference.replace("untrusted/action", "actions/checkout")
    (workflow_dir / "ci.yml").write_text(f"jobs:\n  checks:\n    steps:\n{allowed}\n")
    test_no_unexpected_github_owners()


@pytest.mark.parametrize("composite", [False, True])
def test_owner_gate_checks_reusable_and_composite_references(
    tmp_path: Path, monkeypatch, composite: bool
):
    root = tmp_path / "reusable"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    if composite:
        action_dir = root / "actions" / "example"
        action_dir.mkdir(parents=True)
        (action_dir / "action.yml").write_text(
            "runs:\n  using: composite\n  steps:\n"
            '    - "uses": untrusted/action@'
            "0123456789abcdef0123456789abcdef01234567\n"
        )
    else:
        (workflow_dir / "ci.yml").write_text(
            "jobs:\n  reusable:\n    uses : "
            "untrusted/workflow@0123456789abcdef0123456789abcdef01234567\n"
        )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(AssertionError):
        test_no_unexpected_github_owners()


@pytest.mark.parametrize(
    "key_value",
    [
        "uses:",
        '"uses":',
        "'uses':",
        "uses :",
        "{uses:",
    ],
)
def test_action_sha_gate_validates_yaml_scalar(tmp_path: Path, monkeypatch, key_value: str):
    root = tmp_path / "workflow"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    pin = "0123456789abcdef0123456789abcdef01234567"
    if key_value == "uses:":
        allowed = f"      - uses: actions/checkout@{pin} # v1.0.0"
        unpinned = f"      - uses: actions/checkout@v4 # uses: actions/checkout@{pin} # v4.2.2"
    elif key_value == "{uses:":
        allowed = f"      - {{uses: actions/checkout@{pin}}} # v1.0.0"
        unpinned = "      - {uses: actions/checkout@v4}"
    else:
        key_prefix = f"      - {key_value} "
        allowed = f"{key_prefix}actions/checkout@{pin} # v1.0.0"
        unpinned = f"{key_prefix}actions/checkout@v4"
        if key_value == "uses :":
            allowed = f"      - uses : actions/checkout@{pin} # v1.0.0"
            unpinned = "      - uses : actions/checkout@v4"
    if key_value == "uses :":
        unpinned += f" # uses: actions/checkout@{pin} # v4.2.2"
    (workflow_dir / "ci.yml").write_text(f"jobs:\n  checks:\n    steps:\n{allowed}\n")
    test_actions_pinned_to_sha()
    (workflow_dir / "ci.yml").write_text(f"jobs:\n  checks:\n    steps:\n{unpinned}\n")
    with pytest.raises(AssertionError):
        test_actions_pinned_to_sha()


@pytest.mark.parametrize("reference", ["docker://alpine:latest", "docker://alpine"])
def test_action_sha_gate_rejects_mutable_docker_references(
    tmp_path: Path, monkeypatch, reference: str
):
    root = tmp_path / "workflow"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        f"jobs:\n  checks:\n    steps:\n      - uses: {reference}\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(AssertionError):
        test_actions_pinned_to_sha()


def test_action_sha_gate_does_not_use_a_separate_comment_for_pin(tmp_path: Path, monkeypatch):
    root = tmp_path / "workflow"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    pin = "0123456789abcdef0123456789abcdef01234567"
    (workflow_dir / "ci.yml").write_text(
        "jobs:\n  checks:\n    steps:\n"
        f"      - uses: actions/checkout@{pin}\n"
        f"        # uses: actions/checkout@{pin} # v1.0.0\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(AssertionError):
        test_actions_pinned_to_sha()


@pytest.mark.parametrize("composite", [False, True])
def test_pin_gate_checks_reusable_and_composite_references(
    tmp_path: Path, monkeypatch, composite: bool
):
    root = tmp_path / "repo"
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    if composite:
        action_dir = root / "actions" / "example"
        action_dir.mkdir(parents=True)
        (action_dir / "action.yml").write_text(
            'runs:\n  using: composite\n  steps:\n    - "uses": actions/checkout@v4\n'
        )
    else:
        (workflow_dir / "ci.yml").write_text(
            'jobs:\n  reusable:\n    "uses": actions/workflow@v1\n'
        )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    with pytest.raises(AssertionError):
        test_actions_pinned_to_sha()


def test_owner_scan_ignores_generated_caches(tmp_path: Path, monkeypatch):
    sentinel = "https://" + "github.com/" + "untrusted/generated"
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    for cache_name in ("__pycache__", ".pytest_cache", ".ruff_cache", ".cache"):
        cache = tmp_path / cache_name
        cache.mkdir()
        (cache / "generated.txt").write_text(sentinel)
    (tmp_path / "generated.pyc").write_bytes(sentinel.encode())
    test_no_unexpected_github_owners()


def test_actions_pinned_to_sha():
    assert _valid_action_reference("./actions/check-config", "")
    assert not _valid_action_reference("external/action@v1", "")
    references = _parsed_action_references(ROOT)
    assert references
    for path, reference, text in references:
        assert _valid_action_reference(reference, text), (path, reference)
