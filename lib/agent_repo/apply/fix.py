import base64
import fnmatch
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from agent_repo.apply.actions import (
    Action,
    ApplyContext,
    _check_enabled,
    canonical_bot_login,
    check_identity,
)
from agent_repo.apply.triage import _strings
from agent_repo.commands.fix_artifact import MAX_PATCH_BYTES, git_environment
from agent_repo.deps import is_manifest, new_dependencies
from agent_repo.github import GitHubError
from agent_repo.render import fenced
from agent_repo.scan import find_leaks
from agent_repo.schema import load_schema, validate

BLOCKED_GLOBS = (
    ".github/**",
    ".agents/**",
    "**/.github/**",
    "**/.agents/**",
    ".gitattributes",
    ".gitmodules",
    "AGENTS.md",
    "CODEOWNERS",
    "*.lock",
    "*-lock.json",
    "*-lock.yaml",
    "*-lock.yml",
    "*.lock.*",
    "bun.lockb",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "Pipfile.lock",
    "packages.lock.json",
    "go.sum",
    "Gemfile.lock",
    ".git/**",
    "**/.git/**",
)
AGENT_MESSAGE = re.compile(
    r"Fix #(?P<issue>[1-9][0-9]*)\n\nAgent-Base: (?P<base>[0-9a-f]{40})\n"
    r"Agent-Run: (?P<run>[A-Za-z0-9_.-]{1,64})\n"
    r"Agent-Tests:(?P<tests>(?: [A-Za-z0-9_./:\[\]-]+){0,20})\n?"
)


class Violation(ValueError):
    """A fixed, credential-free reason to refuse the proposed fix."""


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise Violation("Fix refused: invalid Git identity.")
    return value


def branch_for(issue: int) -> str:
    if type(issue) is not int or issue < 1:
        raise Violation("Fix refused: invalid issue.")
    return f"agent/fix-{issue}"


def test_files(ids: list[str]) -> list[str]:
    if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids):
        raise Violation("Fix refused: invalid pytest node ids.")
    paths = []
    for node in ids:
        if (
            not isinstance(node, str)
            or re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(?:::[A-Za-z_][A-Za-z0-9_]*)+(?:\[[A-Za-z0-9_.-]+\])?",
                node,
            )
            is None
        ):
            raise Violation("Fix refused: invalid pytest node ids.")
        path = node.split("::", 1)[0]
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise Violation("Fix refused: invalid pytest node ids.")
        paths.append(path)
    return list(dict.fromkeys(paths))


def _git(repo: Path, *args: str, index: str | None = None, data: bytes | None = None) -> bytes:
    env = git_environment()
    env["GIT_LITERAL_PATHSPECS"] = "1"
    if index:
        env["GIT_INDEX_FILE"] = index
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *args],
            cwd=repo,
            env=env,
            input=data,
            capture_output=True,
            check=True,
            timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise Violation("Fix refused: Git validation failed.") from None


@dataclass(frozen=True)
class ValidatedTree:
    sha: str
    paths: tuple[str, ...]
    manifests: tuple[str, ...]
    dependencies: frozenset[str]


def _blob(repo: Path, tree: str, path: str) -> bytes:
    entry = _git(repo, "ls-tree", "-z", tree, "--", path)
    if not entry:
        return b""
    identity, _, _ = entry.partition(b"\t")
    return _git(repo, "cat-file", "blob", identity.split()[2].decode())


