import json
import shutil
from pathlib import Path

from agent_repo.config import RepoConfig
from agent_repo.yamljson import load_yaml

OS_ENV = {
    "type": "caller_process",
    "cwd": ".",
    "sandbox": {
        "type": "linux_bwrap",
        "write_paths": ["."],
        "allow_network": False,
        "env_passthrough": [],
    },
}


def render_spec(template: Path, config: RepoConfig, output: Path) -> Path:
    spec = load_yaml(template / "config.yaml")
    if not isinstance(spec, dict):
        raise ValueError("invalid fix agent spec")
    # A drifted template must never widen the agent root or weaken the sandbox (type none).
    if spec.get("os_env") != OS_ENV:
        raise ValueError("fix agent spec must keep cwd . and the linux_bwrap sandbox")
    budget = spec["guardrails"]["policies"]["tool_budget"]["function"]["arguments"]
    budget["limit"] = config.raw["limits"]["fix_max_turns"]
    output.mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML 1.2, so the rendered spec needs no YAML writer.
    (output / "config.yaml").write_text(json.dumps(spec, indent=2) + "\n")
    shutil.copyfile(template / "prompt.md", output / "prompt.md")
    return output
