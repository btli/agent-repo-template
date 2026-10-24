import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from agent_repo.apply.actions import ApplyContext
from agent_repo.charter import parse_charter
from agent_repo.config import Area, load_config
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
        self.ref_move_on_read = None
        self.labels = []
        self.requested_users = []
        self.requested_teams = []
        self.expected_tree = None
        self.tree_response = None
        self.installation = {
            "repository_selection": "all",
            "total_count": 2,
            "repositories": [{"full_name": "your-org/project"}, {"full_name": "your-org/other"}],
        }

    def paginate(self, path, params=None):
        self.reads.append(path)
        if path == "pulls":
            return [self.pull] if self.pull else []
        if path == "issues/24/labels":
            return [{"name": label} for label in self.labels]
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
                if self.pull:
                    self.pull["head"]["sha"] = body["sha"]
            elif path == "git/commits":
                self.commits["f" * 40] = body
                return {"sha": "f" * 40}
            elif path == "git/trees":
                return {"sha": self.tree_response or self.expected_tree()}
            elif path == "pulls":
                self.pull = self.agent_pull(self.ref)
                return self.pull
            elif path == "issues/24/labels":
                self.labels = list(dict.fromkeys(self.labels + body["labels"]))
            elif path == "pulls/24/requested_reviewers":
                self.requested_users = list(dict.fromkeys(self.requested_users + body["reviewers"]))
                self.requested_teams = list(
                    dict.fromkeys(self.requested_teams + body["team_reviewers"])
                )
            return {"sha": "a" * 40}
        self.reads.append(path)
        if path == "/installation/repositories":
            assert self.writes == []
            if isinstance(self.installation, Exception):
                raise self.installation
            return self.installation
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path == "contents/.github/workflows":
            self.tree_reads += 1
            return {
                "type": "dir",
                "sha": ("d" * 40 if self.move_at == self.tree_reads else self.tree),
            }
        if path.startswith("git/ref/heads/agent"):
            self.ref_reads += 1
            if self.ref_move_on_read == self.ref_reads:
                self.ref = "a" * 40
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
            if path == "pulls/24/requested_reviewers":
                return {
                    "users": [{"login": user} for user in self.requested_users],
                    "teams": [{"slug": team} for team in self.requested_teams],
                }
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
    from agent_repo.apply.fix import apply_fix, validate_tree

    case["api"].expected_tree = lambda: (
        validate_tree(
            case["repo"],
            case["ctx"].base_sha,
            case["patch"],
            case["ctx"].config.raw["limits"]["fix_max_diff_lines"],
        ).sha
    )

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


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize("reach", [None, GitHubError(403)], ids=["unclear", "failed"])
@pytest.mark.parametrize("trigger", ["exposure", "failed-preflight"])
def test_unsafe_no_patch_reads_installation_before_writes(fix_case, mode, reach, trigger):
    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = mode
    case["artifact"].update(status="no_patch", attempted="Could not reproduce.")
    case["patch"] = None
    if trigger == "exposure":
        case["preflight"]["exposures"] = [
            {
                "workflow": "ci.yml",
                "job": "test",
                "rule": "secrets",
                "detail": "exposed",
                "secrets": ["ROLE_SECRET"],
            }
        ]
    else:
        case["preflight"]["pass"] = False
    case["api"].installation = reach
    calls = []
    request = case["api"].request

    def record(method, path, **kwargs):
        calls.append((method, path))
        return request(method, path, **kwargs)

    case["api"].request = record
    result = apply_case(case)
    marker = "UNSAFE ON SHARED APP: patch retained; no push."
    assert marker in result
    assert calls[0] == ("GET", "/installation/repositories")
    if mode == "live":
        assert calls.index(("GET", "/installation/repositories")) < calls.index(
            ("POST", "issues/12/comments")
        )
        assert marker in case["api"].writes[0][2]["body"]
        assert "Could not reproduce." in case["api"].writes[0][2]["body"]
    else:
        assert case["api"].writes == []


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
                "detail": "exposed",
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