def validate_tree(repo: Path, base: str, patch: str, max_lines: int) -> ValidatedTree:
    _sha(base)
    if (
        not isinstance(patch, str)
        or not patch
        or len(patch.encode("utf-8", "surrogateescape")) > MAX_PATCH_BYTES
    ):
        raise Violation("Fix refused: invalid patch size.")
    with tempfile.TemporaryDirectory(prefix="agent-fix-index-") as directory:
        index = str(Path(directory) / "index")
        _git(repo, "read-tree", base, index=index)
        _git(
            repo,
            "apply",
            "--cached",
            "--whitespace=nowarn",
            "-",
            index=index,
            data=patch.encode("utf-8", "surrogateescape"),
        )
        tree = _sha(_git(repo, "write-tree", index=index).decode().strip())
    raw = _git(
        repo, "diff", "--raw", "-z", "-M", "--no-ext-diff", "--no-textconv", base, tree
    ).split(b"\0")
    paths = set()
    position = 0
    while position < len(raw) - 1:
        fields = raw[position].split()
        modes = {fields[0].lstrip(b":"), fields[1]}
        status = fields[4][:1]
        position += 1
        names = [raw[position].decode("utf-8", "surrogateescape")]
        position += 1
        if status in {b"R", b"C"}:
            names.append(raw[position].decode("utf-8", "surrogateescape"))
            position += 1
        if not modes <= {b"000000", b"100644", b"100755"} or (
            len(modes) > 1 and b"100755" in modes
        ):
            raise Violation("Fix refused: unsafe file mode.")
        for path in names:
            folded = path.casefold()
            if any(
                fnmatch.fnmatchcase(
                    folded if "/" in pattern else PurePosixPath(folded).name, pattern.casefold()
                )
                for pattern in BLOCKED_GLOBS
            ):
                raise Violation("Fix refused: protected path.")
            paths.add(path)
    counts = _git(repo, "diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv", base, tree)
    total = 0
    for entry in counts.split(b"\0"):
        parts = entry.split(b"\t", 2)
        if len(parts) != 3:
            continue
        if parts[0] == b"-" or parts[1] == b"-":
            raise Violation("Fix refused: binary change.")
        total += int(parts[0]) + int(parts[1])
    if not paths or type(max_lines) is not int or total > max_lines:
        raise Violation("Fix refused: diff limit exceeded.")
    manifests, dependencies = [], set()
    for path in sorted(paths):
        before, after = _blob(repo, base, path), _blob(repo, tree, path)
        if b"\0" in before or b"\0" in after:
            raise Violation("Fix refused: binary change.")
        if is_manifest(path):
            manifests.append(path)
            dependencies.update(
                new_dependencies(
                    path, before.decode("utf-8", "replace"), after.decode("utf-8", "replace")
                )
            )
    return ValidatedTree(tree, tuple(sorted(paths)), tuple(manifests), frozenset(dependencies))


def _workflow_tree(ctx: ApplyContext) -> str:
    branch = quote(ctx.event.default_branch, safe="")
    ref = ctx.github.request("GET", f"git/ref/heads/{branch}")
    commit = _sha(ref["object"]["sha"])
    directory = ctx.github.request(
        "GET",
        "contents/.github/workflows",
        params={"ref": commit},
        accept="application/vnd.github.object+json",
    )
    if directory["type"] != "dir":
        raise Violation("Fix refused: workflow tree unavailable.")
    return _sha(directory["sha"])


def _ref(ctx: ApplyContext, branch: str) -> str | None:
    try:
        return _sha(
            ctx.github.request("GET", f"git/ref/heads/{quote(branch, safe='/')}")["object"]["sha"]
        )
    except GitHubError as error:
        if error.status == 404:
            return None
        raise


def _agent_commit(
    ctx: ApplyContext, head: str, issue: int, *, base: str | None = None
) -> re.Match[str]:
    commit = ctx.github.request("GET", f"git/commits/{_sha(head)}")
    author = ctx.github.request("GET", f"commits/{head}")["author"]
    if author["type"] != "Bot" or canonical_bot_login(author["login"]) != ctx.bot_login:
        raise Violation("Fix refused: unverified agent commit.")
    match = AGENT_MESSAGE.fullmatch(commit["message"])
    if commit["sha"] != head or not match or (base is not None and match["base"] != base):
        raise Violation("Fix refused: unverified agent commit.")
    if int(match["issue"]) != issue:
        raise Violation("Fix refused: agent commit issue mismatch.")
    return match


