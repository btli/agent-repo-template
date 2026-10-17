from agent_repo.config import Area, RepoConfig
from agent_repo.diff import diff_index, summary_only


def test_diff_index_includes_added_and_context_lines_but_not_removed_lines():
    diff = """diff --git a/src/file.py b/src/file.py
index 1111111..2222222 100644
--- a/src/file.py
+++ b/src/file.py
@@ -2,3 +2,4 @@
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
