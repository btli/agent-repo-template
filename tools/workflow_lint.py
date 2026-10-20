import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_repo.yamljson import YamlError, load_yaml

ALLOWED_MODEL_EVENTS = {"issues", "issue_comment", "pull_request_target"}
PIN = re.compile(r"[^\s@]+@([0-9a-f]{40})\s+#\s+v\d+\.\d+\.\d+")
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses\s*:\s*(.*?)\s*$")
EVENT_EXPRESSION = re.compile(r"\$\{\{\s*github\.event\.")


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _action_name(step: dict) -> str:
    uses = step.get("uses")
    if not isinstance(uses, str):
        return ""
    return uses.split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1].casefold()


def _is_model_action(step: dict) -> bool:
    name = _action_name(step)
    inputs = _mapping(step.get("with"))
    if name == "review-agent" and inputs.get("mode") == "apply":
        return False
    return name in {"setup", "decide"} or name.endswith("-agent")


def _is_app_token_action(step: dict) -> bool:
    uses = step.get("uses")
    return isinstance(uses, str) and uses.split("@", 1)[0].endswith("/create-github-app-token")


def _cache_name(step: dict) -> str:
    uses = step.get("uses")
    if not isinstance(uses, str):
        return ""
    return uses.split("@", 1)[0].casefold()


def _is_cache_write(step: dict) -> bool:
    name = _cache_name(step)
    return name == "actions/cache" or name.endswith("/cache/save")


def _is_cache_restore(step: dict) -> bool:
    name = _cache_name(step)
    return name == "actions/cache" or name.endswith("/cache/restore")


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
        for line_number, line in enumerate(text.splitlines(), start=1):
            uses_line = USES_LINE.match(line)
            if uses_line and PIN.fullmatch(uses_line.group(1)) is None:
                errors.append(f"{path}:{line_number}: every uses must be SHA-pinned")

        events = _events(workflow)
        jobs = _mapping(workflow.get("jobs"))
        for job_name, raw_job in jobs.items():
            job = _mapping(raw_job)
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            step_mappings = [_mapping(step) for step in steps]
            model_steps = [step for step in step_mappings if _is_model_action(step)]
            if model_steps and (not events or not events <= ALLOWED_MODEL_EVENTS):
                errors.append(
                    f"{path}: model job trigger must be a subset of "
                    f"{sorted(ALLOWED_MODEL_EVENTS)} ({job_name})"
                )
            if model_steps and any(_is_cache_write(step) for step in step_mappings):
                errors.append(f"{path}: model job cache write is forbidden ({job_name})")

            apply_job = any(_is_app_token_action(step) for step in step_mappings)
            if apply_job and any(_is_model_action(step) for step in step_mappings):
                errors.append(f"{path}: apply job model action is forbidden ({job_name})")
            if apply_job and any(_is_cache_restore(step) for step in step_mappings):
                errors.append(f"{path}: apply job cache restore is forbidden ({job_name})")
            if job.get("environment") == "agent-apply" and events & {"pull_request", "push"}:
                errors.append(
                    f"{path}: agent-apply environment is forbidden on pull_request or push "
                    f"({job_name})"
                )

            for step in step_mappings:
                run = step.get("run")
                if isinstance(run, str) and EVENT_EXPRESSION.search(run):
                    errors.append(
                        f"{path}: event expression in run block is forbidden ({job_name})"
                    )

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
