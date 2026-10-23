import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from agent_repo.apply.actions import ApplyContext
from agent_repo.charter import parse_charter
from agent_repo.config import load_config
from agent_repo.event import load_event
from agent_repo.github import GitHubError
from test_fix_artifact import answer, build_artifact, git, make_repo


class FixAPI:
    def __init__(self, base):
        self.base = base
        self.tree = "e" * 40
        self.ref = None
        self.pull = None
        self.writes = []
        self.reads = []
        self.commits = {}
        self.ref_reads = 0
        self.move_ref_at = None
        self.move_at = None
        self.tree_reads = 0
        self.conflict = False

    def paginate(self, path, params=None):
        self.reads.append(path)
        if path == "pulls":
            return [self.pull] if self.pull else []
        return []

    def request(self, method, path, *, body=None, params=None, **kwargs):
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
            if path == "git/refs":
                if self.conflict:
                    raise GitHubError(422)
                self.ref = body["sha"]
            elif path.startswith("git/refs/"):
                assert body["force"] is False
                self.ref = body["sha"]
            elif path == "git/commits":
                self.commits["f" * 40] = body
                return {"sha": "f" * 40}
            elif path == "pulls":
                self.pull = self.agent_pull(self.ref)
                return self.pull
            return {"sha": "a" * 40}
        self.reads.append(path)
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path == "contents/.github/workflows":
            self.tree_reads += 1
            return {
                "type": "dir",
                "sha": ("d" * 40 if self.move_at == self.tree_reads else self.tree),
            }
        if path.startswith("git/ref/heads/agent"):
            if not self.ref:
                raise GitHubError(404)
            return {"object": {"sha": self.ref}}
        if path.startswith("commits/"):
            return {"author": {"type": "Bot", "login": "helper[bot]"}}
        if path.startswith("git/commits/"):
            return {
                "sha": path.rsplit("/", 1)[1],
                "message": self.commits[path.rsplit("/", 1)[1]]["message"],
            }
        if path.startswith("pulls/"):
            return self.pull
        if path == "issues/12":
            return {"labels": [], "state": "open"}
        raise AssertionError(path)

    def agent_pull(self, head):
        return {
            "number": 24,
            "draft": True,
            "state": "open",
            "body": "Fixes #12",
            "user": {"login": "helper[bot]", "type": "Bot"},
            "head": {"ref": "agent/fix-12", "sha": head, "repo": {"full_name": "your-org/project"}},
            "base": {"ref": "main"},
            "html_url": "https://example.invalid/pull/24",
        }


