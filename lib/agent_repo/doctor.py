"""Diagnose an installed repository and its consumer-CI exposure with read calls only."""

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent_repo.bootstrap import (
    ENVIRONMENTS,
    MODEL_VARIABLES,
    PIN,
    RULESET_NAMES,
    SHA,
    UPSTREAM,
    App,
    BootstrapError,
    Gh,
    GhError,
    Target,
    _field,
    _quote,
    environment_is_safe,
    file_content,
    gateway_endpoint,
    load_yaml_bytes,
    model_values,
    read_blob,
    read_environments,
    read_rulesets,
    read_target,
    read_tree,
    read_variables,
    resolve_app,
    resolve_tag,
    ruleset_body,
    ruleset_shape,
    token_readonly,
    valid_https_url,
    valid_model,
    workflow_secrets,
)
from agent_repo.charter import lint_charter
from agent_repo.config import ConfigError, RepoConfig, load_config
from agent_repo.preflight import FIX_RULE, HOSTED_RUNNERS, PreflightContext, analyze

# The fix-agent feedback isolation landed in this upstream commit; older fix-agent pins lack it.
T10B_MERGE = "43c2d3b58ce3ebccd38d9ee2a05464966b6a94da"
UNSAFE_SHARED_APP_CONFIRMED = (
    "UNSAFE ON SHARED APP: consumer CI can expose agent credentials and the GitHub App is "
    "installed on every repository of this organization (repository_selection=all)."
)
UNSAFE_SHARED_APP_ASSUMED = (
    "UNSAFE ON SHARED APP: consumer CI can expose agent credentials and the GitHub App's reach "
    "cannot be read with maintainer credentials, so it is assumed to reach other repositories; "
    "fix apply re-checks the installation with the App token before any push."
)
RULESETS_UNSAFE = (
    "UNSAFE: rulesets are unavailable, so nothing stops the GitHub App from writing non-agent refs."
)
REQUIRED_VARIABLES = (
    "AGENTS_ENABLED",
    "AGENT_BOT_LOGIN",
    "AGENT_APP_CLIENT_ID",
    "AGENT_GATEWAY_KIND",
    "AGENT_GATEWAY_URL",
    *MODEL_VARIABLES.values(),
    "AGENT_FIX_ALLOWED_ENDPOINTS",
)
VERIFIED_VARIABLES = ("AGENT_SAFE_ENVIRONMENTS", "AGENT_CI_DEFAULT_TOKEN_READONLY")
FIX_WORKFLOW = ".github/workflows/agent-fix.yml"


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    errors: int = 0

    def ok(self, text: str) -> None:
        self.lines.append(f"OK: {text}")

    def warn(self, text: str) -> None:
        self.lines.append(f"WARN: {text}")

    def error(self, text: str, *, prefix: str = "ERROR: ") -> None:
        if prefix + text not in self.lines:
            self.lines.append(prefix + text)
            self.errors += 1


def _configuration(gh: Gh, target: Target, report: Report) -> RepoConfig | None:
    config = None
    with tempfile.TemporaryDirectory() as directory:
        agents = Path(directory) / ".agents"
        agents.mkdir()
        for name in ("config.yml", "areas.yml", "labels.yml"):
            content = file_content(gh, target, f".agents/{name}")
            if content is not None:
                (agents / name).write_bytes(content)
        try:
            config = load_config(agents)
        except ConfigError as exc:
            report.error(f"Configuration: {exc}")
    charter = file_content(gh, target, ".agents/charter.md")
    if charter is None:
        report.error("Missing .agents/charter.md.")
    else:
        for problem in lint_charter(charter.decode(errors="replace")):
            report.error(f"Charter: {problem}")
    return config


