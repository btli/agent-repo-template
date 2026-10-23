import fnmatch
import re
from dataclasses import dataclass

# Verified 2026-10-15: https://docs.github.com/actions/reference/runners/github-hosted-runners
# Verified 2026-10-15: https://docs.github.com/actions/reference/runners/larger-runners
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
SAFE_EVENTS = frozenset(
    {
        # GitHub "Events that trigger workflows", schedule: cron runs only on the default branch.
        "schedule",
        # GitHub REST "Create a workflow dispatch event": API tokens require Actions write.
        "workflow_dispatch",
    }
)
# Other target events retain unknown branch provenance for downstream filtering.
DEFAULT_REF_EVENTS = frozenset(
    {
        "pull_request_target",
        "issue_comment",
        "issues",
        "discussion",
        "discussion_comment",
        "repository_dispatch",
        "label",
        "milestone",
        "release",
        "delete",
    }
)
AGENT_REF_EVENTS = frozenset(
    {
        "push",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "merge_group",
    }
)
# Deliberately minimal: only these pinned metadata actions may use target-event credentials.
TARGET_ACTIONS = frozenset({"actions/labeler", "actions/first-interaction", "actions/stale"})
TEMPLATE_ACTIONS = frozenset(
    {"gate", "decide", "triage-agent", "review-agent", "apply", "preflight", "setup"}
)
MODEL_ACTIONS = frozenset({"triage-agent", "review-agent", "fix-agent"})
ROLE_KEYS = frozenset({"AGENT_TRIAGE_KEY", "AGENT_REVIEW_KEY", "AGENT_FIX_KEY", "*"})
TEMPLATE_PREFIX = "btli/agent-repo-template/actions/"
FIX_RULE = "template-fix-propose"


@dataclass(frozen=True)
class Exposure:
    workflow: str
    job: str
    rule: str
    detail: str
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightContext:
    default_branch: str
    agent_ref_glob: str = "agent/fix-*"
    token_readonly: bool = False
    safe_environments: frozenset[str] = frozenset()
    template_ref: str | None = None


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
    patterns = [patterns] if isinstance(patterns, str) else patterns
    ignored = [ignored] if isinstance(ignored, str) else ignored
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


def _target_events(events: dict) -> set[str]:
    return events.keys() - SAFE_EVENTS - AGENT_REF_EVENTS - {"workflow_call", "workflow_run"}


def _reachable_refs(events: dict, ctx: PreflightContext) -> set[str | None]:
    refs = set()
    for event, config in events.items():
        if event == "push":
            if _branches(config, ctx.agent_ref_glob, glob=True):
                refs.add(ctx.agent_ref_glob)
        elif event == "pull_request":
            if _branches(config, ctx.default_branch, glob=False):
                # Fork PR authors choose the head branch independently of the agent glob.
                refs.update({ctx.agent_ref_glob, None})
        elif event in {"pull_request_review", "pull_request_review_comment"}:
            refs.update({ctx.agent_ref_glob, None})
        elif event == "merge_group":
            refs.add("gh-readonly-queue/**")
    return refs


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _expressions(value: str):
    start = value.find("${{")
    while start != -1:
        begin = index = start + 3
        quoted = False
        while index < len(value):
            if value[index] == "'":
                if quoted and value[index : index + 2] == "''":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted and value[index : index + 2] == "}}":
                break
            index += 1
        # An unfinished expression includes the remaining scalar for fail-closed scans.
        yield value[begin:index]
        start = value.find("${{", index + 2)


def _secrets(
    value: object, *, include_token: bool = False, grants: frozenset[str] | None = None
) -> bool:
    if isinstance(value, dict):
        return (value.get("secrets") == "inherit" and grants != frozenset()) or any(
            _secrets(item, include_token=include_token, grants=grants) for item in value.values()
        )
    elif isinstance(value, list):
        return any(_secrets(item, include_token=include_token, grants=grants) for item in value)
    elif isinstance(value, str):
        for expression in _expressions(value):
            if not include_token:
                expression = re.sub(
                    r"\bsecrets\s*(?:\.\s*GITHUB_TOKEN\b|\[\s*['\"]GITHUB_TOKEN['\"]\s*\])",
                    "",
                    expression,
                    flags=re.IGNORECASE,
                )
            if grants is not None:
                if not grants:
                    continue
                expression = re.sub(
                    r"\bsecrets\.([a-z_][a-z_0-9]*)",
                    lambda match: match[0] if match[1] in grants else "",
                    _context(expression),
                )
            if re.search(r"\bsecrets\b", expression, re.IGNORECASE):
                return True
    return False