@pytest.fixture
def fix_case(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    artifact, patch = build_artifact(repo, base, "run-1", answer(base))
    event_path = tmp_path / "event.json"
    data = json.loads(Path("tests/fixtures/events/issue_comment.created.issue.json").read_text())
    data["comment"]["body"] = "/fix"
    data["issue"]["number"] = 12
    event_path.write_text(json.dumps(data))
    config = load_config(Path("tests/fixtures/agents"))
    config.raw["mode"]["fix"] = "live"
    client = FixAPI(base)
    ctx = ApplyContext(
        load_event("issue_comment", event_path),
        config,
        parse_charter(Path("tests/fixtures/charter/example.md").read_text()),
        "run-1",
        base,
        ["synthetic-scan-key"],
        "b" * 32,
        False,
        github=client,
        bot_login="helper[bot]",
    )
    preflight = {
        "schema_version": "1",
        "run_id": "run-1",
        "base_sha": base,
        "workflow_tree_sha": client.tree,
        "exposures": [],
        "accepted_template_exposures": [],
        "pass": True,
    }
    return {
        "repo": repo,
        "base": base,
        "ctx": ctx,
        "artifact": artifact,
        "patch": patch,
        "preflight": preflight,
        "api": client,
        "event_path": event_path,
    }


def apply_case(case, **kwargs):
    from agent_repo.apply.fix import apply_fix

    return apply_fix(
        case["artifact"],
        case["patch"],
        case["repo"],
        case["ctx"],
        case["preflight"],
        issue=12,
        **kwargs,
    )


def test_fix_creates_draft_with_exact_feedback_compatible_commit(fix_case):
    import importlib.util

    from agent_repo.apply.fix import branch_for

    case = fix_case
    result = apply_case(case)
    assert "draft" in result.casefold()
    assert branch_for(12) == "agent/fix-12"
    writes = case["api"].writes
    commit = next(body for _, path, body in writes if path == "git/commits")
    assert commit["parents"] == [case["base"]]
    assert (
        commit["message"] == f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: run-1\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    spec = importlib.util.spec_from_file_location("fix_agent_readonly", "actions/fix-agent/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.AGENT_COMMIT.fullmatch(commit["message"])
    pull = next(body for _, path, body in writes if path == "pulls")
    assert pull["draft"] is True and "Fixes #12" in pull["body"]
    assert "```" in pull["body"] and pull["head"] == branch_for(12)
    assert any(
        path == "issues/24/labels" and "agent:fix" in body["labels"] for _, path, body in writes
    )


@pytest.mark.parametrize("mode", ["dry-run", "live"])
def test_no_patch_is_scanned_bounded_and_never_pushes(fix_case, mode):
    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = mode
    case["artifact"].update(
        status="no_patch", attempted="Could not reproduce. " * 100, summary="No patch"
    )
    case["patch"] = None
    result = apply_case(case)
    assert "no patch" in result.casefold()
    if mode == "dry-run":
        assert "Could not reproduce." in result
        assert "```" in result and len(result) < 5000
    assert all(path == "issues/12/comments" for _, path, _ in case["api"].writes)
    assert len(case["api"].writes) == (1 if mode == "live" else 0)


@pytest.mark.parametrize(
    "reason", ["dry-run", "unsafe", "shared-app", "moved-before-push", "moved-before-pr"]
)
def test_fix_does_not_push_when_policy_or_workflow_tree_refuses(fix_case, reason):
    case = fix_case
    if reason == "dry-run":
        case["ctx"].config.raw["mode"]["fix"] = "dry-run"
    elif reason in {"unsafe", "shared-app"}:
        case["preflight"]["pass"] = False
        case["preflight"]["exposures"] = [
            {
                "workflow": "ci.yml",
                "job": "test",
                "rule": "secrets",
                "detail": "UNSAFE ON SHARED APP" if reason == "shared-app" else "exposed",
                "secrets": ["ROLE_SECRET"],
            }
        ]
        if reason == "shared-app":
            case["ctx"].config.raw["fix"]["accept_ci_exposure"] = True
    else:
        case["api"].move_at = 1 if reason == "moved-before-push" else 2
    result = apply_case(case)
    if reason != "moved-before-pr":
        assert not any(
            path in {"git/refs", "git/commits", "pulls"} for _, path, _ in case["api"].writes
        )
    else:
        assert not any(path == "pulls" for _, path, _ in case["api"].writes)
    assert any(word in result.casefold() for word in ["dry-run", "unsafe", "moved"])


def test_exposure_bypass_names_secrets_in_body_and_summary(fix_case):
    case = fix_case
    case["ctx"].config.raw["fix"]["accept_ci_exposure"] = True
    case["preflight"].update(pass_=False)
    case["preflight"].pop("pass_")
    case["preflight"]["pass"] = False
    case["preflight"]["exposures"] = [
        {
            "workflow": "ci.yml",
            "job": "test",
            "rule": "secrets",
            "detail": "exposed",
            "secrets": ["ROLE_SECRET"],
        }
    ]
    result = apply_case(case)
    body = next(body for _, path, body in case["api"].writes if path == "pulls")["body"]
    assert "ROLE_SECRET" in result and "ROLE_SECRET" in body


def test_feedback_patch_is_cumulative_but_commit_is_child_of_captured_head(fix_case):
    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: old\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {"message": message}
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    assert "updated" in apply_case(case, feedback_head=head, feedback_ref="agent/fix-12").casefold()
    commit = next(body for _, path, body in case["api"].writes if path == "git/commits")
    assert commit["parents"] == [head]
    assert not any(path == "git/refs" for _, path, _ in case["api"].writes)
    assert any(
        path == "git/refs/heads/agent/fix-12" and body["force"] is False
        for _, path, body in case["api"].writes
    )


@pytest.mark.parametrize("existing_pr", [False, True])
def test_existing_bot_ref_recovers_missing_pr_or_links_existing_draft(fix_case, existing_pr):
    case = fix_case
    case["api"].ref = "c" * 40
    case["api"].commits["c" * 40] = {
        "message": f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    if existing_pr:
        case["api"].pull = case["api"].agent_pull(case["api"].ref)
    result = apply_case(case)
    assert not any(path in {"git/refs", "git/commits"} for _, path, _ in case["api"].writes)
    assert sum(path == "pulls" for _, path, _ in case["api"].writes) == (0 if existing_pr else 1)
    assert "draft" in result.casefold()


@pytest.mark.parametrize("feedback", [False, True])
@pytest.mark.parametrize("allow,change", [(False, "new"), (True, "new"), (False, "version")])
def test_manifest_changes_gate_new_dependencies_and_require_maintainer(
    fix_case, allow, change, feedback
):
    from agent_repo.apply.fix import Violation

    case = fix_case
    repo = case["repo"]
    git(repo, "checkout", "--", "lib.py")
    (repo / "package.json").write_text('{"dependencies":{"old":"1"}}')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "manifest base")
    base = git(repo, "rev-parse", "HEAD").strip()
    case["base"] = base
    case["ctx"] = replace(case["ctx"], base_sha=base)
    case["api"].base = base
    (repo / "package.json").write_text(
        '{"dependencies":{"old":"2"' + (',"new":"1"' if change == "new" else "") + "}}"
    )
    case["artifact"], case["patch"] = build_artifact(repo, base, "run-1", answer(base))
    case["preflight"]["base_sha"] = base
    case["ctx"].config.raw["fix"]["allow_new_deps"] = allow
    arguments = {}
    if feedback:
        git(repo, "add", ".")
        message = (
            f"Fix #12\n\nAgent-Base: {base}\nAgent-Run: prior\n"
            "Agent-Tests: tests/test_lib.py::test_add\n"
        )
        git(repo, "commit", "-qm", message)
        head = git(repo, "rev-parse", "HEAD").strip()
        case["api"].ref = head
        case["api"].pull = case["api"].agent_pull(head)
        case["api"].commits[head] = {"message": message}
        case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
        arguments = {"feedback_head": head, "feedback_ref": "agent/fix-12"}
    if change == "new" and not allow:
        with pytest.raises(Violation):
            apply_case(case, **arguments)
        assert case["api"].writes == []
    else:
        apply_case(case, **arguments)
        assert any(
            path == "issues/24/labels" and "needs-maintainer" in body["labels"]
            for _, path, body in case["api"].writes
        )


def test_apply_command_uses_event_policy_and_trusted_context(fix_case, tmp_path, monkeypatch):
    from agent_repo.commands import apply_fix

    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = "dry-run"
    artifact = tmp_path / "fix.json"
    patch = tmp_path / "fix.patch"
    context = tmp_path / "canary.json"
    artifact.write_text(json.dumps(case["artifact"]))
    patch.write_text(case["patch"])
    context.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": "run-1",
                "base_sha": case["base"],
                "canary": case["ctx"].canary,
            }
        )
    )
    monkeypatch.setattr(apply_fix, "GitHub", lambda *args: case["api"])
    policy_reads = []

    def policy(gh, sha, directory):
        policy_reads.append(sha)
        return sha, case["ctx"].config, case["ctx"].charter

    monkeypatch.setattr(apply_fix, "_policy", policy)
    monkeypatch.setattr(apply_fix, "_preflight", lambda *args: case["preflight"])
    for key, value in {
        "GH_TOKEN": "synthetic-token",
        "AGENT_SCAN_KEYS": "synthetic-scan-key",
        "AGENT_BOT_LOGIN": "helper",
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(case["event_path"]),
        "GITHUB_SHA": case["base"],
        "GITHUB_RUN_ID": "run-1",
        "FIX_ISSUE": "12",
        "FIX_BASE": case["base"],
        "FIX_HEAD": "",
        "FIX_REF": "agent/fix-12",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }.items():
        monkeypatch.setenv(key, value)
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 0
    assert policy_reads == [case["base"]]
    assert "dry-run" in (tmp_path / "summary").read_text()
    assert case["api"].writes == []
    trusted = json.loads(context.read_text())
    for key, value in {
        "schema_version": "other",
        "run_id": "other",
        "base_sha": "c" * 40,
        "canary": "malformed",
    }.items():
        context.write_text(json.dumps({**trusted, key: value}))
        assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 1
        assert case["api"].writes == []
    context.unlink()
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 1
    assert case["api"].writes == []