@pytest.mark.parametrize("mode", ["dry-run", "live"])
@pytest.mark.parametrize(
    "reach",
    [
        pytest.param(
            {
                "repository_selection": "all",
                "total_count": 1,
                "repositories": [{"full_name": "your-org/project"}],
            },
            id="all",
        ),
        pytest.param(
            {
                "repository_selection": "selected",
                "total_count": 2,
                "repositories": [
                    {"full_name": "your-org/project"},
                    {"full_name": "your-org/other"},
                ],
            },
            id="two",
        ),
        pytest.param(
            {
                "repository_selection": "selected",
                "total_count": 1,
                "repositories": [{"full_name": "YOUR-ORG/PROJECT"}],
            },
            id="scoped-single",
        ),
        pytest.param(GitHubError(403, "synthetic-scan-key"), id="403"),
        pytest.param(GitHubError(500, "synthetic-scan-key"), id="500"),
        pytest.param(None, id="null"),
        pytest.param([], id="array"),
        pytest.param({}, id="missing-fields"),
        pytest.param(
            {
                "repository_selection": "selected",
                "total_count": "1",
                "repositories": [{"full_name": "your-org/project"}],
            },
            id="malformed-count",
        ),
        pytest.param(
            {
                "repository_selection": "selected",
                "total_count": 1,
                "repositories": [{"full_name": "your-org/other"}],
            },
            id="different-repo",
        ),
    ],
)
def test_installation_reach_withholds_accepted_exposure(fix_case, mode, reach):
    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = mode
    case["ctx"].config.raw["fix"]["accept_ci_exposure"] = True
    case["api"].installation = reach
    case["preflight"]["exposures"] = [
        {
            "workflow": "ci.yml",
            "job": "test",
            "rule": "secrets",
            "detail": "exposed",
            "secrets": ["ROLE_SECRET"],
        }
    ]
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    result = apply_case(case)
    assert result == (
        "UNSAFE ON SHARED APP: patch retained; no push.\n"
        "Accepted template exposure: rule=template-fix-propose job=propose secret=AGENT_FIX_KEY"
    )
    assert case["api"].reads.count("/installation/repositories") == 1
    assert all(path == "issues/12/comments" for _, path, _ in case["api"].writes)
    assert len(case["api"].writes) == (1 if mode == "live" else 0)
    if mode == "live":
        assert case["api"].writes[0][2]["body"].splitlines()[0] == (
            "UNSAFE ON SHARED APP: patch retained; no push."
        )
    assert "synthetic-scan-key" not in result


@pytest.mark.parametrize("preflight_pass", [False, True])
def test_installation_read_requires_valid_preflight_and_ci_exposure(fix_case, preflight_pass):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = "dry-run"
    case["preflight"]["pass"] = preflight_pass
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    case["preflight"]["base_sha"] = "c" * 40
    with pytest.raises(Violation):
        apply_case(case)
    assert case["api"].reads == [] and case["api"].writes == []
    case["preflight"]["base_sha"] = case["base"]
    result = apply_case(case)
    assert case["api"].reads == ([] if preflight_pass else ["/installation/repositories"])
    if preflight_pass:
        assert result.startswith("Fix dry-run: validated patch; no writes.")
        assert "template-fix-propose" in result and "AGENT_FIX_KEY" in result
    else:
        assert result.startswith("UNSAFE ON SHARED APP: patch retained; no push.\n")
        assert "template-fix-propose job=propose secret=AGENT_FIX_KEY" in result
    assert case["api"].writes == []


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
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    case["api"].ref = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[case["api"].ref] = {"message": message}
    if existing_pr:
        case["api"].pull = case["api"].agent_pull(case["api"].ref)
    result = apply_case(case)
    assert not any(path in {"git/refs", "git/commits"} for _, path, _ in case["api"].writes)
    assert sum(path == "pulls" for _, path, _ in case["api"].writes) == (0 if existing_pr else 1)
    assert "draft" in result.casefold()
    if existing_pr:
        assert any(path.startswith("git/commits/") for path in case["api"].reads)
        assert case["api"].labels == ["agent:fix"]
    else:
        body = next(body for _, path, body in case["api"].writes if path == "pulls")
        assert "Agent-Run: prior" in body["body"]


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


