import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_repo.yamljson import YamlError, load_yaml

ALLOWED_MODEL_EVENTS = {"issues", "issue_comment", "pull_request_target"}
PINNED_USE = re.compile(r"[^\s@]+@[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+\.\d+\s*$")
EVENT_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
EVENT_FIELD = re.compile(r"\bgithub\s*(?:\.\s*event\b|\[\s*['\"]event['\"]\s*\])")
BARE_GITHUB_ARGUMENT = re.compile(r"(?:\(|,)\s*github\s*(?=[,)])")
USES_LINE = re.compile(r"(?:^\s*(?:-\s*)?|\{\s*|,\s*)uses\s*:\s*([^,}\s#]+)")


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _uses_path(step: dict) -> str:
    uses = step.get("uses")
    return uses.split("@", 1)[0] if isinstance(uses, str) else ""


def _action_name(step: dict) -> str:
    return _uses_path(step).rstrip("/").rsplit("/", 1)[-1].casefold()


def _is_model_action(step: dict) -> bool:
    name = _action_name(step)
    inputs = _mapping(step.get("with"))
    if name == "review-agent" and inputs.get("mode") == "apply":
        return False
    return name in {"setup", "decide"} or name.endswith("-agent")


def _is_app_token_action(step: dict) -> bool:
    return _uses_path(step).endswith("/create-github-app-token")


def _is_cache_write(step: dict) -> bool:
    name = _uses_path(step).casefold()
    return name == "actions/cache" or name.endswith("/cache/save") or _has_cache_input(step)


def _is_cache_restore(step: dict) -> bool:
    name = _uses_path(step).casefold()
    return name == "actions/cache" or name.endswith("/cache/restore") or _has_cache_input(step)


def _has_cache_input(step: dict) -> bool:
    inputs = _mapping(step.get("with"))
    if _uses_path(step).casefold() == "astral-sh/setup-uv" and "enable-cache" not in inputs:
        return True
    for name in ("cache", "enable-cache"):
        if name not in inputs:
            continue
        value = inputs[name]
        if isinstance(value, str):
            if value.strip().casefold() not in {"", "false"}:
                return True
        elif bool(value):
            return True
    return False


def _uses_version_comments_present(text: str, uses_values: list[str]) -> bool:
    remaining = list(uses_values)
    run_block_indent = None
    for line in text.splitlines():
        indentation = len(line) - len(line.lstrip())
        if run_block_indent is not None:
            if not line.strip() or indentation > run_block_indent:
                continue
            run_block_indent = None
        if re.match(r"^\s*(?:-\s*)?run\s*:\s*[|>]", line):
            run_block_indent = indentation
            continue
        match = USES_LINE.search(line)
        if match is None:
            continue
        value = match.group(1).strip("'\"")
        try:
            index = remaining.index(value)
        except ValueError:
            continue
        if VERSION_COMMENT.search(line) is None:
            return False
        remaining.pop(index)
    return not remaining


def _events(workflow: dict) -> set[str]:
    trigger = workflow.get("on")
    if isinstance(trigger, str):
        return {trigger}
    if isinstance(trigger, list) and all(isinstance(event, str) for event in trigger):
        return set(trigger)
    if isinstance(trigger, dict):
        return set(trigger)
    return set()


def lint(paths: list[Path]) -> list[str]:
    errors = []
    for path in paths:
        try:
            workflow = load_yaml(path)
        except YamlError as exc:
            errors.append(f"{path}: cannot parse workflow: {exc}")
            continue
        if not isinstance(workflow, dict):
            errors.append(f"{path}: workflow must be a mapping")
            continue

        text = path.read_text(encoding="utf-8")

        events = _events(workflow)
        jobs = _mapping(workflow.get("jobs"))
        parsed_uses = []
        for job_name, raw_job in jobs.items():
            job = _mapping(raw_job)
            job_uses = job.get("uses")
            if isinstance(job_uses, str):
                parsed_uses.append(job_uses)
            if job_uses is not None and (
                not isinstance(job_uses, str) or PINNED_USE.fullmatch(job_uses) is None
            ):
                errors.append(f"{path}: every uses must be SHA-pinned ({job_name})")
            steps = job.get("steps")
            step_mappings = [_mapping(step) for step in steps] if isinstance(steps, list) else []
            for step in step_mappings:
                step_uses = step.get("uses")
                if isinstance(step_uses, str):
                    parsed_uses.append(step_uses)
                if step_uses is not None and (
                    not isinstance(step_uses, str) or PINNED_USE.fullmatch(step_uses) is None
                ):
                    errors.append(f"{path}: every uses must be SHA-pinned ({job_name})")
            model_job = any(_is_model_action(step) for step in step_mappings)
            if model_job and (not events or not events <= ALLOWED_MODEL_EVENTS):
                errors.append(
                    f"{path}: model job trigger must be a subset of "
                    f"{sorted(ALLOWED_MODEL_EVENTS)} ({job_name})"
                )
            if model_job and any(_is_cache_write(step) for step in step_mappings):
                errors.append(f"{path}: model job cache write is forbidden ({job_name})")

            apply_job = any(_is_app_token_action(step) for step in step_mappings)
            if apply_job and model_job:
                errors.append(f"{path}: apply job model action is forbidden ({job_name})")
            if apply_job and any(_is_cache_restore(step) for step in step_mappings):
                errors.append(f"{path}: apply job cache restore is forbidden ({job_name})")
            environment = job.get("environment")
            environment_name = (
                environment.get("name") if isinstance(environment, dict) else environment
            )
            if environment_name == "agent-apply" and events & {"pull_request", "push"}:
                errors.append(
                    f"{path}: agent-apply environment is forbidden on pull_request or push "
                    f"({job_name})"
                )

            for step in step_mappings:
                run = step.get("run")
                if isinstance(run, str) and any(
                    EVENT_FIELD.search(expression) or BARE_GITHUB_ARGUMENT.search(expression)
                    for expression in EVENT_EXPRESSION.findall(run)
                ):
                    errors.append(
                        f"{path}: event expression in run block is forbidden ({job_name})"
                    )

        if not _uses_version_comments_present(text, parsed_uses):
            errors.append(f"{path}: every uses must include a # vX.Y.Z comment")

    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: workflow_lint.py <workflow.yml> [workflow.yml ...]", file=sys.stderr)
        return 2
    errors = lint([Path(value) for value in argv])
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