def _job_secrets(job: dict, workflow: dict, grants: frozenset[str] | None) -> bool:
    # A callee's environment supplies its own secrets independently of caller grants.
    return _secrets(job, grants=None if job.get("environment") else grants) or _secrets(
        workflow.get("env"), grants=grants
    )


def _secret_names(
    value: object, grants: frozenset[str] | None = None, *, include_token: bool = False
) -> set[str]:
    names = set()
    if isinstance(value, dict):
        if value.get("secrets") == "inherit":
            if grants is None:
                names.add("*")
            else:
                names.update(name.upper() for name in grants)
        for child in value.values():
            names.update(_secret_names(child, grants, include_token=include_token))
    elif isinstance(value, list):
        for child in value:
            names.update(_secret_names(child, grants, include_token=include_token))
    elif isinstance(value, str):
        for expression in _expressions(value):
            if not _secrets("${{" + expression + "}}", grants=grants, include_token=include_token):
                continue
            normalized = _context(expression)
            matches = list(re.finditer(r"\bsecrets\.([a-z_][a-z_0-9]*)", normalized))
            names.update(
                match[1].upper()
                for match in matches
                if (include_token or match[1] != "github_token")
                and (grants is None or match[1] in grants)
            )
            remainder = re.sub(r"\bsecrets\.[a-z_][a-z_0-9]*", "", normalized)
            if re.search(r"\bsecrets\b", remainder):
                names.add("*")
    return names


def _callee_grants(job: dict, grants: frozenset[str] | None) -> frozenset[str] | None:
    passed = job.get("secrets", {})
    if passed == "inherit":
        return grants
    if not isinstance(passed, dict):
        raise ValueError("invalid reusable workflow secrets")
    return frozenset(
        name.lower()
        for name, value in passed.items()
        if _secrets(value, grants=grants)
        or (
            not _secrets(value, include_token=True)
            and not (
                isinstance(value, str)
                and re.fullmatch(r"\s*\$\{\{\s*github\.token\s*\}\}\s*", _context(value))
            )
        )
    )


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


def _hosted(runner: object) -> bool:
    if isinstance(runner, list):
        if len(runner) != 1:
            return False
        runner = runner[0]
    return isinstance(runner, str) and runner in HOSTED_RUNNERS


def _context(value: str) -> str:
    value = re.sub(r"\[\s*[\"']([A-Za-z_][A-Za-z_0-9]*)[\"']\s*\]", r".\1", value)
    return re.sub(r"\s*\.\s*", ".", value).lower()


def _executable(job: dict, workflow: dict) -> list:
    return [
        workflow.get("env"),
        workflow.get("defaults"),
        job.get("env"),
        job.get("defaults"),
        job.get("with"),
        job.get("container"),
        job.get("services"),
        job.get("strategy"),
        [other.get("outputs") for other in workflow["jobs"].values()],
        {"secrets": job.get("secrets")},
        [
            {
                key: step[key]
                for key in ("uses", "run", "with", "env", "if", "shell", "working-directory")
                if key in step
            }
            for step in _steps(job)
        ],
    ]


def _head_reference(value: str) -> bool:
    if re.search(r"\bGITHUB_HEAD_REF\b", value):
        return True
    contexts = []
    for expression in _expressions(value):
        contexts.append(_context(expression))
        value = value.replace("${{" + expression + "}}", "ref")
    contexts.append(_context(value))
    return any(
        re.search(
            r"github\.(?:event\.pull_request\.(?:head\b|merge_commit_sha\b)|head_ref\b)"
            r"|(?:refs/)?pull/[^\s/]+/(?:head|merge)\b|\bgh\s+pr\s+checkout\b"
            r"|\bpulls/|\.head\.(?:sha|ref)\b",
            context,
        )
        for context in contexts
    )