@pytest.mark.parametrize(
    "manifest",
    [
        "deno.json",
        "deno.jsonc",
        "go.mod",
        "build.sbt",
        "nested/constraints-dev.txt",
        "requirements/base.txt",
        "nested/requirements/dev.txt",
    ],
)
@pytest.mark.parametrize("allow", [False, True], ids=["refuse", "label"])
def test_unparsed_manifest_changes_follow_dependency_policy(fix_case, manifest, allow):
    from agent_repo.apply.fix import Violation

    case = fix_case
    content = {
        "go.mod": "module example.invalid/project\nrequire example.invalid/pkg v1.0.0\n",
        "build.sbt": 'libraryDependencies += "org.example" %% "new-library" % "1.0"\n',
    }.get(manifest, '{"imports":{"example":"https://example.invalid/mod.ts"}}\n')
    if manifest.endswith(".txt"):
        content = "requests==2.0\n"
    (case["repo"] / manifest).parent.mkdir(parents=True, exist_ok=True)
    (case["repo"] / manifest).write_text(content)
    case["artifact"], case["patch"] = build_artifact(
        case["repo"], case["base"], "run-1", answer(case["base"])
    )
    case["ctx"].config.raw["fix"]["allow_new_deps"] = allow
    if allow:
        assert "draft" in apply_case(case).casefold()
        assert any(
            path == "issues/24/labels" and "needs-maintainer" in body["labels"]
            for _, path, body in case["api"].writes
        )
    else:
        with pytest.raises(Violation, match="new or unparsed dependencies"):
            apply_case(case)
        assert case["api"].writes == []


@pytest.mark.parametrize(
    "table",
    [
        "build-system",
        "tool.poetry.dependencies",
        "tool.uv.sources",
        "tool.pdm.dev-dependencies",
        "tool.hatch.envs.default",
        "tool.ruff",
    ],
)
@pytest.mark.parametrize("allow", [False, True], ids=["refuse", "label"])
def test_pyproject_resolver_tables_are_unparsed_dependency_changes(fix_case, table, allow):
    from agent_repo.apply.fix import Violation

    case = fix_case
    repo = case["repo"]
    before = f'[project]\nname = "x"\ndependencies = ["requests"]\n\n[{table}]\nkey = "1"\n'
    (repo / "pyproject.toml").write_text(before)
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "-qm", "pyproject base")
    base = git(repo, "rev-parse", "HEAD").strip()
    case["base"] = base
    case["api"].base = base
    case["ctx"] = replace(case["ctx"], base_sha=base)
    case["preflight"]["base_sha"] = base
    (repo / "pyproject.toml").write_text(before + 'evilpkg = "*"\n')
    case["artifact"], case["patch"] = build_artifact(repo, base, "run-1", answer(base))
    case["ctx"].config.raw["fix"]["allow_new_deps"] = allow
    if allow or table == "tool.ruff":
        assert "Created draft fix." in apply_case(case)
        assert any(
            path == "issues/24/labels" and "needs-maintainer" in body["labels"]
            for _, path, body in case["api"].writes
        )
    else:
        with pytest.raises(Violation, match="new or unparsed dependencies"):
            apply_case(case)
        assert case["api"].writes == []


