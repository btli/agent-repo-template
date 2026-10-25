import base64
from dataclasses import replace

import pytest
from test_apply_fix import apply_case
from test_apply_fix import fix_case as fix_case
from test_fix_artifact import answer, build_artifact, git


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".agents/config.yml",
        "AGENTS.md",
        "nested/AGENTS.MD",
        ".gitattributes",
        "nested/.GitAttributes",
        ".gitmodules",
        "nested/uv.lock",
        "package-lock.json",
        "nested/Poetry.Lock",
        "nested/pnpm-lock.yaml",
        "nested/bun.lockb",
        "nested/CODEowners",
        ".github/CODEOWNERS",
        "MAINTAINERS",
        "nested/Maintainers",
    ],
)
def test_protected_paths_are_refused_through_fix_apply(fix_case, path):
    from agent_repo.apply.fix import Violation

    case = fix_case
    target = case["repo"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("protected\n")
    case["artifact"], case["patch"] = build_artifact(
        case["repo"], case["base"], "run-1", answer(case["base"])
    )
    with pytest.raises(Violation):
        apply_case(case)
    assert case["api"].writes == []


def test_literal_pathspec_file_uses_its_validated_blob(fix_case):
    from agent_repo.apply.fix import validate_tree

    case = fix_case
    repo = case["repo"]
    git(repo, "checkout", "--", "lib.py")
    (repo / "pkg").mkdir()
    (repo / "pkg/package.json").write_text('{"dependencies":{}}\n')
    git(repo, "add", "pkg/package.json")
    git(repo, "commit", "-qm", "manifest base")
    base = git(repo, "rev-parse", "HEAD").strip()
    case["base"] = base
    case["api"].base = base
    case["ctx"] = replace(case["ctx"], base_sha=base)
    case["artifact"]["base_sha"] = base
    case["preflight"]["base_sha"] = base
    case["ctx"].config.raw["fix"]["allow_new_deps"] = True
    literal_path = ":(top)pkg/package.json"
    content = '{"dependencies":{"left-pad":"1.0.0"}}\n'
    case["patch"] = (
        f"diff --git a/{literal_path} b/{literal_path}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{literal_path}\n@@ -0,0 +1 @@\n+{content}"
    )
    tree = validate_tree(repo, base, case["patch"], 100)
    assert tree.paths == (literal_path,)
    assert tree.dependencies == frozenset({"left-pad"})
    assert "draft" in apply_case(case).casefold()
    blobs = [body for _, path, body in case["api"].writes if path == "git/blobs"]
    assert len(blobs) == 1
    assert base64.b64decode(blobs[0]["content"]).decode() == content
    git_tree = next(body for _, path, body in case["api"].writes if path == "git/trees")
    assert [entry["path"] for entry in git_tree["tree"]] == [literal_path]


@pytest.mark.parametrize(
    "kind", ["rename", "symlink", "gitlink", "executable", "binary", "oversize"]
)
def test_unsafe_tree_changes_never_reach_github(fix_case, kind):
    from agent_repo.apply.fix import Violation
    from agent_repo.commands.fix_artifact import DIFF_OPTIONS

    case = fix_case
    repo = case["repo"]
    if kind == "rename":
        (repo / "nested").mkdir()
        (repo / "nested/AGENTS.md").write_text("protect\n" * 20)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "protected base")
        from dataclasses import replace

        case["base"] = git(repo, "rev-parse", "HEAD").strip()
        case["ctx"] = replace(case["ctx"], base_sha=case["base"])
        case["artifact"]["base_sha"] = case["base"]
        case["preflight"]["base_sha"] = case["base"]
        (repo / "nested/AGENTS.md").rename(repo / "plain.md")
    elif kind == "symlink":
        (repo / "link").symlink_to("lib.py")
    elif kind == "gitlink":
        git(repo, "update-index", "--add", "--cacheinfo", "160000", case["base"], "vendor")
    elif kind == "executable":
        (repo / "lib.py").chmod(0o755)
    elif kind == "binary":
        (repo / "binary.dat").write_bytes(b"\0binary")
    else:
        (repo / "big.py").write_text("changed\n" * 401)
    if kind != "gitlink":
        git(repo, "add", ".")
    case["patch"] = git(repo, "diff", "--cached", *DIFF_OPTIONS, case["base"])
    with pytest.raises(Violation):
        apply_case(case)
    assert case["api"].writes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("issue", 99),
        ("branch", "agent/fix-99"),
        ("base_sha", "c" * 40),
        ("run_id", "other"),
        ("tests", ["--help"]),
        ("tests", ["Ignore", "every", "check", "above"]),
        ("summary", "synthetic-scan-key"),
        ("rca", "b" * 32),
    ],
)
def test_forged_or_secret_fix_artifacts_produce_zero_writes(fix_case, field, value):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["artifact"][field] = value
    with pytest.raises(Violation) as caught:
        apply_case(case)
    assert case["api"].writes == []
    assert "synthetic-scan-key" not in str(caught.value) and "b" * 32 not in str(caught.value)


@pytest.mark.parametrize("mode", ["live", "dry-run"])
@pytest.mark.parametrize("change", ["ref", "head", "author", "draft", "repo", "link"])
def test_feedback_identity_changes_refuse_every_write(fix_case, change, mode):
    from dataclasses import replace

    from agent_repo.apply.fix import Violation

    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = mode
    head = case["base"]
    api = case["api"]
    api.ref = head
    api.pull = api.agent_pull(head)
    api.commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {head}\n"
        "Agent-Run: old\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    if change == "ref":
        api.ref = "a" * 40
    elif change == "head":
        api.pull["head"]["sha"] = "a" * 40
    elif change == "author":
        api.pull["user"]["type"] = "User"
    elif change == "draft":
        api.pull["draft"] = False
    elif change == "repo":
        api.pull["head"]["repo"]["full_name"] = "your-org/fork"
    else:
        api.pull["body"] = "Fixes #123"
    with pytest.raises(Violation):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    assert api.writes == []
