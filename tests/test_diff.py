import json
from pathlib import Path

import pytest
from agent_repo.config import Area, RepoConfig
from agent_repo.diff import diff_index, summary_only


def _file(patch_text, **overrides):
    entry = {
        "filename": "src/file.py",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": patch_text,
    }
    entry.update(overrides)
    return entry


def _config(maximum=1500, areas=()):
    return RepoConfig({"limits": {"review_max_lines": maximum}}, tuple(areas), ())


def test_diff_index_includes_added_and_context_lines_but_not_removed_lines():
    files = [
        _file(
            "@@ -2,3 +2,3 @@\n context\n-removed\n+added\n context-after",
            additions=1,
            deletions=1,
        )
    ]

    assert diff_index(files) == {"src/file.py": {2, 3, 4}}


def test_diff_index_accepts_multiple_hunks_and_no_newline_marker():
    patch = (
        "@@ -1 +1 @@ function name\n-old\n+new\n\\ No newline at end of file\n@@ -5,0 +6 @@\n+last"
    )

    assert diff_index([_file(patch, additions=2, deletions=1, changes=3)]) == {
        "src/file.py": {1, 6}
    }


@pytest.mark.parametrize(
    "patch",
    [
        "@@ -1, +1 @@\n-old\n+new",
        "@@ -1,3 +1,3 @@\n-old\n+new",
        "@@ -1 +1 @@\n-old\n+new\ngarbage",
        "@@ -1 +1 @@\n-old\n+new\n\\ evil",
    ],
    ids=["malformed-hunk-header", "short-hunk", "stray-row", "wrong-marker"],
)
def test_malformed_file_patch_fails_closed(patch):
    files = [_file(patch)]

    assert diff_index(files) == {}
    limited, reason = summary_only(files, _config())
    assert limited is True
    assert "needs-maintainer" in reason


@pytest.mark.parametrize(
    "entry",
    [
        _file("@@ -1 +1 @@\n-old\n+new", status="mystery"),
        _file("@@ -1 +1 @@\n-old\n+new", status=[]),
        _file("@@ -1 +1 @@\n-old\n+new", status="renamed"),
        _file("@@ -1 +1 @@\n-old\n+new", additions=True),
        _file("@@ -1 +1 @@\n-old\n+new", changes=3),
        _file("@@ -1 +1 @@\n-old\n+new", patch=3),
    ],
    ids=[
        "unknown-status",
        "nonstr-status",
        "rename-without-previous-filename",
        "boolean-count",
        "count-mismatch",
        "nontext-patch",
    ],
)
def test_invalid_file_entries_fail_closed(entry):
    files = [entry]

    assert diff_index(files) == {}
    limited, reason = summary_only(files, _config())
    assert limited is True
    assert "needs-maintainer" in reason


def test_file_with_changes_and_missing_patch_is_summary_only():
    entry = _file(None)
    entry.pop("patch")

    assert diff_index([entry]) == {}
    limited, reason = summary_only([entry], _config())
    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_counts_additions_and_deletions():
    patch = "@@ -1,751 +1,750 @@\n" + "-old\n" * 751 + "+new\n" * 750
    file = _file(patch, additions=750, deletions=751, changes=1501)

    limited, reason = summary_only([file], _config())

    assert limited is True
    assert "1501" in reason


def test_summary_only_matches_both_current_and_previous_paths():
    sensitive = Area("workflows", "Workflow", (".github/**",), (), True)
    renamed = _file(
        "@@ -1 +1 @@\n-old\n+new",
        filename="src/workflow.yml",
        previous_filename=".github/workflows/ci.yml",
        status="renamed",
    )

    limited, reason = summary_only([renamed], _config(areas=(sensitive,)))

    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_rejects_more_than_api_file_limit():
    files = [
        _file(None, filename=f"src/file-{number}.py", additions=0, deletions=0, changes=0)
        for number in range(3001)
    ]
    for entry in files:
        entry.pop("patch")

    limited, reason = summary_only(files, _config())

    assert limited is True
    assert "API limit" in reason


def test_summary_only_rejects_files_over_prompt_budget():
    patch = "@@ -1 +1 @@\n " + "x" * (120 * 1024)
    entry = _file(patch, additions=0, deletions=0, changes=0)

    limited, reason = summary_only([entry], _config())

    assert limited is True
    assert "prompt budget" in reason


def test_real_github_files_response_validates_and_indexes_patches():
    pages = json.loads(Path("tests/fixtures/github/pr-10-files-pages.json").read_text())
    files = [entry for page in pages for entry in page]

    index = diff_index(files)

    assert len(files) == 18
    assert {entry["status"] for entry in files} >= {"added", "modified"}
    assert index["lib/agent_repo/diff.py"]
    assert index["actions/gate/action.yml"] == set(range(35, 44))
    assert all(isinstance(line, int) and line > 0 for lines in index.values() for line in lines)
    limited, reason = summary_only(files, _config())
    assert limited is True
    assert "review_max_lines" in reason
