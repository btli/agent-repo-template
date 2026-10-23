import argparse

from agent_repo.bootstrap import REPOSITORY
from agent_repo.doctor import check


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="doctor")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--app-slug")
    args = parser.parse_args(argv)
    if REPOSITORY.fullmatch(args.repo) is None:
        parser.error("--repo must be OWNER/NAME")
    return check(args.repo, args.app_slug)