def test_mismatched_git_tree_refuses_before_commit(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["api"].tree_response = "a" * 40
    with pytest.raises(Violation, match="published tree mismatch"):
        apply_case(case)
    assert any(path == "git/trees" for _, path, _ in case["api"].writes)
    assert not any(
        path in {"git/commits", "git/refs", "pulls"} for _, path, _ in case["api"].writes
    )


def test_kill_switch_refuses_before_live_push(fix_case):
    from agent_repo.apply.actions import _AutomationDisabled

    case = fix_case
    request = case["api"].request

    def disabled(method, path, **kwargs):
        if method == "GET" and path == "issues/12":
            return {"labels": [{"name": "agent:skip"}], "state": "open"}
        return request(method, path, **kwargs)

    case["api"].request = disabled
    with pytest.raises(_AutomationDisabled):
        apply_case(case)
    assert case["api"].writes == []


def test_apply_command_uses_event_policy_and_trusted_context(
    fix_case, tmp_path, monkeypatch, capsys
):
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
    assert "/installation/repositories" not in case["api"].reads
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 0
    assert "AGENT_FIX_KEY" in (tmp_path / "summary").read_text()
    case["preflight"]["pass"] = False
    case["ctx"].config.raw["fix"]["accept_ci_exposure"] = True
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 0
    assert capsys.readouterr().out.splitlines()[-2] == (
        "UNSAFE ON SHARED APP: patch retained; no push."
    )
    assert (tmp_path / "summary").read_text().splitlines()[-2] == (
        "UNSAFE ON SHARED APP: patch retained; no push."
    )
    assert "AGENT_FIX_KEY" in (tmp_path / "summary").read_text()
    assert case["api"].writes == []
    trusted = json.loads(context.read_text())
    apply_calls = []
    original_apply = apply_fix.apply_fix

    def tracked_apply(*args, **kwargs):
        apply_calls.append(args)
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(apply_fix, "apply_fix", tracked_apply)
    for key, value in {
        "schema_version": "other",
        "run_id": "other",
        "base_sha": "c" * 40,
        "canary": "malformed",
    }.items():
        context.write_text(json.dumps({**trusted, key: value}))
        assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 1
        assert case["api"].writes == []
        assert apply_calls == []
    context.write_text(json.dumps(trusted))
    monkeypatch.setenv("FIX_REF", "agent/fix-13")
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 1
    monkeypatch.setenv("FIX_REF", "agent/fix-12")
    linked = tmp_path / "linked-fix.json"
    linked.symlink_to(artifact)
    assert apply_fix.main([str(linked), str(patch), str(case["repo"]), str(context)]) == 1
    assert apply_calls == [] and case["api"].writes == []
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 0
    context.unlink()
    assert apply_fix.main([str(artifact), str(patch), str(case["repo"]), str(context)]) == 1
    assert case["api"].writes == []


@pytest.mark.parametrize(
    "node", ["--help", "tests/../test_lib.py::test_add", "tests/./test_lib.py::test_add"]
)
def test_invalid_node_id_is_rejected_before_tree_validation(fix_case, monkeypatch, node):
    from agent_repo.apply import fix

    case = fix_case
    case["artifact"]["tests"] = [node]
    calls = []

    def validate(*args, **kwargs):
        calls.append(args)
        raise AssertionError("tree validation reached")

    monkeypatch.setattr(fix, "validate_tree", validate)
    with pytest.raises(fix.Violation, match="invalid pytest node ids"):
        apply_case(case)
    assert calls == [] and case["api"].writes == []


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
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].conflict = True
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {"message": message}
    original = case["api"].request

    def request(method, path, **kwargs):
        if method == "POST" and path == "git/refs":
            case["api"].ref = head
        return original(method, path, **kwargs)

    case["api"].request = request
    assert case["api"].pull["html_url"] in apply_case(case)
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)
    assert case["api"].labels == ["agent:fix"]
    assert case["api"].ref_reads >= 2
    assert case["api"].reads.count("pulls/24") >= 2


@pytest.mark.parametrize("forged", ["author", "commit-sha"])
def test_existing_ref_must_have_verified_bot_authorship(fix_case, forged):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    case["api"].ref = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[case["api"].ref] = {"message": message}
    original = case["api"].request

    def request(method, path, **kwargs):
        if forged == "author" and method == "GET" and path.startswith("commits/"):
            return {"author": {"type": "User", "login": "helper[bot]"}}
        if forged == "commit-sha" and method == "GET" and path.startswith("git/commits/"):
            return {**original(method, path, **kwargs), "sha": "c" * 40}
        return original(method, path, **kwargs)

    case["api"].request = request
    with pytest.raises(Violation, match="unverified agent commit"):
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


def test_new_draft_discloses_accepted_template_exposure(fix_case):
    case = fix_case
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    result = apply_case(case)
    body = next(body["body"] for _, path, body in case["api"].writes if path == "pulls")
    note = "Accepted template exposure: rule=template-fix-propose job=propose secret=AGENT_FIX_KEY"
    assert note in result and note in body