def _pins(gh: Gh, texts: dict[str, bytes], report: Report) -> str | None:
    """Check every template pin upstream and return the ref preflight would run at."""
    resolved: dict[str, str | None] = {}
    trees: dict[str, set[str]] = {}
    pins: dict[str, set[str]] = {"preflight": set(), "fix-agent": set()}
    for path, data in sorted(texts.items()):
        for line in data.decode(errors="replace").splitlines():
            match = PIN.search(line)
            if line.lstrip().startswith("#") or match is None:
                continue
            action, sha, version = match.groups()
            if SHA.fullmatch(sha) is None:
                report.error(f"{path}: {action} pin is not a 40-hex commit.")
                continue
            pins.setdefault(action, set()).add(sha)
            if sha not in trees:
                try:
                    trees[sha] = set(read_tree(gh, UPSTREAM, sha)[1])
                except BootstrapError:
                    trees[sha] = set()
            if f"actions/{action}/action.yml" not in trees[sha]:
                report.error(f"{path}: {action} action is missing at its pinned commit upstream.")
            if version is None:
                report.error(f"{path}: {action} pin has no # vX.Y.Z comment.")
                continue
            if version not in resolved:
                try:
                    resolved[version] = resolve_tag(gh, version)
                except BootstrapError:
                    resolved[version] = None
            if resolved[version] is None:
                report.error(f"{path}: {version} does not resolve to an upstream tag.")
            elif resolved[version] != sha:
                report.error(f"{path}: {action} pin does not match upstream {version}.")
            if action == "fix-agent":
                try:
                    comparison = gh.get(f"repos/{UPSTREAM}/compare/{T10B_MERGE}...{sha}")
                except GhError:
                    comparison = None
                if _field(comparison, "status") not in {"ahead", "identical"} or (
                    _field(comparison, "behind_by") != 0
                ):
                    report.error(
                        f"{path}: fix-agent pin predates the required fix-agent hardening."
                    )
    # Without its own preflight pin, a uniformly pinned fix workflow runs preflight at that SHA.
    for action in ("preflight", "fix-agent"):
        if pins[action]:
            return next(iter(pins[action])) if len(pins[action]) == 1 else None
    return None


def _agent_branches(gh: Gh, repo: str, bot: str | None, report: Report) -> None:
    if bot is None:
        return
    for ref in gh.items(f"repos/{repo}/git/matching-refs/heads/agent/"):
        name, sha = _field(ref, "ref"), _field(ref, "object", "sha")
        if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
            continue
        commit = gh.get(f"repos/{repo}/commits/{sha}")
        logins = {_field(commit, "author", "login"), _field(commit, "committer", "login")}
        if bot.casefold() not in {login.casefold() for login in logins if isinstance(login, str)}:
            continue
        message = _field(commit, "commit", "message")
        runs = re.findall(
            r"^Agent-Run:\s*(\d+)\s*$", message if isinstance(message, str) else "", re.M
        )
        path = None
        if len(runs) == 1:
            try:
                path = _field(gh.get(f"repos/{repo}/actions/runs/{runs[0]}"), "path")
            except GhError:
                path = None
        if not isinstance(path, str) or path.split("@", 1)[0] != FIX_WORKFLOW:
            report.error(f"{name} head is App-authored but its Agent-Run is not an agent-fix run.")


def _secrets(gh: Gh, repo: str, workflows: dict, llm_only: bool, report: Report) -> None:
    agent_workflows = {
        name: value for name, value in workflows.items() if name.startswith("agent-")
    }
    available = {
        None: {
            item.get("name")
            for item in gh.items(f"repos/{repo}/actions/secrets?per_page=100", "secrets")
        }
    }
    for environment, name in workflow_secrets(agent_workflows, llm_only):
        if environment not in available:
            try:
                names = {
                    item.get("name")
                    for item in gh.items(
                        f"repos/{repo}/environments/{_quote(environment)}/secrets?per_page=100",
                        "secrets",
                    )
                }
            except GhError:
                names = set()
            available[environment] = names
        if name not in available[environment] | available[None]:
            where = f"environment {environment}" if environment else "the repository"
            report.error(f"Missing secret {name} for {where}.")


def _runners(gh: Gh, repo: str, report: Report) -> None:
    try:
        runners = gh.items(f"repos/{repo}/actions/runners?per_page=100", "runners")
    except GhError:
        report.error("Cannot list self-hosted runners; repository admin access is required.")
        return
    labels = set()
    for runner in runners:
        for label in runner.get("labels") or []:
            name = _field(label, "name")
            if name in HOSTED_RUNNERS:
                report.error(
                    f"Self-hosted runner {runner.get('name')} uses the GitHub-hosted label {name}."
                )
            if isinstance(name, str):
                labels.add(name)
    if runners:
        report.warn(f"Self-hosted runners are registered with labels: {', '.join(sorted(labels))}.")


