import pytest


@pytest.mark.parametrize(
    "path,before,after,expected",
    [
        ("requirements-dev.txt", "pytest==7\n", "pytest==8\nrequests>=2\n", {"requests"}),
        (
            "nested/requirements.txt",
            "",
            "-r private.txt\n",
            {"nested/requirements.txt: unparsed change"},
        ),
        (
            "pyproject.toml",
            '[project]\ndependencies=["a"]',
            '[project]\ndependencies=["a", "b"]'
            '\n[project.optional-dependencies]\ndev=["c"]\n[dependency-groups]\ntest=["d"]',
            {"b", "c", "d"},
        ),
        (
            "package.json",
            '{"dependencies":{"a":"1"}}',
            '{"dependencies":{"a":"2"},"devDependencies":{"b":"1"},"optionalDependencies":{"c":"1"},"peerDependencies":{"d":"1"}}',
            {"b", "c", "d"},
        ),
        ("Cargo.toml", "", '[dependencies]\na="1"', {"Cargo.toml: unparsed change"}),
        ("package.json", "{}", "invalid", {"package.json: unparsed change"}),
        ("pyproject.toml", "", "invalid", {"pyproject.toml: unparsed change"}),
        ("requirements.txt", "a\n", "", set()),
        (
            "requirements.txt",
            "https://example.invalid/old.whl\n",
            "https://example.invalid/new.whl\n",
            {"requirements.txt: unparsed change"},
        ),
        (
            "requirements.txt",
            "git+https://example.invalid/old.git\n",
            "git+https://example.invalid/new.git\n",
            {"requirements.txt: unparsed change"},
        ),
        ("build.sbt", "", 'libraryDependencies += "a" % "b" % "1"', {"build.sbt: unparsed change"}),
        ("deep/constraints-ci.txt", "a==1\n", "a==1\nb==2\n", {"b"}),
        ("requirements/base.txt", "a\n", "a\nb\n", {"b"}),
        ("docs/notes.txt", "a\n", "a\nb\n", set()),
        (
            "pyproject.toml",
            '[project]\ndependencies=["a"]\n[tool.uv.sources]\na={path="x"}',
            '[project]\ndependencies=["a"]\n[tool.uv.sources]\na={git="https://example.invalid"}',
            {"pyproject.toml: unparsed change"},
        ),
        (
            "pyproject.toml",
            '[project]\ndependencies=["a"]\n[tool.ruff]\nline-length=99',
            '[project]\ndependencies=["a"]\n[tool.ruff]\nline-length=100',
            set(),
        ),
        ("lib.py", "x", "y", set()),
    ],
)
def test_dependencies_used_by_fix_manifest_gate(path, before, after, expected):
    from agent_repo.deps import new_dependencies

    assert new_dependencies(path, before, after) == expected