def _safe_action(step: dict) -> bool:
    uses = step.get("uses")
    match = re.fullmatch(r"([^@]+)@([0-9a-fA-F]{40})", uses) if isinstance(uses, str) else None
    return "run" not in step and match is not None and match[1].lower() in TARGET_ACTIONS


def _pinned_action(step: dict) -> tuple[str, str]:
    uses = step.get("uses")
    match = (
        re.fullmatch(r"([^/@]+)/([^@]+)@([0-9a-fA-F]{40})", uses) if isinstance(uses, str) else None
    )
    if match is None or "run" in step:
        return "", ""
    return f"{match[1].casefold()}/{match[2]}", match[3].lower()


def _action_inputs(step: dict) -> dict | None:
    inputs = step.get("with", {})
    if not isinstance(inputs, dict) or not all(isinstance(key, str) for key in inputs):
        return None
    normalized = {key.casefold(): value for key, value in inputs.items()}
    return normalized if len(normalized) == len(inputs) else None


def _role_keys_scoped(job: dict, workflow: dict) -> bool:
    if (
        _secret_names(
            [workflow.get("env"), {key: value for key, value in job.items() if key != "steps"}]
        )
        & ROLE_KEYS
    ):
        return False
    model_steps = [
        step
        for step in _steps(job)
        if _pinned_action(step)[0] in {TEMPLATE_PREFIX + name for name in MODEL_ACTIONS}
        and (_action_inputs(step) or {}).get("mode", "propose") == "propose"
    ]
    for step in _steps(job):
        if not _secret_names(step) & ROLE_KEYS:
            continue
        if len(model_steps) != 1 or step is not model_steps[0]:
            return False
        model = _pinned_action(step)[0].removeprefix(TEMPLATE_PREFIX).removesuffix("-agent")
        if _secret_names(step) & ROLE_KEYS != {f"AGENT_{model.upper()}_KEY"}:
            return False
        if _secret_names({key: value for key, value in step.items() if key != "env"}) & ROLE_KEYS:
            return False
    return True


def _safe_target_action(step: dict, previous: list[dict]) -> bool:
    inputs = _action_inputs(step)
    if inputs is None:
        return False
    if _safe_action(step):
        return True
    action, _ = _pinned_action(step)
    if (
        action.startswith(TEMPLATE_PREFIX)
        and action.removeprefix(TEMPLATE_PREFIX) in TEMPLATE_ACTIONS
    ):
        name = action.removeprefix(TEMPLATE_PREFIX)
        if "ref" in inputs or "repository" in inputs:
            return False
        for key, value in inputs.items():
            if "token" not in key.casefold():
                continue
            if key != "token":
                return False
            trusted_tokens = {"${{ github.token }}"}
            if name == "apply" or (name == "review-agent" and inputs.get("mode") == "apply"):
                trusted_tokens.update(
                    "${{ steps." + prior["id"] + ".outputs.token }}"
                    for prior in previous
                    if _pinned_action(prior)[0] == "actions/create-github-app-token"
                    and isinstance(prior.get("id"), str)
                )
            if value not in trusted_tokens:
                return False
        if "default-branch" in inputs and (
            name != "decide"
            or inputs["default-branch"] != "${{ github.event.repository.default_branch }}"
        ):
            return False
        if name == "setup":
            return not any(_secret_names(prior) for prior in [*previous, step])
        return True
    if action == "actions/checkout":
        return (
            (
                "ref" not in inputs
                or inputs["ref"]
                in {"${{ github.sha }}", "${{ github.event.repository.default_branch }}"}
            )
            and "repository" not in inputs
            and inputs.get("persist-credentials") is False
        )
    if action == "actions/download-artifact":
        return not {"run-id", "github-token"} & inputs.keys()
    return action in {"actions/upload-artifact", "actions/create-github-app-token"}