def _recovered_tree(repo: Path, branch: str, head: str, base: str, max_lines: int) -> ValidatedTree:
    try:
        patch = _git(
            repo, "diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", base, head
        ).decode("utf-8", "surrogateescape")
        recovered = validate_tree(repo, base, patch, max_lines)
        tree_sha = _sha(_git(repo, "rev-parse", head + "^{tree}").decode().strip())
        if recovered.sha != tree_sha:
            raise ValueError
        return recovered
    except (Violation, ValueError, UnicodeError):
        raise Violation(f"Fix refused: cannot inspect published change on {branch}.") from None


def _pull_identity(pull: dict, ctx: ApplyContext, issue: int, head: str) -> None:
    if not (
        pull["draft"] is True
        and pull["state"] == "open"
        and pull["user"]["type"] == "Bot"
        and canonical_bot_login(pull["user"]["login"]) == ctx.bot_login
        and pull["head"]["repo"]["full_name"] == ctx.event.repo
        and pull["head"]["ref"] == branch_for(issue)
        and pull["head"]["sha"] == head
        and pull["base"]["ref"] == ctx.event.default_branch
        and re.search(
            rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+#{issue}(?![0-9])",
            pull["body"] or "",
            re.I,
        )
    ):
        raise Violation("Fix refused: feedback identity moved.")


def _enabled(ctx: ApplyContext, issue: int) -> None:
    _check_enabled(
        [Action("comment", {"number": issue, "bot_login": ctx.bot_login})], ctx.github, issue
    )


def _create_commit(
    ctx: ApplyContext, repo: Path, tree: ValidatedTree, parent: str, message: str
) -> str:
    entries = []
    for path in tree.paths:
        listing = _git(repo, "ls-tree", "-z", tree.sha, "--", path)
        if not listing:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        metadata = listing.partition(b"\t")[0].split()
        blob = _git(repo, "cat-file", "blob", metadata[2].decode())
        value = ctx.github.request(
            "POST",
            "git/blobs",
            body={"content": base64.b64encode(blob).decode(), "encoding": "base64"},
        )
        entries.append(
            {"path": path, "mode": metadata[0].decode(), "type": "blob", "sha": _sha(value["sha"])}
        )
    base_tree = _git(repo, "rev-parse", ctx.base_sha + "^{tree}").decode().strip()
    value = ctx.github.request("POST", "git/trees", body={"base_tree": base_tree, "tree": entries})
    if _sha(value["sha"]) != tree.sha:
        raise Violation("Fix refused: published tree mismatch.")
    return _sha(
        ctx.github.request(
            "POST",
            "git/commits",
            body={"message": message, "tree": _sha(value["sha"]), "parents": [parent]},
        )["sha"]
    )


def _decorate_pull(
    ctx: ApplyContext, number: int, tree: ValidatedTree, *, existing: bool = False
) -> None:
    labels = ["agent:fix"] + (["needs-maintainer"] if tree.manifests else [])
    if existing:
        current_labels = {item["name"] for item in ctx.github.paginate(f"issues/{number}/labels")}
        labels = [label for label in labels if label not in current_labels]
    if labels:
        ctx.github.request("POST", f"issues/{number}/labels", body={"labels": labels})
    owners = {
        owner.removeprefix("@")
        for area in ctx.config.areas
        if any(fnmatch.fnmatchcase(path, pattern) for path in tree.paths for pattern in area.paths)
        for owner in area.owners
    }
    users = sorted(owner for owner in owners if "/" not in owner)
    organization = ctx.event.repo.split("/")[0].casefold()
    teams = sorted(
        owner.split("/", 1)[1]
        for owner in owners
        if "/" in owner and owner.split("/", 1)[0].casefold() == organization
    )
    if existing and (users or teams):
        requested = ctx.github.request("GET", f"pulls/{number}/requested_reviewers")
        current_users = {user["login"] for user in requested["users"]}
        current_teams = {team["slug"] for team in requested["teams"]}
        users = [user for user in users if user not in current_users]
        teams = [team for team in teams if team not in current_teams]
    if users or teams:
        ctx.github.request(
            "POST",
            f"pulls/{number}/requested_reviewers",
            body={"reviewers": users, "team_reviewers": teams},
        )


