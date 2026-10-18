import pytest
from agent_repo.config import Area, RepoConfig
from agent_repo.diff import _parse_diff, diff_index, summary_only


def test_diff_index_includes_added_and_context_lines_but_not_removed_lines():
    diff = """diff --git a/src/file.py b/src/file.py
index 1111111..2222222 100644
--- a/src/file.py
+++ b/src/file.py
@@ -2,3 +2,3 @@
 context
-removed
+added
 context-after
"""

    assert diff_index(diff) == {"src/file.py": {2, 3, 4}}


def test_diff_index_parses_crlf_lines():
    diff = (
        "diff --git a/src/file.py b/src/file.py\r\n"
        "--- a/src/file.py\r\n"
        "+++ b/src/file.py\r\n"
        "@@ -0,0 +1 @@\r\n"
        "+added\r\n"
    )

    assert diff_index(diff) == {"src/file.py": {1}}


def test_diff_index_uses_renamed_path():
    diff = """diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -1 +1 @@
-old
+new
"""

    assert diff_index(diff) == {"new.py": {1}}


def test_diff_index_does_not_parse_added_header_like_rows_between_hunks():
    diff = """diff --git a/src/real.py b/src/real.py
--- a/src/real.py
+++ b/src/real.py
@@ -0,0 +1 @@
+++ b/src/fake.py
@@ -10,0 +12 @@
+real content
"""

    assert diff_index(diff) == {"src/real.py": {1, 12}}


def test_diff_index_decodes_quoted_paths():
    diff = """diff --git a/src/quoted name.py b/src/quoted name.py
--- "a/src/quoted\\040name.py"
+++ "b/src/quoted\\040name.py"
@@ -0,0 +1 @@
+added
"""

    assert diff_index(diff) == {"src/quoted name.py": {1}}


def test_summary_only_for_large_diff():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = "".join(f"+line {line}\n" for line in range(1501))

    summary_only_result, reason = summary_only(diff, config)

    assert summary_only_result is True
    assert "1500" in reason


def test_summary_only_for_sensitive_area():
    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}},
        (Area("security", "Secrets", (".github/**",), (), True),),
        (),
    )

    diff = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-old
+new
"""
    summary_only_result, reason = summary_only(diff, config)

    assert summary_only_result is True
    assert "needs-maintainer" in reason


def test_summary_only_sensitive_deletion_uses_old_path():
    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}},
        (Area("security", "Secrets", (".github/**",), (), True),),
        (),
    )
    diff = """diff --git a/.github/secrets.yml b/.github/secrets.yml
deleted file mode 100644
--- a/.github/secrets.yml
+++ /dev/null
"""

    summary_only_result, reason = summary_only(diff, config)

    assert summary_only_result is True
    assert "needs-maintainer" in reason


def test_summary_only_detects_sensitive_path_with_spaces():
    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}},
        (Area("ci", "CI", (".github/**",), (), True),),
        (),
    )
    diff = """diff --git a/.github/workflows/a b.yml b/.github/workflows/a b.yml
--- a/.github/workflows/a b.yml\t
+++ b/.github/workflows/a b.yml\t
@@ -1 +1 @@
-old
+new
"""

    assert diff_index(diff) == {".github/workflows/a b.yml": {1}}

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason


@pytest.mark.parametrize("separator", ["\x0c", "\u2028", "\x1c", "\x85"])
@pytest.mark.parametrize("new_file", [False, True])
def test_summary_only_keeps_separator_injection_in_sensitive_workflow(separator, new_file):
    config = RepoConfig(
        {"limits": {"review_max_lines": 1500}},
        (Area("security", "CI", (".github/**",), (), True),),
        (),
    )
    path = ".github/workflows/new.yml" if new_file else ".github/workflows/ci.yml"
    old_path = "/dev/null" if new_file else f"a/{path}"
    new_path = f"b/{path}"
    hunk = "@@ -0,0 +1 @@\n" if new_file else "@@ -1 +1 @@\n-old\n"
    diff = (
        f"diff --git a/{path} b/{path}\n--- {old_path}\n+++ {new_path}\n"
        f"{hunk}+y{separator}--- /dev/null{separator}+++ b/src/ok.py\n"
    )

    assert diff_index(diff) == {path: {1}}

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_counts_lines_using_newline_only():
    config = RepoConfig({"limits": {"review_max_lines": 5}}, (), ())
    diff = (
        "diff --git a/src/file.py b/src/file.py\n--- a/src/file.py\n"
        "+++ b/src/file.py\n@@ -0,0 +1 @@\n+first\u2028second\n"
    )

    limited, reason = summary_only(diff, config)

    assert limited is False
    assert reason == ""


@pytest.mark.parametrize("header", ["--- a/src/other.py", "+++ b/src/other.py"])
@pytest.mark.parametrize("after_hunk", [False, True])
def test_summary_only_fails_closed_for_repeated_or_late_file_headers(header, after_hunk):
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = "diff --git a/src/file.py b/src/file.py\n--- a/src/file.py\n+++ b/src/file.py\n"
    if after_hunk:
        diff += f"@@ -0,0 +1 @@\n+added\n{header}\n"
    else:
        diff += f"{header}\n@@ -0,0 +1 @@\n+added\n"

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_ignores_first_added_header_after_hunk():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = """diff --git a/src/file.py b/src/file.py
--- a/src/file.py
@@ -0,0 +1 @@
+added
+++ b/src/late.py
"""

    limited, reason = summary_only(diff, config)
    _, paths, _ = _parse_diff(diff)

    assert limited is True
    assert "needs-maintainer" in reason
    assert paths == {"src/file.py"}


def test_summary_only_ignores_first_removed_header_after_hunk():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = """diff --git a/src/file.py b/src/file.py
+++ b/src/file.py
@@ -0,0 +1 @@
+added
--- a/src/late.py
"""

    limited, reason = summary_only(diff, config)
    _, paths, _ = _parse_diff(diff)

    assert limited is True
    assert "needs-maintainer" in reason
    assert paths == {"src/file.py"}


def test_summary_only_fails_closed_for_garbage_inside_hunk():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = """diff --git a/src/file.py b/src/file.py
--- a/src/file.py
+++ b/src/file.py
@@ -0,0 +1,2 @@
+valid row
garbage row
"""

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_fails_closed_for_ambiguous_path_header():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    diff = """diff --git a/src/file.py b/src/file.py
--- "a/src/file.py
+++ b/src/file.py
"""

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason


def test_summary_only_for_diff_over_prompt_byte_budget():
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())
    rows = ["+" + "x" * 200 for _ in range(1000)]
    diff = "diff --git a/src/large.py b/src/large.py\n--- a/src/large.py\n+++ b/src/large.py\n"
    diff += "@@ -0,0 +1,1000 @@\n" + "\n".join(rows) + "\n"

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "byte" in reason.lower()


def test_diff_index_keeps_modified_file_when_neighbor_file_is_deleted():
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -1 +0,0 @@
-removed
"""
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())

    assert diff_index(diff) == {"src/app.py": {1}}
    assert summary_only(diff, config) == (False, "")


def test_summary_only_fails_closed_for_truncated_hunk():
    diff = """diff --git a/src/file.py b/src/file.py
--- a/src/file.py
+++ b/src/file.py
@@ -1,2 +1,2 @@
-old
+new
"""
    config = RepoConfig({"limits": {"review_max_lines": 1500}}, (), ())

    limited, reason = summary_only(diff, config)

    assert limited is True
    assert "needs-maintainer" in reason