def org_shares_app(gh: Gh, target: Target, app: App | None) -> bool:
    """Only an org listing of this App with repository_selection=all confirms the sharing."""
    if app is None or target.owner_type != "Organization":
        return False
    owner = target.repo.split("/", 1)[0]
    try:
        installations = gh.items(
            f"orgs/{_quote(owner)}/installations?per_page=100", "installations"
        )
    except GhError:
        return False
    return any(
        _field(item, "app_slug") == app.slug and _field(item, "repository_selection") == "all"
        for item in installations
    )


def _declared_app(
    gh: Gh, variables: dict, slug: str | None, client_id: str | None, report: Report
) -> App | None:
    """The repository variables declare the App; flags, when given, must agree with them."""
    login = variables.get("AGENT_BOT_LOGIN")
    declared_slug = login.removesuffix("[bot]") if login and login.endswith("[bot]") else None
    if login and declared_slug is None:
        report.error("AGENT_BOT_LOGIN must be a bot login ending in [bot].")
    declared_client = variables.get("AGENT_APP_CLIENT_ID")
    if slug and declared_slug and slug.casefold() != declared_slug.casefold():
        report.error("--app-slug does not match AGENT_BOT_LOGIN.")
    if client_id and declared_client and client_id != declared_client:
        report.error("--app-client-id does not match AGENT_APP_CLIENT_ID.")
    try:
        app = resolve_app(gh, slug or declared_slug, client_id or declared_client)
    except BootstrapError as exc:
        report.error(str(exc))
        return None
    report.ok(
        f"GitHub App {app.slug} has client id {app.client_id} and bot account {app.slug}[bot]."
    )
    return app