@pytest.mark.parametrize("phase", [2, 3])
def test_workflow_tree_is_rechecked_at_each_publication(fix_case, phase):
    case = fix_case
    case["api"].move_at = phase
    result = apply_case(case)
    assert "moved" in result
    if phase == 2:
        assert not any(path == "git/refs" for _, path, _ in case["api"].writes)
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)


def test_ref_creation_conflict_links_existing_draft(fix_case):
    case = fix_case
    case["api"].conflict = True
    case["api"].pull = case["api"].agent_pull("c" * 40)
    assert case["api"].pull["html_url"] in apply_case(case)
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)


def test_existing_ref_must_have_verified_bot_authorship(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["api"].ref = "c" * 40
    case["api"].commits["c" * 40] = {
        "message": f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    original = case["api"].request

    def request(method, path, **kwargs):
        if method == "GET" and path.startswith("commits/"):
            return {"author": {"type": "User", "login": "helper[bot]"}}
        return original(method, path, **kwargs)

    case["api"].request = request
    with pytest.raises(Violation):
        apply_case(case)
    assert case["api"].writes == []


def test_area_owner_review_includes_users_and_repository_teams(fix_case):
    from agent_repo.config import Area

    case = fix_case
    case["ctx"] = replace(
        case["ctx"],
        config=replace(
            case["ctx"].config,
            areas=(
                Area("core", "Core", ("lib.py",), ("@maintainer", "@your-org/core-team"), False),
            ),
        ),
    )
    apply_case(case)
    request = next(
        body for _, path, body in case["api"].writes if path == "pulls/24/requested_reviewers"
    )
    assert request == {"reviewers": ["maintainer"], "team_reviewers": ["core-team"]}