@pytest.mark.parametrize("mode", ["dry-run", "live"])
def test_unsafe_result_discloses_accepted_template_exposure(fix_case, mode):
    case = fix_case
    case["ctx"].config.raw["mode"]["fix"] = mode
    case["preflight"]["pass"] = False
    case["preflight"]["exposures"] = [
        {
            "workflow": "unsafe.yml",
            "job": "build",
            "rule": "secrets",
            "detail": "synthetic",
            "secrets": ["UNSAFE_KEY"],
        }
    ]
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    result = apply_case(case)
    note = "Accepted template exposure: rule=template-fix-propose job=propose secret=AGENT_FIX_KEY"
    assert result == "UNSAFE ON SHARED APP: patch retained; no push.\n" + note
    assert all(path == "issues/12/comments" for _, path, _ in case["api"].writes)
    if mode == "live":
        comment = case["api"].writes[0][2]["body"]
        assert comment.splitlines()[0] == "UNSAFE ON SHARED APP: patch retained; no push."
        assert note in comment
    else:
        assert case["api"].writes == []


@pytest.mark.parametrize("recovery", ["existing-ref", "conflict"])
def test_recovery_rejects_commit_for_different_issue(fix_case, recovery):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #13\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[head] = {"message": message}
    if recovery == "existing-ref":
        case["api"].ref = head
    else:
        case["api"].conflict = True
        original = case["api"].request

        def request(method, path, **kwargs):
            if method == "POST" and path == "git/refs":
                case["api"].ref = head
            return original(method, path, **kwargs)

        case["api"].request = request
    with pytest.raises(Violation, match="agent commit issue mismatch"):
        apply_case(case)
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)


def test_feedback_rejects_commit_for_different_issue(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #13\n\nAgent-Base: {head}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    with pytest.raises(Violation, match="agent commit issue mismatch"):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    assert case["api"].writes == []


@pytest.mark.parametrize("recovery", ["existing-ref", "conflict"])
def test_existing_draft_completes_decoration_from_published_head(fix_case, recovery):
    case = fix_case
    case["ctx"] = replace(
        case["ctx"],
        config=replace(
            case["ctx"].config,
            areas=(Area("manifest", "Manifest", ("package.json",), ("@maintainer",), False),),
        ),
    )
    if recovery == "existing-ref":
        original_artifact, original_patch = case["artifact"], case["patch"]
        (case["repo"] / "package.json").write_text("{}\n")
        case["artifact"], case["patch"] = build_artifact(
            case["repo"], case["base"], "run-1", answer(case["base"])
        )
        original = case["api"].request
        failed = False

        def request(method, path, **kwargs):
            nonlocal failed
            if method == "POST" and path == "issues/24/labels" and not failed:
                failed = True
                raise GitHubError(503)
            return original(method, path, **kwargs)

        case["api"].request = request
        with pytest.raises(GitHubError):
            apply_case(case)
        assert case["api"].pull is not None
        case["api"].request = original
        case["artifact"], case["patch"] = original_artifact, original_patch
        case["api"].writes.clear()
    else:
        case["api"].conflict = True
    git(case["repo"], "checkout", "--", "lib.py")
    (case["repo"] / "package.json").write_text("{}\n")
    git(case["repo"], "add", "package.json")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {"message": message}
    assert "Existing draft" in apply_case(case)
    assert case["api"].labels == ["agent:fix", "needs-maintainer"]
    assert case["api"].requested_users == ["maintainer"]
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)
    if recovery == "existing-ref":
        case["api"].writes.clear()
        assert "Existing draft" in apply_case(case)
        assert not any(
            path.endswith("/labels") or path.endswith("/requested_reviewers")
            for _, path, _ in case["api"].writes
        )


@pytest.mark.parametrize("route", ["existing-ref", "conflict"])
@pytest.mark.parametrize("move_at", ["published-read", "after-decoration"])
@pytest.mark.parametrize("moved", ["ref", "pull", "both"])
def test_existing_draft_refuses_late_published_head_move(fix_case, route, move_at, moved):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    old_head = git(case["repo"], "rev-parse", "HEAD").strip()
    (case["repo"] / "package.json").write_text("{}\n")
    git(case["repo"], "add", "package.json")
    git(case["repo"], "commit", "-qm", message)
    new_head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].ref = old_head if route == "existing-ref" else None
    case["api"].pull = case["api"].agent_pull(old_head)
    case["api"].commits[old_head] = {"message": message}
    case["api"].conflict = route == "conflict"
    original = case["api"].request

    def move():
        if moved in {"ref", "both"}:
            case["api"].ref = new_head
        if moved in {"pull", "both"}:
            case["api"].pull["head"]["sha"] = new_head

    def request(method, path, **kwargs):
        if method == "POST" and path == "git/refs":
            case["api"].ref = old_head
        if move_at == "published-read" and method == "GET" and path == "git/commits/" + old_head:
            move()
        result = original(method, path, **kwargs)
        if move_at == "after-decoration" and method == "POST" and path == "issues/24/labels":
            move()
        return result

    case["api"].request = request
    with pytest.raises(Violation, match="Fix refused: published draft moved"):
        apply_case(case)
    if move_at == "published-read":
        assert case["api"].labels == []
        assert case["api"].requested_users == []
        assert not any(path.endswith("/requested_reviewers") for _, path, _ in case["api"].writes)


