import sys
from pathlib import Path

from agent_repo.config import ConfigError, load_config


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_config <agents-dir>", file=sys.stderr)
        return 2
    try:
        load_config(Path(argv[0]))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Configuration is valid.")
    return 0
