from agent_repo.config import Area, RepoConfig
from agent_repo.diff import diff_index, summary_only


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