def test_existing_draft_refuses_unreadable_published_head(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["api"].ref = "c" * 40
    case["api"].pull = case["api"].agent_pull(case["api"].ref)
    case["api"].commits[case["api"].ref] = {
        "message": f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    with pytest.raises(Violation, match="cannot inspect published change on agent/fix-12"):
        apply_case(case)
    assert case["api"].writes == []


def test_feedback_update_discloses_accepted_template_exposure_in_pr_body(fix_case):
    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {head}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    result = apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    body = next(body["body"] for _, path, body in case["api"].writes if path == "pulls/24")
    assert "AGENT_FIX_KEY" in result and "AGENT_FIX_KEY" in body
    assert next(path for _, path, _ in case["api"].writes if path.startswith("git/refs/"))


def test_recovery_uses_published_manifest_and_fixed_description(fix_case):
    from agent_repo.config import Area

    case = fix_case
    repo = case["repo"]
    git(repo, "checkout", "--", "lib.py")
    (repo / "package.json").write_text('{"dependencies":{"old":"1"}}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "manifest base")
    base = git(repo, "rev-parse", "HEAD").strip()
    case["base"] = base
    case["api"].base = base
    case["ctx"] = replace(case["ctx"], base_sha=base)
    case["preflight"]["base_sha"] = base
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    case["artifact"], case["patch"] = build_artifact(repo, base, "run-1", answer(base))
    git(repo, "checkout", "--", "lib.py")
    (repo / "package.json").write_text('{"dependencies":{"old":"2"}}\n')
    git(repo, "add", ".")
    message = (
        f"Fix #12\n\nAgent-Base: {base}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(repo, "commit", "-qm", message)
    head = git(repo, "rev-parse", "HEAD").strip()
    case["api"].ref = head
    case["api"].commits[head] = {"message": message}
    case["ctx"] = replace(
        case["ctx"],
        config=replace(
            case["ctx"].config,
            areas=(Area("manifest", "Manifest", ("package.json",), ("@maintainer",), False),),
        ),
    )
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    result = apply_case(case)
    pull = next(body for _, path, body in case["api"].writes if path == "pulls")
    labels = next(
        body["labels"] for _, path, body in case["api"].writes if path.endswith("/labels")
    )
    reviewers = next(
        body for _, path, body in case["api"].writes if path.endswith("/requested_reviewers")
    )
    assert "needs-maintainer" in labels and reviewers["reviewers"] == ["maintainer"]
    assert f"Agent-Base: {base}" in pull["body"] and "Agent-Run: prior" in pull["body"]
    assert "AGENT_FIX_KEY" in pull["body"] and "AGENT_FIX_KEY" in result
    assert case["artifact"]["summary"] not in pull["body"]
    assert case["api"].ref_reads >= 2


def test_existing_draft_links_when_its_base_predates_event_base(fix_case):
    case = fix_case
    old_base = case["base"]
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {old_base}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    (case["repo"] / "unrelated.txt").write_text("later\n")
    git(case["repo"], "add", "unrelated.txt")
    git(case["repo"], "commit", "-qm", "new event base")
    new_base = git(case["repo"], "rev-parse", "HEAD").strip()
    (case["repo"] / "new.py").write_text("value = 1\n")
    case["artifact"], case["patch"] = build_artifact(
        case["repo"], new_base, "run-1", answer(new_base)
    )
    case["base"] = new_base
    case["ctx"] = replace(case["ctx"], base_sha=new_base)
    case["api"].base = new_base
    case["preflight"]["base_sha"] = new_base
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {"message": message}
    assert case["api"].pull["html_url"] in apply_case(case)
    assert any(path == "git/commits/" + head for path in case["api"].reads)
    assert case["api"].labels == ["agent:fix"]


def test_recovery_ref_move_refuses_before_pull_creation(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    case["api"].ref = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[case["api"].ref] = {"message": message}
    case["api"].ref_move_on_read = 2
    with pytest.raises(Violation, match=r"^Fix refused: published draft moved\.$"):
        apply_case(case)
    assert case["api"].writes == []


@pytest.mark.parametrize("move_on_read", [2, 3], ids=["before-objects", "after-objects"])
def test_feedback_ref_move_after_object_creation_refuses_before_patch(fix_case, move_on_read):
    from agent_repo.apply.fix import Violation

    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {head}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    case["api"].ref_move_on_read = move_on_read
    with pytest.raises(Violation, match="feedback ref moved"):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    if move_on_read == 2:
        assert case["api"].writes == []
    else:
        assert any(path == "git/commits" for _, path, _ in case["api"].writes)
    assert not any(path.startswith("git/refs") for _, path, _ in case["api"].writes)


def test_feedback_rejects_head_with_different_agent_base(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {'9' * 40}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    with pytest.raises(Violation, match="unverified agent commit"):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    assert case["api"].writes == []


def test_ref_creation_conflict_recovers_published_commit(fix_case):
    case = fix_case
    git(case["repo"], "checkout", "--", "lib.py")
    (case["repo"] / "package.json").write_text("{}\n")
    git(case["repo"], "add", "package.json")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    prior = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[prior] = {"message": message}
    case["api"].conflict = True
    original = case["api"].request

    def request(method, path, **kwargs):
        if method == "POST" and path == "git/refs":
            case["api"].ref = prior
        return original(method, path, **kwargs)

    case["api"].request = request
    assert "recovered" in apply_case(case)
    pull = next(body for _, path, body in case["api"].writes if path == "pulls")
    assert "Agent-Run: prior" in pull["body"] and case["artifact"]["summary"] not in pull["body"]
    labels = next(
        body["labels"] for _, path, body in case["api"].writes if path.endswith("/labels")
    )
    assert "needs-maintainer" in labels


def test_unreadable_published_change_refuses_before_pull(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["api"].ref = "c" * 40
    case["api"].commits[case["api"].ref] = {
        "message": f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    with pytest.raises(Violation, match="cannot inspect published change on agent/fix-12"):
        apply_case(case)
    assert case["api"].writes == []


def test_recovery_uses_older_agent_base_after_event_base_advances(fix_case):
    case = fix_case
    old_base = case["base"]
    (case["repo"] / "package.json").write_text("{}\n")
    git(case["repo"], "add", "lib.py", "package.json")
    message = (
        f"Fix #12\n\nAgent-Base: {old_base}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    old_head = git(case["repo"], "rev-parse", "HEAD").strip()
    (case["repo"] / "unrelated.txt").write_text("new base\n")
    git(case["repo"], "add", "unrelated.txt")
    git(case["repo"], "commit", "-qm", "advance default branch")
    new_base = git(case["repo"], "rev-parse", "HEAD").strip()
    (case["repo"] / "new.py").write_text("value = 1\n")
    case["artifact"], case["patch"] = build_artifact(
        case["repo"], new_base, "run-1", answer(new_base)
    )
    case["base"] = new_base
    case["ctx"] = replace(case["ctx"], base_sha=new_base)
    case["api"].base = new_base
    case["preflight"]["base_sha"] = new_base
    case["api"].ref = old_head
    case["api"].commits[old_head] = {"message": message}
    case["ctx"] = replace(
        case["ctx"],
        config=replace(
            case["ctx"].config,
            areas=(Area("manifest", "Manifest", ("package.json",), ("@maintainer",), False),),
        ),
    )
    assert "recovered" in apply_case(case)
    body = next(body["body"] for _, path, body in case["api"].writes if path == "pulls")
    assert f"Agent-Base: {old_base}" in body and f"Agent-Base: {new_base}" not in body
    assert case["api"].labels == ["agent:fix", "needs-maintainer"]
    assert case["api"].requested_users == ["maintainer"]


def test_recovery_refuses_published_protected_path(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "checkout", "--", "lib.py")
    (case["repo"] / "AGENTS.md").write_text("protected\n")
    git(case["repo"], "add", "AGENTS.md")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    head = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].ref = head
    case["api"].commits[head] = {"message": message}
    with pytest.raises(Violation, match="cannot inspect published change on agent/fix-12"):
        apply_case(case)
    assert case["api"].writes == []


def test_feedback_update_conflict_never_claims_success(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {head}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    original = case["api"].request

    def request(method, path, **kwargs):
        if method == "PATCH" and path == "git/refs/heads/agent/fix-12":
            raise GitHubError(422)
        return original(method, path, **kwargs)

    case["api"].request = request
    with pytest.raises(Violation, match="feedback ref update conflict"):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    assert not any(path == "pulls" for _, path, _ in case["api"].writes)


def _move_published_head_after(case, method, target):
    original = case["api"].request

    def request(called, path, **kwargs):
        result = original(called, path, **kwargs)
        if called == method and path == target:
            case["api"].ref = "a" * 40
            if case["api"].pull:
                case["api"].pull["head"]["sha"] = "a" * 40
        return result

    case["api"].request = request


def _writes_after(case, target):
    paths = [path for _, path, _ in case["api"].writes]
    return paths[paths.index(target) + 1 :]


@pytest.mark.parametrize(
    "target,owned",
    [
        ("git/refs", False),
        ("pulls", False),
        ("issues/24/labels", False),
        ("issues/24/labels", True),
        ("pulls/24/requested_reviewers", True),
    ],
)
def test_new_draft_refuses_when_published_head_moves(fix_case, target, owned):
    from agent_repo.apply.fix import Violation

    case = fix_case
    if owned:
        areas = (Area("core", "Core", ("lib.py",), ("@maintainer",), False),)
        case["ctx"] = replace(case["ctx"], config=replace(case["ctx"].config, areas=areas))
    _move_published_head_after(case, "POST", target)
    with pytest.raises(Violation, match="Fix refused: published draft moved."):
        apply_case(case)
    assert _writes_after(case, target) == []


def test_recovered_draft_refuses_when_published_head_moves_after_creation(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    git(case["repo"], "add", "lib.py")
    message = (
        f"Fix #12\n\nAgent-Base: {case['base']}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    )
    git(case["repo"], "commit", "-qm", message)
    case["api"].ref = git(case["repo"], "rev-parse", "HEAD").strip()
    case["api"].commits[case["api"].ref] = {"message": message}
    _move_published_head_after(case, "POST", "pulls")
    with pytest.raises(Violation, match="Fix refused: published draft moved."):
        apply_case(case)
    assert _writes_after(case, "pulls") == []


def test_feedback_update_refuses_when_published_head_moves_after_push(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    head = case["base"]
    case["api"].ref = head
    case["api"].pull = case["api"].agent_pull(head)
    case["api"].commits[head] = {
        "message": f"Fix #12\n\nAgent-Base: {head}\n"
        "Agent-Run: prior\nAgent-Tests: tests/test_lib.py::test_add\n"
    }
    case["ctx"] = replace(case["ctx"], event=replace(case["ctx"].event, is_pr=True, number=24))
    case["preflight"]["accepted_template_exposures"] = [
        {"rule": "template-fix-propose", "job": "propose", "secret": "AGENT_FIX_KEY"}
    ]
    _move_published_head_after(case, "PATCH", "git/refs/heads/agent/fix-12")
    with pytest.raises(Violation, match="Fix refused: published draft moved."):
        apply_case(case, feedback_head=head, feedback_ref="agent/fix-12")
    assert _writes_after(case, "git/refs/heads/agent/fix-12") == []


def test_malformed_trusted_canary_refuses_before_any_request(fix_case):
    from agent_repo.apply.fix import Violation

    case = fix_case
    case["ctx"] = replace(case["ctx"], canary="B" * 32)
    with pytest.raises(Violation, match="trusted inputs unavailable"):
        apply_case(case)
    assert case["api"].writes == [] and case["api"].reads == []
