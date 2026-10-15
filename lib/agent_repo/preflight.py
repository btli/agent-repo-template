import fnmatch
import re
from dataclasses import dataclass

# https://docs.github.com/actions/reference/runners/github-hosted-runners
# https://docs.github.com/actions/reference/runners/larger-runners
HOSTED_RUNNERS = frozenset(
    {
        "ubuntu-latest",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-26.04",
        "ubuntu-22.04-arm",
        "ubuntu-24.04-arm",
        "ubuntu-26.04-arm",
        "ubuntu-slim",
        "windows-latest",
        "windows-2022",
        "windows-2025",
        "windows-2025-vs2026",
        "windows-11-arm",
        "windows-11-vs2026-arm",
        "macos-latest",
        "macos-14",
        "macos-15",
        "macos-26",
        "macos-15-intel",
        "macos-26-intel",
        "macos-latest-large",
        "macos-14-large",
        "macos-15-large",
        "macos-26-large",
        "macos-latest-xlarge",
        "macos-14-xlarge",
        "macos-15-xlarge",
        "macos-26-xlarge",
        "xcode-27",
        "xcode-27-xlarge",
    }
)
TARGET_EVENTS = frozenset({"pull_request_target", "issue_comment"})


@dataclass(frozen=True)
class Exposure:
    workflow: str
    job: str
    rule: str
    detail: str


@dataclass(frozen=True)
class PreflightContext:
    default_branch: str
    agent_ref_glob: str = "agent/fix-*"
    token_readonly: bool = False
    safe_environments: frozenset[str] = frozenset()


def _glob_tokens(pattern: str) -> list[tuple[str, bool]] | None:
    if any(char in pattern for char in "?[\\+"):
        return None
    tokens = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            recursive = pattern[index : index + 2] == "**"
            tokens.append(("any" if recursive else "segment", True))
            index += 2 if recursive else 1
        else:
            tokens.append((char, False))
            index += 1
    return tokens


def _intersects(left: str, right: str) -> bool:
    def accepts(token, char):
        return token == "any" or (token == "segment" and char != "/") or token == char

    a, b = _glob_tokens(left), _glob_tokens(right)
    if a is None or b is None:
        # Unhandled glob syntax cannot prove that agent branches are excluded.
        return True
    pending, seen = [(0, 0)], set()
    while pending:
        i, j = pending.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and a[i][1]:
            pending.append((i + 1, j))
        if j < len(b) and b[j][1]:
            pending.append((i, j + 1))
        if i < len(a) and j < len(b):
            x, loop_x = a[i]
            y, loop_y = b[j]
            candidates = {x, y, "/", "x"} - {"any", "segment"}
            if any(accepts(x, char) and accepts(y, char) for char in candidates):
                pending.append((i if loop_x else i + 1, j if loop_y else j + 1))
    return False


def _covers(pattern: str, target: str) -> bool:
    if pattern == target or pattern == "**":
        return True
    prefix = re.split(r"[*?\[\\+]", target, maxsplit=1)[0]
    if pattern.endswith("**"):
        fixed = pattern[:-2]
        return not any(char in fixed for char in "*?[\\+") and prefix.startswith(fixed)
    if pattern.endswith("*"):
        fixed = pattern[:-1]
        return (
            not any(char in fixed for char in "*?[\\+")
            and prefix.startswith(fixed)
            and "/" not in target[len(fixed) :]
            and "**" not in target
        )
    return False


def _branches(config: object, target: str, *, glob: bool) -> bool:
    if config is None:
        return True
    if not isinstance(config, dict):
        raise ValueError("invalid trigger configuration")
    patterns = config.get("branches")
    ignored = config.get("branches-ignore")
    if patterns is None and ignored is None:
        return not ("tags" in config or "tags-ignore" in config) if glob else True
    for values in (patterns, ignored):
        if values is not None and (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) for item in values)
        ):
            raise ValueError("invalid branch filters")
    covers = (
        _covers
        if glob
        else lambda pattern, ref: _glob_tokens(pattern) is not None and _intersects(pattern, ref)
    )
    reachable = patterns is None
    for pattern in patterns or []:
        if pattern.startswith("!"):
            if covers(pattern[1:], target):
                reachable = False
        elif _intersects(pattern, target):
            reachable = True
    return reachable and not any(covers(pattern, target) for pattern in ignored or [])


