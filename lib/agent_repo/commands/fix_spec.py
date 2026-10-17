import json
import shutil
import sys
from pathlib import Path

from agent_repo.config import ConfigError, RepoConfig, load_config
from agent_repo.yamljson import YamlError, load_yaml

SANDBOX = {
    "type": "linux_bwrap",
    "write_paths": ["."],
    "allow_network": False,
    "env_passthrough": [],
}


def render_spec(template: Path, config: RepoConfig, output: Path) -> Path:
    spec = load_yaml(template / "config.yaml")
    if not isinstance(spec, dict):
        raise ValueError("invalid fix agent spec")
    # A drifted template must never run with a weaker sandbox, least of all type none.
    os_env = spec.get("os_env")
    if not isinstance(os_env, dict) or os_env.get("sandbox") != SANDBOX:
        raise ValueError("fix agent spec must use the linux_bwrap sandbox")
    budget = spec["guardrails"]["policies"]["tool_budget"]["function"]["arguments"]
    budget["limit"] = config.raw["limits"]["fix_max_turns"]
    output.mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML 1.2, so the rendered spec needs no YAML writer.
    (output / "config.yaml").write_text(json.dumps(spec, indent=2) + "\n")
    shutil.copyfile(template / "prompt.md", output / "prompt.md")
    return output


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: fix_spec <trusted-agents-dir> <template-dir> <output-dir>", file=sys.stderr)
        return 2
    try:
        render_spec(Path(argv[1]), load_config(Path(argv[0])), Path(argv[2]))
    except (OSError, KeyError, TypeError, ValueError, ConfigError, YamlError):
        print("fix spec rendering failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
