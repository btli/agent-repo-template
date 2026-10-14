import importlib
import re
import sys


def main(argv: list[str]) -> int:
    if not argv or re.fullmatch(r"[a-z_]+", argv[0]) is None:
        print("invalid command", file=sys.stderr)
        return 2
    module_name = f"agent_repo.commands.{argv[0]}"
    try:
        command = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        print("unknown command", file=sys.stderr)
        return 2
    return command.main(argv[1:])
