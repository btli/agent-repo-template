import argparse
import re
from pathlib import Path

from agent_repo.bootstrap import (
    REPOSITORY,
    SHA,
    VERSION,
    BootstrapError,
    Gh,
    InstallOptions,
    Journal,
    app_link,
    execute,
    journal_path,
    plan_install,
    plan_sync_models,
)
from agent_repo.doctor import check


def _app_link(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap.sh app-link")
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.owner) is None:
        parser.error("--owner must be a GitHub account name")
    try:
        url = app_link(Gh(), args.owner)
    except BootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1
    print("Register the GitHub App (no webhook; union of the agent role permissions) at:")
    print(url)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["app-link"]:
        return _app_link(argv[1:])
    parser = argparse.ArgumentParser(prog="bootstrap.sh")
    parser.add_argument("--repo", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--sync-models", action="store_true")
    pin = parser.add_mutually_exclusive_group()
    pin.add_argument("--ref")
    pin.add_argument("--candidate")
    parser.add_argument("--version")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--gateway-url")
    parser.add_argument("--gateway-kind")
    parser.add_argument("--system-one-url")
    parser.add_argument("--app-slug")
    parser.add_argument("--app-client-id")
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args(argv)
    if REPOSITORY.fullmatch(args.repo) is None:
        parser.error("--repo must be OWNER/NAME")
    if args.check:
        return check(args.repo, args.app_slug, args.app_client_id)
    journal = Journal(args.journal or journal_path(args.repo))
    try:
        if args.sync_models:
            plan = plan_sync_models(Gh(), args.repo)
        else:
            if args.ref is None and args.candidate is None:
                parser.error("pass --ref vX.Y.Z, or --candidate <sha> --version vX.Y.Z")
            if args.ref is not None and args.version is not None:
                parser.error("--version goes with --candidate")
            if args.candidate is not None and (
                SHA.fullmatch(args.candidate) is None
                or args.version is None
                or VERSION.fullmatch(args.version) is None
            ):
                parser.error("--candidate needs a 40-hex commit and --version vX.Y.Z")
            options = InstallOptions(
                args.repo,
                args.ref,
                args.candidate,
                args.ref or args.version,
                args.force,
                args.gateway_url,
                args.gateway_kind,
                args.system_one_url,
                args.app_slug,
                args.app_client_id,
            )
            plan = plan_install(Gh(), options)
    except BootstrapError as exc:
        print(f"ERROR: {exc}")
        print("Bootstrap made no changes.")
        return 1
    return execute(plan, args.repo, preview=args.plan, journal=journal)