def run_doctor(
    gh: Gh, repo: str, app_slug: str | None = None, app_client_id: str | None = None
) -> Report:
    report = Report()
    target = read_target(gh, repo)
    config = _configuration(gh, target, report)
    raw = config.raw if config is not None else {}
    llm_only = config is not None and raw.get("system_one") is None
    if config is not None:
        if llm_only:
            report.ok("Mode: LLM-only (no System One)")
            if _field(raw, "close", "enabled") is True:
                report.warn("close.enabled is true in LLM-only mode; auto-close stays off.")
        else:
            report.ok("Mode: System One")
        existing = {
            str(label.get("name")).casefold()
            for label in gh.items(f"repos/{repo}/labels?per_page=100")
        }
        for name in config.labels:
            if name.casefold() not in existing:
                report.error(f"Missing label {name}.")

    environments, _ = read_environments(gh, repo)
    for name in ENVIRONMENTS:
        if name not in environments:
            report.error(f"Missing environment {name}; rerun bootstrap.")
        elif environments[name] != [(target.default_branch, "branch")]:
            report.error(
                f"Environment {name} must allow deployments from {target.default_branch} only."
            )

    variables = read_variables(gh, repo)
    for name in (*REQUIRED_VARIABLES, *(() if llm_only else ("AGENT_SYSTEM_ONE_URL",))):
        if name not in variables:
            report.error(f"Missing variable {name}.")
    for name in VERIFIED_VARIABLES:
        if name not in variables:
            report.error(f"Missing variable {name}; rerun bootstrap to set it.")
    gateway = variables.get("AGENT_GATEWAY_URL")
    allowed = variables.get("AGENT_FIX_ALLOWED_ENDPOINTS")
    if gateway and not valid_https_url(gateway):
        report.error(
            "AGENT_GATEWAY_URL must be an https URL without credentials or query; "
            "rerun bootstrap with --gateway-url."
        )
    elif gateway and allowed is not None:
        if gateway_endpoint(gateway) not in allowed.split():
            report.error(
                "AGENT_FIX_ALLOWED_ENDPOINTS does not allow the gateway "
                f"{gateway_endpoint(gateway)}; rerun bootstrap."
            )
    if variables.get("AGENTS_ENABLED", "true") != "true":
        report.warn("AGENTS_ENABLED is not true, so every agent workflow is off.")
    if config is not None:
        for error in model_values(raw)[1]:
            report.error(error)
    for name in MODEL_VARIABLES.values():
        if name in variables and not valid_model(variables[name]):
            report.error(f"Variable {name} is not an Anthropic-family gateway model id.")
    for role, name in MODEL_VARIABLES.items():
        if config is not None and name in variables and variables[name] != raw["models"][role]:
            report.error(
                f"Variable {name} differs from models.{role}; "
                f"run ./bootstrap.sh --sync-models --repo {repo}."
            )
    listed = [name for name in variables.get("AGENT_SAFE_ENVIRONMENTS", "").splitlines() if name]
    for name in listed:
        if not environment_is_safe(environments.get(name)):
            report.error(
                f"AGENT_SAFE_ENVIRONMENTS lists {name}, "
                "whose deployment policy does not exclude agent branches."
            )
    readonly = token_readonly(gh, repo)
    if variables.get("AGENT_CI_DEFAULT_TOKEN_READONLY") == "true" and not readonly:
        report.error(
            "AGENT_CI_DEFAULT_TOKEN_READONLY is true but the default workflow token can write."
        )
    elif not readonly:
        report.warn("The default workflow token can write; make it read-only in the settings.")

    app = _declared_app(gh, variables, app_slug, app_client_id, report)
    bot = f"{app.slug}[bot]" if app is not None else variables.get("AGENT_BOT_LOGIN")

    try:
        rulesets = read_rulesets(gh, repo)
    except BootstrapError:
        report.error(RULESETS_UNSAFE, prefix="")
    else:
        for kind, name in RULESET_NAMES.items():
            if ruleset_shape(rulesets.get(name)) != ruleset_shape(ruleset_body(kind)):
                report.error(
                    f"Ruleset {name} is missing or differs from bootstrap; rerun bootstrap."
                )

    texts = {
        path: read_blob(gh, repo, sha)
        for path, sha in target.files.items()
        if path.startswith(".github/workflows/")
        and path.count("/") == 2
        and path.endswith((".yml", ".yaml"))
    }
    template_ref = _pins(gh, texts, report)
    workflows = {}
    for path, data in texts.items():
        try:
            workflows[path.rsplit("/", 1)[1]] = load_yaml_bytes(data, path)
        except BootstrapError as exc:
            report.error(str(exc))
    _agent_branches(gh, repo, bot, report)
    _secrets(gh, repo, workflows, llm_only, report)
    _runners(gh, repo, report)

    context = PreflightContext(
        target.default_branch,
        token_readonly=readonly and variables.get("AGENT_CI_DEFAULT_TOKEN_READONLY") == "true",
        safe_environments=frozenset(
            name for name in listed if environment_is_safe(environments.get(name))
        ),
        template_ref=template_ref,
    )
    try:
        exposures = analyze(workflows, context)
    except (TypeError, ValueError):
        report.error("Preflight could not verify consumer workflows.")
        return report
    ordinary = [item for item in exposures if item.rule != FIX_RULE]
    for item in exposures:
        if item.rule == FIX_RULE:
            report.lines.append(
                f"Accepted template exposure: {FIX_RULE} job={item.job} secret=AGENT_FIX_KEY"
            )
    for item in ordinary:
        reached = ", ".join(item.secrets) or "no named secret"
        report.error(
            f"CI exposure: {item.workflow} job {item.job} rule {item.rule} reaches {reached}."
        )
    if not ordinary:
        report.ok("Consumer CI exposes no agent credentials outside accepted template jobs.")
    elif _field(raw, "fix", "accept_ci_exposure") is True:
        report.warn("fix.accept_ci_exposure is true; it never clears UNSAFE ON SHARED APP.")
    # Maintainer credentials never prove the App reaches only this repository, so unknown is shared.
    if ordinary:
        confirmed = org_shares_app(gh, target, app)
        report.error(
            UNSAFE_SHARED_APP_CONFIRMED if confirmed else UNSAFE_SHARED_APP_ASSUMED, prefix=""
        )
    return report


def check(repo: str, app_slug: str | None = None, app_client_id: str | None = None) -> int:
    try:
        report = run_doctor(Gh(), repo, app_slug, app_client_id)
    except BootstrapError as exc:
        print(f"ERROR: {exc}")
        return 1
    for line in report.lines:
        print(line)
    print(f"Doctor found {report.errors} error(s)." if report.errors else "Doctor found no errors.")
    return 1 if report.errors else 0
