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
        ("lib.py", "x", "y", set()),
    ],
)
def test_dependencies_used_by_fix_manifest_gate(path, before, after, expected):
    from agent_repo.deps import new_dependencies

    assert new_dependencies(path, before, after) == expected