def _accepted_template_note(preflight: dict) -> str:
    records = preflight["accepted_template_exposures"]
    if len(records) > 10:
        raise Violation("Fix refused: too many accepted template exposures.")
    return "\n".join(
        f"Accepted template exposure: rule={item['rule']} job={item['job']} secret={item['secret']}"
        for item in records
    )


def _create_recovered_pull(
    ctx: ApplyContext,
    repo: Path,
    issue: int,
    branch: str,
    head: str,
    note: str,
    workflow_sha: str,
) -> str:
    match, recovered = _published_change(ctx, repo, issue, branch, head)
    if _workflow_tree(ctx) != workflow_sha:
        return "Fix dry-run: workflow tree moved before PR creation."
    if _ref(ctx, branch) != head:
        raise Violation(f"Fix refused: published ref moved on {branch}.")
    body = (
        f"Fixes #{issue}\n\nRecovered published agent fix.\n\n"
        f"Agent-Run: {match['run']}\nAgent-Base: {match['base']}"
    )
    if note:
        body += "\n\n" + note
    pull = ctx.github.request(
        "POST",
        "pulls",
        body={
            "title": f"Fix #{issue}",
            "body": body,
            "head": branch,
            "base": ctx.event.default_branch,
            "draft": True,
        },
    )
    number = pull["number"]
    if type(number) is not int or number < 1:
        raise Violation("Fix refused: invalid pull request.")
    _decorate_pull(ctx, number, recovered)
    return "Created recovered draft fix." + ("\n" + note if note else "")


def _published_change(
    ctx: ApplyContext, repo: Path, issue: int, branch: str, head: str
) -> tuple[re.Match[str], ValidatedTree]:
    match = _agent_commit(ctx, head, issue)
    recovered = _recovered_tree(
        repo, branch, head, match["base"], ctx.config.raw["limits"]["fix_max_diff_lines"]
    )
    if recovered.dependencies and not ctx.config.raw["fix"]["allow_new_deps"]:
        raise Violation(f"Fix refused: cannot inspect published change on {branch}.")
    return match, recovered


def _unchanged_published_draft(
    ctx: ApplyContext, issue: int, branch: str, number: int, head: str
) -> None:
    try:
        if _ref(ctx, branch) != head:
            raise Violation("Fix refused: published draft moved.")
        _pull_identity(ctx.github.request("GET", f"pulls/{number}"), ctx, issue, head)
    except (GitHubError, KeyError, TypeError, Violation):
        raise Violation("Fix refused: published draft moved.") from None


def _feedback_body_with_note(pull: dict, note: str, ctx: ApplyContext) -> str | None:
    if not note:
        return None
    body = pull.get("body")
    if not isinstance(body, str) or len(body) > 16000:
        raise Violation("Fix refused: feedback body unavailable.")
    if note in body:
        return None
    updated = body + "\n\n" + note
    if find_leaks(updated, ctx.secrets, ctx.canary):
        raise Violation("Fix refused: outbound scan alert.")
    return updated


