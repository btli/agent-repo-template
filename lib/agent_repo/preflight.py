import fnmatch
import re
from dataclasses import dataclass


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
            not isinstance(values, list) or not all(isinstance(item, str) for item in values)
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
    if "push" in events and _branches(events["push"], ctx.agent_ref_glob, glob=True):
        return True
    return any(
        event in events and _branches(events[event], ctx.default_branch, glob=False)
        for event in ("pull_request", "pull_request_review", "pull_request_review_comment")
    )


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


def analyze(workflows: dict[str, object], ctx: PreflightContext) -> list[Exposure]:
    parsed, triggers, reachable = {}, {}, set()
    for path, workflow in sorted(workflows.items()):
        if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
            raise ValueError("invalid workflow jobs")
        if not all(isinstance(job, dict) for job in workflow["jobs"].values()):
            raise ValueError("invalid workflow job")
        parsed[path] = workflow
        triggers[path] = _events(workflow)
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
            config = events.get("workflow_run")
            if config is None:
                continue
            if not isinstance(config, dict):
                raise ValueError("invalid workflow_run trigger")
            patterns = config.get("workflows", [])
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                raise ValueError("invalid workflow_run names")
            if path not in downstream and any(
                fnmatch.fnmatchcase(name, pattern) for name in names for pattern in patterns
            ):
                downstream.add(path)
                changed = True
    exposures = []
    for path, workflow in parsed.items():
        for name, job in workflow["jobs"].items():
            if not _enabled(job):
                continue
            rules = set()
            steps = _steps(job)
            if path in downstream and any(
                str(step.get("uses", "")).lower().startswith("actions/download-artifact@")
                for step in steps
            ):
                rules.add("workflow_run")
            if "pull_request_target" in triggers[path] and any(
                str(step.get("uses", "")).lower().startswith("actions/checkout@")
                and any(
                    re.search(
                        r"github\s*\.\s*event\s*\.\s*pull_request\s*\.\s*head\s*\.",
                        re.sub(r"\[\s*[\"']([A-Za-z_]+)[\"']\s*\]", r".\1", value),
                        re.IGNORECASE,
                    )
                    for value in _strings(step.get("with"))
                )
                for step in steps
            ):
                rules.add("pull_request_target")
            environment = job.get("environment")
            if isinstance(environment, dict):
                environment = environment.get("name")
            if ("push" in triggers[path] or "pull_request" in triggers[path]) and (
                environment == "agent-apply"
            ):
                rules.add("agent-apply")
            if path in reachable:
                permissions = job.get("permissions", workflow.get("permissions"))
                if permissions is None:
                    if not ctx.token_readonly:
                        rules.add("permissions")
                elif permissions == "write-all":
                    rules.update({"permissions", "id-token"})
                elif isinstance(permissions, dict):
                    if permissions.get("id-token") == "write":
                        rules.add("id-token")
                    if any(
                        value == "write" for key, value in permissions.items() if key != "id-token"
                    ):
                        rules.add("permissions")
                elif permissions != "read-all":
                    raise ValueError("invalid workflow permissions")
                if environment not in ctx.safe_environments and (
                    _secrets(job) or _secrets(workflow.get("env"))
                ):
                    rules.add("secrets")
                runner = job.get("runs-on")
                if isinstance(runner, dict) or any(
                    value.lower() == "self-hosted" or "${{" in value for value in _strings(runner)
                ):
                    rules.add("runner")
            for rule in sorted(rules):
                exposures.append(
                    Exposure(
                        path,
                        name,
                        rule,
                        {
                            "permissions": "Job may receive a writable token.",
                            "id-token": "Job may mint an OIDC token.",
                            "secrets": "Job may receive secrets outside a safe environment.",
                            "runner": "Job may run on a private runner.",
                            "workflow_run": "Job downloads artifacts from a reachable workflow.",
                            "pull_request_target": "Job checks out pull request head code.",
                            "agent-apply": "Push or pull request workflow declares agent-apply.",
                        }[rule],
                    )
                )
    return exposures