def _events(workflow: dict) -> dict:
    events = workflow.get("on")
    if isinstance(events, str):
        return {events: None}
    if isinstance(events, list) and all(isinstance(event, str) for event in events):
        return dict.fromkeys(events)
    if isinstance(events, dict) and all(isinstance(event, str) for event in events):
        return events
    raise ValueError("invalid workflow triggers")


def _reachable(events: dict, ctx: PreflightContext) -> bool:
    independent = {
        "schedule",
        "workflow_dispatch",
        "issues",
        "release",
        "delete",
        "workflow_call",
        "workflow_run",
        "repository_dispatch",
        "label",
        "milestone",
        "discussion",
        "discussion_comment",
        "gollum",
        "page_build",
        "watch",
        "fork",
        "public",
        "registry_package",
        "project",
        "project_card",
        "project_column",
        "branch_protection_rule",
    }
    # Merge/check/status/deployment events can follow agent work and remain reachable.
    reachable = False
    for event, config in events.items():
        if event == "push":
            if _branches(config, ctx.agent_ref_glob, glob=True):
                reachable = True
        elif event in {"pull_request", "pull_request_review", "pull_request_review_comment"}:
            if _branches(config, ctx.default_branch, glob=False):
                reachable = True
        elif event not in independent and event not in TARGET_EVENTS:
            reachable = True
    return reachable


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _secrets(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("secrets") == "inherit" or any(_secrets(item) for item in value.values())
    elif isinstance(value, list):
        return any(_secrets(item) for item in value)
    elif isinstance(value, str):
        for expression in re.findall(r"\$\{\{(.*?)\}\}", value, re.DOTALL):
            expression = re.sub(
                r"\bsecrets\s*(?:\.\s*GITHUB_TOKEN\b|\[\s*['\"]GITHUB_TOKEN['\"]\s*\])",
                "",
                expression,
                flags=re.IGNORECASE,
            )
            if re.search(r"\bsecrets\b", expression, re.IGNORECASE):
                return True
    return False


def _enabled(job: dict) -> bool:
    condition = job.get("if", True)
    return condition is not False and str(condition).strip() not in {"false", "${{ false }}"}


def _steps(job: dict) -> list:
    steps = job.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ValueError("invalid workflow steps")
    return steps


def _writes(value: object, ctx: PreflightContext) -> frozenset[str]:
    if value is None:
        return frozenset() if ctx.token_readonly else frozenset({"*"})
    if value == "write-all":
        return frozenset({"*", "id-token"})
    if value == "read-all":
        return frozenset()
    if not isinstance(value, dict) or any(
        not isinstance(level, str) or level not in {"read", "write", "none"}
        for level in value.values()
    ):
        raise ValueError("invalid workflow permissions")
    return frozenset(key for key, level in value.items() if level == "write")


def _effective(
    job: dict, workflow: dict, ceiling: frozenset[str] | None, ctx: PreflightContext
) -> frozenset[str]:
    declaration = job.get("permissions", workflow.get("permissions"))
    requested = (
        ceiling if declaration is None and ceiling is not None else _writes(declaration, ctx)
    )
    if ceiling is None:
        return requested
    effective = (requested - {"*", "id-token"}) & (ceiling - {"*", "id-token"})
    if "*" in requested:
        effective |= ceiling - {"id-token"}
    if "*" in ceiling:
        effective |= requested - {"id-token"}
    if "id-token" in requested and "id-token" in ceiling:
        effective |= {"id-token"}
    return frozenset(effective)


def _hosted(runner: object) -> bool:
    if isinstance(runner, list):
        if len(runner) != 1:
            return False
        runner = runner[0]
    return isinstance(runner, str) and runner in HOSTED_RUNNERS


def _context(value: str) -> str:
    value = re.sub(r"\[\s*[\"']([A-Za-z_]+)[\"']\s*\]", r".\1", value)
    return re.sub(r"\s*\.\s*", ".", value).lower()


def _checkout_input_safe(value: object, key: str, ctx: PreflightContext) -> bool:
    if value is None or value == "":
        return True
    if not isinstance(value, str):
        return False
    if key == "ref" and value in {ctx.default_branch, f"refs/heads/{ctx.default_branch}"}:
        return True
    expression = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", value, re.DOTALL)
    if expression is None:
        return False
    safe_contexts = {
        "ref": {
            "github.sha",
            "github.ref",
            "github.ref_name",
            "github.base_ref",
            "github.event.pull_request.base.ref",
            "github.event.pull_request.base.sha",
            "github.event.repository.default_branch",
        },
        "repository": {
            "github.repository",
            "github.event.repository.full_name",
            "github.event.pull_request.base.repo.full_name",
        },
    }
    return _context(expression[1]).strip() in safe_contexts[key]


def _target_unsafe(job: dict, workflow: dict, ctx: PreflightContext) -> bool:
    for value in _strings([job, workflow.get("env"), workflow.get("defaults")]):
        if re.search(
            r"github\.head_ref\b|github\.event\.pull_request\.(?:head\w*|merge_commit_sha\b)"
            r"|context\.payload\.pull_request\.head\w*"
            r"|tojson\s*\(\s*github\.event\.pull_request\s*\)"
            r"|/(?:tarball|zipball)(?:/|\b)|\b(?:refs/)?pull/|\bgh\s+pr\s+checkout\b",
            _context(value),
        ):
            return True
    for step in _steps(job):
        if any(
            re.search(r"github\.event\.pull_request\.(?:title|body)\b", _context(value))
            for value in _strings(step.get("run"))
        ):
            return True
        if str(step.get("uses", "")).lower().startswith("actions/checkout@"):
            inputs = step.get("with")
            if inputs is None:
                inputs = {}
            if not isinstance(inputs, dict):
                return True
            for key in ("ref", "repository"):
                if not _checkout_input_safe(inputs.get(key), key, ctx):
                    return True
    return False


def _downstream_unsafe(job: dict, workflow: dict, writes: frozenset[str]) -> bool:
    steps = _steps(job)
    executable = [
        workflow.get("env"),
        workflow.get("defaults"),
        job.get("env"),
        job.get("defaults"),
        job.get("with"),
        {"secrets": job.get("secrets")},
        [
            {
                key: step[key]
                for key in ("uses", "run", "with", "env", "shell", "working-directory")
                if key in step
            }
            for step in steps
        ],
    ]
    if writes or _secrets(executable):
        return True
    if any(
        re.search(r"github\.event\.workflow_run\.head_\w+", _context(value))
        for value in _strings(executable)
    ):
        return True
    for step in steps:
        uses = step.get("uses", "")
        if isinstance(uses, str) and "download" in uses.split("@")[0].lower():
            return True
        run = step.get("run", "")
        if not isinstance(run, str):
            return True
        if re.search(r"\bgh\s+run\s+download\b", run):
            return True
        if re.search(r"\b(?:gh\s+api|curl|wget)\b", run) and re.search(
            r"actions/(?:runs/[^/\s]+/)?artifacts\b", run
        ):
            return True
    return False


def analyze(workflows: dict[str, object], ctx: PreflightContext) -> list[Exposure]:
    parsed, triggers, reachable = {}, {}, set()
    findings = set()
    for path, workflow in sorted(workflows.items()):
        if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
            raise ValueError("invalid workflow jobs")
        if "name" in workflow and not isinstance(workflow["name"], str):
            raise ValueError("invalid workflow name")
        if not all(isinstance(job, dict) for job in workflow["jobs"].values()):
            raise ValueError("invalid workflow job")
        _writes(workflow.get("permissions"), ctx)
        for job in workflow["jobs"].values():
            _writes(job.get("permissions"), ctx)
        parsed[path] = workflow
        triggers[path] = _events(workflow)
        if "push" in triggers[path] or "pull_request" in triggers[path]:
            for name, job in workflow["jobs"].items():
                environment = job.get("environment")
                if isinstance(environment, dict):
                    environment = environment.get("name")
                if environment == "agent-apply" and _enabled(job):
                    findings.add((path, name, "agent-apply"))
        if _reachable(triggers[path], ctx):
            reachable.add(path)
    downstream = set()
    changed = True
    while changed:
        changed = False
        names = {
            name
            for path in reachable | downstream
            for name in (parsed[path].get("name", path), f".github/workflows/{path}")
        }
        for path, events in triggers.items():
            if "workflow_run" not in events:
                continue
            config = events["workflow_run"]
            if not isinstance(config, dict):
                raise ValueError("invalid workflow_run trigger")
            patterns = config.get("workflows", [])
            if (
                not isinstance(patterns, list)
                or not patterns
                or not all(isinstance(pattern, str) for pattern in patterns)
            ):
                raise ValueError("invalid workflow_run names")
            if not _branches(config, "agent/**", glob=True):
                continue
            if path not in downstream and any(
                fnmatch.fnmatchcase(name, pattern) for name in names for pattern in patterns
            ):
                downstream.add(path)
                changed = True
    targets = {path for path in parsed if TARGET_EVENTS.intersection(triggers[path])}
    pending = [
        (path, None, path in downstream, path in targets, path in reachable, (path,))
        for path in sorted(reachable | downstream | targets)
    ]
    seen = set()
    while pending:
        path, ceiling, is_downstream, is_target, is_reachable, active = pending.pop()
        state = (path, ceiling, is_downstream, is_target, is_reachable)
        if state in seen:
            continue
        seen.add(state)
        workflow = parsed[path]
        for name, job in workflow["jobs"].items():
            if not _enabled(job):
                continue
            rules = set()
            _steps(job)
            writes = _effective(job, workflow, ceiling, ctx)
            if is_downstream:
                if _downstream_unsafe(job, workflow, writes):
                    rules.add("workflow_run")
            elif is_reachable:
                if writes - {"id-token"}:
                    rules.add("permissions")
                if "id-token" in writes:
                    rules.add("id-token")
            if is_target and _target_unsafe(job, workflow, ctx):
                rules.add("pull_request_target")
            environment = job.get("environment")
            if isinstance(environment, dict):
                environment = environment.get("name")
            if (
                is_reachable
                and not is_downstream
                and environment not in ctx.safe_environments
                and (_secrets(job) or _secrets(workflow.get("env")))
            ):
                rules.add("secrets")
            if "uses" in job:
                uses = job["uses"]
                match = (
                    re.fullmatch(r"\./\.github/workflows/([^/]+\.ya?ml)", uses)
                    if (isinstance(uses, str))
                    else None
                )
                callee = match[1] if match else None
                if callee not in parsed or "workflow_call" not in triggers[callee]:
                    rules.add("reusable_workflow")
                elif callee in active:
                    rules.add("reusable_workflow")
                else:
                    pending.append(
                        (callee, writes, is_downstream, is_target, is_reachable, (*active, callee))
                    )
            elif (is_reachable or is_downstream) and not _hosted(job.get("runs-on")):
                rules.add("runner")
            for rule in rules:
                findings.add((path, name, rule))
    details = {
        "permissions": "Job may receive a writable token.",
        "id-token": "Job may mint an OIDC token.",
        "secrets": "Job may receive secrets outside a safe environment.",
        "runner": "Job cannot be proven to run on a hosted runner.",
        "workflow_run": "Downstream job may consume agent content or privileged credentials.",
        "pull_request_target": "Job cannot be proven to use only trusted base code and inputs.",
        "agent-apply": "Push or pull request workflow declares agent-apply.",
        "reusable_workflow": "Reusable workflow cannot be verified locally.",
    }
    return [Exposure(path, name, rule, details[rule]) for path, name, rule in sorted(findings)]