def apply_fix(
    artifact: dict,
    patch: str | None,
    repo: Path,
    ctx: ApplyContext,
    preflight: dict,
    *,
    issue: int,
    feedback_head: str = "",
    feedback_ref: str = "",
) -> str:
    mode = ctx.config.mode_for("fix")
    branch = branch_for(issue)
    if (
        mode not in {"live", "dry-run"}
        or not ctx.github
        or re.fullmatch(r"[0-9a-f]{32}", ctx.canary) is None
        or not ctx.secrets
        or not canonical_bot_login(ctx.bot_login)
    ):
        raise Violation("Fix refused: trusted inputs unavailable.")
    if (
        check_identity(artifact, ctx, "fix")
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ctx.run_id) is None
    ):
        raise Violation("Fix refused: artifact identity or schema.")
    if find_leaks("".join(_strings(artifact)), ctx.secrets, ctx.canary) or (
        patch and find_leaks(patch, ctx.secrets, ctx.canary)
    ):
        raise Violation("Fix refused: outbound scan alert.")
    if feedback_head:
        _sha(feedback_head)
        if feedback_ref != branch or not ctx.event.is_pr:
            raise Violation("Fix refused: feedback identity moved.")
        _pull_identity(
            ctx.github.request("GET", f"pulls/{ctx.event.number}"), ctx, issue, feedback_head
        )
        _agent_commit(ctx, feedback_head, issue, base=ctx.base_sha)
        if _ref(ctx, branch) != feedback_head:
            raise Violation("Fix refused: feedback ref moved.")
    elif ctx.event.is_pr or issue != ctx.event.number or feedback_ref:
        raise Violation("Fix refused: event identity mismatch.")
    if (
        validate(preflight, load_schema("preflight"))
        or preflight["run_id"] != ctx.run_id
        or preflight["base_sha"] != ctx.base_sha
        or preflight["workflow_tree_sha"] is None
    ):
        raise Violation("Fix refused: preflight identity or schema.")
    template_note = _accepted_template_note(preflight)
    unsafe = not preflight["pass"] or bool(preflight["exposures"])
    unsafe_marker = "UNSAFE ON SHARED APP: patch retained; no push."
    if unsafe:
        try:
            ctx.github.request("GET", "/installation/repositories")
        except GitHubError:
            pass
    if artifact["status"] == "no_patch":
        if not artifact["attempted"].strip() or patch:
            raise Violation("Fix refused: invalid no-patch result.")
        body = "No patch proposed.\n\n" + fenced(artifact["attempted"], 4000)
        if template_note:
            body += "\n\n" + template_note
        if unsafe:
            body += "\n\n" + unsafe_marker
        if mode == "live":
            _enabled(ctx, issue)
            ctx.github.request("POST", f"issues/{issue}/comments", body={"body": body})
        else:
            return body + "\n\nDry-run: no writes."
        result = (
            "No patch proposed (" + mode + ")." + ("\n" + template_note if template_note else "")
        )
        return result + ("\n" + unsafe_marker if unsafe else "")
    test_files(artifact["tests"])
    tree = validate_tree(repo, ctx.base_sha, patch, ctx.config.raw["limits"]["fix_max_diff_lines"])
    if tree.dependencies and not ctx.config.raw["fix"]["allow_new_deps"]:
        raise Violation("Fix refused: new or unparsed dependencies.")
    if unsafe:
        # A repository-scoped token's response does not prove installation-wide reach.
        result = unsafe_marker + ("\n" + template_note if template_note else "")
        if mode == "live":
            _enabled(ctx, issue)
            ctx.github.request(
                "POST",
                f"issues/{issue}/comments",
                body={
                    "body": (
                        result + "\nFix withheld: unsafe CI exposure. "
                        "Patch retained for maintainer review."
                    )
                },
            )
        return result
    if mode == "dry-run":
        return "Fix dry-run: validated patch; no writes. " + (
            "\n" + template_note if template_note else ""
        )
    _enabled(ctx, issue)
    if _workflow_tree(ctx) != preflight["workflow_tree_sha"]:
        return "Fix dry-run: workflow tree moved before push."
    existing = _ref(ctx, branch)
    if feedback_head and existing != feedback_head:
        raise Violation("Fix refused: feedback ref moved.")
    if not feedback_head and existing:
        pulls = ctx.github.paginate(
            "pulls", params={"state": "open", "head": ctx.event.repo.split("/")[0] + ":" + branch}
        )
        for pull in pulls:
            _pull_identity(pull, ctx, issue, existing)
            _, published = _published_change(ctx, repo, issue, branch, existing)
            _unchanged_published_draft(ctx, issue, branch, pull["number"], existing)
            _decorate_pull(ctx, pull["number"], published, existing=True)
            _unchanged_published_draft(ctx, issue, branch, pull["number"], existing)
            return (
                "Existing draft: "
                + pull["html_url"]
                + ("\n" + template_note if template_note else "")
            )
        return _create_recovered_pull(
            ctx, repo, issue, branch, existing, template_note, preflight["workflow_tree_sha"]
        )
    else:
        message = (
            f"Fix #{issue}\n\nAgent-Base: {ctx.base_sha}\n"
            f"Agent-Run: {ctx.run_id}\nAgent-Tests: {' '.join(artifact['tests'])}\n"
        )
        commit = _create_commit(ctx, repo, tree, feedback_head or ctx.base_sha, message)
        # Ref and workflows are re-read after object creation, immediately before publication.
        feedback_body = None
        if feedback_head:
            feedback_pull = ctx.github.request("GET", f"pulls/{ctx.event.number}")
            _pull_identity(feedback_pull, ctx, issue, feedback_head)
            feedback_body = _feedback_body_with_note(feedback_pull, template_note, ctx)
            if _ref(ctx, branch) != feedback_head:
                raise Violation("Fix refused: feedback ref moved.")
        if _workflow_tree(ctx) != preflight["workflow_tree_sha"]:
            return "Fix dry-run: workflow tree moved before push."
        try:
            if feedback_head:
                ctx.github.request(
                    "PATCH", f"git/refs/heads/{branch}", body={"sha": commit, "force": False}
                )
            else:
                ctx.github.request(
                    "POST", "git/refs", body={"ref": "refs/heads/" + branch, "sha": commit}
                )
        except GitHubError as error:
            if error.status != 422:
                raise
            if feedback_head:
                raise Violation("Fix refused: feedback ref update conflict.") from None
            pulls = ctx.github.paginate(
                "pulls",
                params={"state": "open", "head": ctx.event.repo.split("/")[0] + ":" + branch},
            )
            for pull in pulls:
                published_head = _sha(pull["head"]["sha"])
                _pull_identity(pull, ctx, issue, published_head)
                _, published = _published_change(ctx, repo, issue, branch, published_head)
                _unchanged_published_draft(ctx, issue, branch, pull["number"], published_head)
                _decorate_pull(ctx, pull["number"], published, existing=True)
                _unchanged_published_draft(ctx, issue, branch, pull["number"], published_head)
                return (
                    "Existing draft: "
                    + pull["html_url"]
                    + ("\n" + template_note if template_note else "")
                )
            current = _ref(ctx, branch)
            if current is None:
                raise Violation("Fix refused: ref creation conflict.") from None
            return _create_recovered_pull(
                ctx, repo, issue, branch, current, template_note, preflight["workflow_tree_sha"]
            )
        if feedback_head:
            if feedback_body is not None:
                ctx.github.request(
                    "PATCH", f"pulls/{ctx.event.number}", body={"body": feedback_body}
                )
            _decorate_pull(ctx, ctx.event.number, tree)
            return "Updated draft fix." + ("\n" + template_note if template_note else "")
    if _workflow_tree(ctx) != preflight["workflow_tree_sha"]:
        return "Fix dry-run: workflow tree moved before PR creation."
    body = f"Fixes #{issue}\n\n" + fenced(artifact["summary"] + "\n\n" + artifact["rca"], 8000)
    if template_note:
        body += "\n\n" + template_note
    if find_leaks(body, ctx.secrets, ctx.canary):
        raise Violation("Fix refused: outbound scan alert.")
    pull = ctx.github.request(
        "POST",
        "pulls",
        body={
            "title": f"Fix #{issue}",
            "body": body,
            "head": branch,
            "base": ctx.event.default_branch,
            "draft": True,
        },
    )
    number = pull["number"]
    if type(number) is not int or number < 1:
        raise Violation("Fix refused: invalid pull request.")
    _decorate_pull(ctx, number, tree)
    return "Created draft fix." + ("\n" + template_note if template_note else "")