def _template_fix_propose(name: str, job: dict, workflow: dict, ctx: PreflightContext) -> bool:
    if (
        name != "propose"
        or not isinstance(ctx.template_ref, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", ctx.template_ref)
    ):
        return False
    if not _events(workflow).keys() <= {"issues", "issue_comment"} or any(
        key in workflow for key in ("env", "defaults")
    ):
        return False
    gate = {
        "runs-on": "ubuntu-24.04",
        "outputs": {
            key: "${{ steps.gate.outputs." + key + " }}"
            for key in ("run", "issue", "base_sha", "head_sha", "head_ref")
        },
        "steps": [
            {
                "id": "gate",
                "uses": TEMPLATE_PREFIX + "gate@" + ctx.template_ref.lower(),
                "with": {
                    "workflow": "fix",
                    "enabled": "${{ vars.AGENTS_ENABLED }}",
                    "bot-login": "${{ vars.AGENT_BOT_LOGIN }}",
                },
            }
        ],
    }
    if workflow.get("permissions") != {"contents": "read"} or workflow["jobs"].get("gate") != gate:
        return False
    expected = {
        "needs": "gate",
        "if": "needs.gate.outputs.run == 'true'",
        "runs-on": "ubuntu-24.04",
        "timeout-minutes": 30,
        "environment": "agent-propose-fix",
        "permissions": {"contents": "read"},
        "concurrency": {
            "group": "agent-fix-propose-${{ needs.gate.outputs.issue }}",
            "cancel-in-progress": True,
        },
    }
    if {key: value for key, value in job.items() if key != "steps"} != expected:
        return False
    if any(_head_reference(value) for value in _strings(_executable(job, workflow))):
        return False
    steps = _steps(job)
    actions = [
        "actions/checkout",
        "actions/checkout",
        "actions/download-artifact",
        TEMPLATE_PREFIX + "setup",
        TEMPLATE_PREFIX + "fix-agent",
        "actions/upload-artifact",
    ]
    if len(steps) != len(actions) or [_pinned_action(step)[0] for step in steps] != actions:
        return False
    if any(
        _action_inputs(step) is None or step.get("with", {}) != _action_inputs(step)
        for step in steps
    ):
        return False
    if any(step.keys() - {"name", "uses", "with", "env"} for step in steps):
        return False
    if any("env" in step for index, step in enumerate(steps) if index != 4):
        return False
    if any("token" in key.casefold() for step in steps for key in step.get("with", {})):
        return False
    if any(_pinned_action(steps[index])[1] != ctx.template_ref.lower() for index in (3, 4)):
        return False
    if steps[4].get("env") != {"AGENT_FIX_KEY": "${{ secrets.AGENT_FIX_KEY }}"}:
        return False
    if _secret_names(job, include_token=True) != {"AGENT_FIX_KEY"} or not _role_keys_scoped(
        job, workflow
    ):
        return False
    for index, step in enumerate(steps):
        if index != 4 and not _safe_target_action(step, steps[:index]):
            return False
    policy, work = steps[0].get("with"), steps[1].get("with")
    if policy != {"ref": "${{ github.sha }}", "path": "policy", "persist-credentials": False}:
        return False
    if work != {
        "ref": "${{ github.sha }}",
        "path": "work",
        "fetch-depth": 0,
        "persist-credentials": False,
    }:
        return False
    if steps[3].get("with") != {
        "gateway-url": "${{ vars.AGENT_GATEWAY_URL }}",
        "gateway-kind": "${{ vars.AGENT_GATEWAY_KIND }}",
        "model": "${{ vars.AGENT_FIX_MODEL }}",
        "api-key-env": "AGENT_FIX_KEY",
    }:
        return False
    if steps[2].get("with") != {
        "name": "agent-fix-context-${{ github.run_id }}",
        "path": "${{ runner.temp }}/agent-fix/context",
    } or steps[5].get("with") != {
        "name": "agent-fix-${{ github.run_id }}",
        "path": "${{ runner.temp }}/agent-fix/proposal/fix.json\n"
        "${{ runner.temp }}/agent-fix/proposal/fix.patch\n",
        "if-no-files-found": "error",
        "retention-days": 1,
    }:
        return False
    inputs = steps[4].get("with", {})
    fixed_inputs = {
        "work-path": "work",
        "policy-path": "policy",
        "base-sha": "${{ needs.gate.outputs.base_sha }}",
        "feedback-head-sha": "${{ needs.gate.outputs.head_sha }}",
        "feedback-head-ref": "${{ needs.gate.outputs.head_ref }}",
        "api-key-env": "AGENT_FIX_KEY",
        "allowed-endpoints": "${{ vars.AGENT_FIX_ALLOWED_ENDPOINTS }}",
        "issue-file": "${{ runner.temp }}/agent-fix/context/issue.txt",
        "output-dir": "${{ runner.temp }}/agent-fix/proposal",
    }
    if inputs.keys() != fixed_inputs.keys() | {"agent-timeout"} or any(
        inputs[key] != value for key, value in fixed_inputs.items()
    ):
        return False
    timeout = inputs["agent-timeout"]
    return (
        isinstance(timeout, int)
        and not isinstance(timeout, bool)
        and 0 < timeout <= 1500
        and isinstance(inputs["issue-file"], str)
        and bool(inputs["issue-file"])
        and isinstance(inputs["output-dir"], str)
        and bool(inputs["output-dir"])
    )


def _target_unsafe(
    job: dict,
    workflow: dict,
    writes: frozenset[str],
    grants: frozenset[str] | None,
) -> bool:
    if any(_head_reference(value) for value in _strings(_executable(job, workflow))):
        return True
    # Target events run on the base branch, outside agent-branch environment exclusions.
    privileged = writes or _job_secrets(job, workflow, grants)
    if not privileged:
        return False
    steps = _steps(job)
    if not steps or "container" in job or "services" in job:
        return True
    if _secret_names([workflow.get("env"), job.get("env")]) and any(
        _pinned_action(step)[0] == TEMPLATE_PREFIX + "setup" for step in steps
    ):
        return True
    return not _role_keys_scoped(job, workflow) or any(
        not _safe_target_action(step, steps[:index]) for index, step in enumerate(steps)
    )


def _downstream_unsafe(
    job: dict, workflow: dict, writes: frozenset[str], grants: frozenset[str] | None
) -> bool:
    return bool(
        writes
        or _job_secrets(job, workflow, grants)
        or any(not _safe_action(step) for step in _steps(job))
    )


def analyze(workflows: dict[str, object], ctx: PreflightContext) -> list[Exposure]:
    parsed, triggers, reachable = {}, {}, set()
    refs = {}
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
        refs[path] = _reachable_refs(triggers[path], ctx)
        if refs[path]:
            reachable.add(path)
        # Fork PRs can produce artifacts from any base branch.
        if "pull_request" in triggers[path]:
            refs[path].add(None)
        # Read-only target jobs can still produce artifacts containing PR code.
        refs[path].update(
            ctx.default_branch if event in DEFAULT_REF_EVENTS else None
            for event in _target_events(triggers[path])
        )

    def lint_agent_apply(path, active):
        for name, job in parsed[path]["jobs"].items():
            if not _enabled(job):
                continue
            environment = job.get("environment")
            if isinstance(environment, dict):
                environment = environment.get("name")
            if isinstance(environment, str) and (
                environment.casefold() == "agent-apply" or "${{" in environment
            ):
                findings.add((path, name, "agent-apply"))
            uses = job.get("uses")
            if isinstance(uses, str) and uses.startswith("./"):
                local = re.fullmatch(r"\./\.github/workflows/([^/]+\.ya?ml)", uses)
                callee = local[1] if local else None
                if (
                    callee not in parsed
                    or "workflow_call" not in triggers[callee]
                    or callee in active
                    or len(active) > 10
                ):
                    findings.add((path, name, "agent-apply"))
                else:
                    lint_agent_apply(callee, (*active, callee))

    for path in parsed:
        if "push" in triggers[path] or "pull_request" in triggers[path]:
            lint_agent_apply(path, (path,))

    downstream = set()
    changed = True
    while changed:
        changed = False
        for path, events in triggers.items():
            if "workflow_run" not in events:
                continue
            config = events["workflow_run"]
            if not isinstance(config, dict):
                raise ValueError("invalid workflow_run trigger")
            patterns = config.get("workflows", [])
            patterns = [patterns] if isinstance(patterns, str) else patterns
            if (
                not isinstance(patterns, list)
                or not patterns
                or not all(isinstance(pattern, str) for pattern in patterns)
            ):
                raise ValueError("invalid workflow_run names")
            if path not in downstream and any(
                (
                    name.casefold() == pattern.casefold()
                    or fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
                )
                and any(ref is None or _branches(config, ref, glob=True) for ref in refs[source])
                for source in parsed
                for name in (parsed[source].get("name", source), f".github/workflows/{source}")
                for pattern in patterns
            ):
                downstream.add(path)
                refs[path].add(ctx.default_branch)
                changed = True
    targets = {path for path in parsed if _target_events(triggers[path])}

    def visit(path, events, is_reachable, is_downstream, active, ceiling=None, grants=None):
        is_target = bool(_target_events(events))
        workflow = parsed[path]
        for name, job in workflow["jobs"].items():
            if not _enabled(job):
                continue
            rules = set()
            _steps(job)
            declaration = job.get("permissions", workflow.get("permissions"))
            writes = (
                frozenset({"*"})
                if declaration is None and "pull_request_target" in events
                else _writes(declaration, ctx)
            )
            if ceiling is not None:
                if declaration is None:
                    writes = ceiling
                else:
                    limited = writes if "*" in ceiling else writes & ceiling
                    if "*" in writes:
                        limited |= ceiling - {"id-token"}
                    writes = limited if "id-token" in ceiling else limited - {"id-token"}
            if is_downstream:
                if _downstream_unsafe(job, workflow, writes, grants):
                    rules.add("workflow_run")
            elif is_reachable:
                if writes - {"id-token"}:
                    rules.add("permissions")
                if "id-token" in writes:
                    rules.add("id-token")
            if is_target and _target_unsafe(job, workflow, writes, grants):
                rules.add(
                    FIX_RULE
                    if not is_reachable
                    and not is_downstream
                    and _template_fix_propose(name, job, workflow, ctx)
                    else "pull_request_target"
                )
            environment = job.get("environment")
            if isinstance(environment, dict):
                environment = environment.get("name")
            if (
                is_reachable
                and not is_downstream
                and environment not in ctx.safe_environments
                and _job_secrets(job, workflow, grants)
            ):
                rules.add("secrets")
            if "uses" in job:
                if writes or _job_secrets(job, workflow, grants):
                    rules.add("reusable_workflow")
                uses = job["uses"]
                local = (
                    re.fullmatch(r"\./\.github/workflows/([^/]+\.ya?ml)", uses)
                    if isinstance(uses, str)
                    else None
                )
                callee = local[1] if local else None
                if (
                    callee not in parsed
                    or "workflow_call" not in triggers[callee]
                    or callee in active
                    or len(active) > 10
                ):
                    rules.add("runner")
                else:
                    visit(
                        callee,
                        events,
                        is_reachable,
                        is_downstream,
                        (*active, callee),
                        writes,
                        _callee_grants(job, grants),
                    )
            elif (is_reachable or is_downstream or is_target) and not _hosted(job.get("runs-on")):
                rules.add("runner")
            for rule in rules:
                findings.add((path, name, rule))

    for path in sorted(reachable | downstream | targets):
        visit(path, triggers[path], path in reachable, path in downstream, (path,))
    details = {
        "permissions": "Job may receive a writable token.",
        "id-token": "Job may mint an OIDC token.",
        "secrets": "Job may receive secrets outside a safe environment.",
        "runner": "Job cannot be proven to run on a hosted runner.",
        "workflow_run": "Downstream job may consume agent content or privileged credentials.",
        "pull_request_target": "Job {job!r} may expose target-event credentials. "
        "Use the logged fix.accept_ci_exposure override to accept this exposure.",
        "agent-apply": "Push or pull request workflow declares agent-apply.",
        "reusable_workflow": "Reusable caller may expose privileged credentials.",
        FIX_RULE: "Canonical template fix job accepts exposure of AGENT_FIX_KEY.",
    }
    return [
        Exposure(
            path,
            name,
            rule,
            details[rule].format(job=name),
            tuple(
                sorted(
                    _secret_names(parsed[path]["jobs"][name])
                    | _secret_names(parsed[path].get("env"))
                )
            ),
        )
        for path, name, rule in sorted(findings)
    ]
