"""Install the template into a repository through the maintainer's own `gh` authentication."""

import base64
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from agent_repo.preflight import _secret_names
from agent_repo.schema import load_schema, validate
from agent_repo.yamljson import YamlError, load_yaml

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = "btli/agent-repo-template"
GITHUB_WEB = "https://github.com"
ENVIRONMENTS = ("agent-propose-triage", "agent-propose-review", "agent-propose-fix", "agent-apply")
MODEL_VARIABLES = {
    "triage": "AGENT_TRIAGE_MODEL",
    "review": "AGENT_REVIEW_MODEL",
    "fix": "AGENT_FIX_MODEL",
}
SYSTEM_ONE_KEY = "AGENT_SYSTEM_ONE_KEY"
RULESET_NAMES = {"branch": "agent-repo-template branches", "tag": "agent-repo-template tags"}
# Repository role ids for maintain, write and admin: humans bypass, the App never does.
HUMAN_ROLE_IDS = (2, 4, 5)
# Union of the App token permissions the triage, review and fix apply jobs request.
APP_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
}
# Fix-job egress besides the gateway: GitHub, artifact storage and the registries setup uses.
FIX_ENDPOINT_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "results-receiver.actions.githubusercontent.com",
    "*.blob.core.windows.net",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
COMMAND_DEFAULTS = (
    ("uv.lock", {"setup": "uv sync --frozen", "test": "uv run --frozen pytest"}),
    (
        "requirements.txt",
        {"setup": "python -m pip install -r requirements.txt", "test": "python -m pytest"},
    ),
    ("package-lock.json", {"setup": "npm ci", "test": "npm test"}),
)
# Ref prefixes an agent branch or a pull request run deploys from (GITHUB_REF and short form).
AGENT_REF_PREFIXES = ("agent/fix-", "refs/heads/agent/fix-", "pull/", "refs/pull/")
GLOB_CHARACTERS = "*?[{\\"
AGENT_REF = re.compile(r"(?:refs/heads/)?agent/fix-.+|(?:refs/)?pull/[^/]+/merge")
CONTRIBUTING_SECTION = "CONTRIBUTING.agents.md"
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
SECRET_LOOKING = re.compile(r"gh[pousr]_|github_pat_|sk-|xox[abprs]-|AKIA|-----BEGIN|dapi", re.I)
RULESETS_UNAVAILABLE = (
    "Rulesets are unavailable for this repository; bootstrap cannot protect non-agent refs."
)
NOT_ON_MAIN = "Template commit is not on upstream main."
APP_IDENTITY_UNKNOWN = (
    "GitHub App identity unknown: pass --app-slug and --app-client-id from the App's settings page."
)
APP_SLUG_MISSING = "GitHub App identity incomplete: pass --app-slug."
APP_CLIENT_ID_MISSING = "GitHub App identity incomplete: pass --app-client-id."
APP_BOT_MISSING = "GitHub App bot account not found: GET /users/{app_slug}[bot] must return a Bot."
APP_BOT_LOOKUP_FAILED = (
    "GET /users/{app_slug}[bot] failed with an error other than 404; "
    "bootstrap cannot confirm the App's bot account."
)
APP_MISMATCH = "The public GET /apps/{app_slug} does not match --app-slug and --app-client-id."
APP_LOOKUP_FAILED = (
    "GET /apps/{app_slug} failed with an error other than a private App's 404; "
    "bootstrap cannot cross-check the App."
)


class BootstrapError(Exception):
    """A fixed, secret-free reason bootstrap or doctor cannot continue."""


class GhError(BootstrapError):
    """Raised when a `gh api` call fails; the message names only the method and endpoint."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Gh:
    """Runs `gh api` with the maintainer's own authentication."""

    def _api(self, method: str, endpoint: str, arguments: tuple = (), body=None) -> str:
        command = ["gh", "api", *arguments]
        stream: dict = {"stdin": subprocess.DEVNULL}
        if method != "GET":
            command += ["-X", method, "--input", "-"]
            stream = {"input": json.dumps(body)}
        try:
            result = subprocess.run(
                [*command, endpoint],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
                **stream,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise GhError(f"gh api failed: {method} {endpoint}") from None
        if result.returncode != 0:
            # Only the numeric status is kept from gh's error output, never its text.
            status = re.search(r"\(HTTP (\d{3})\)", result.stderr)
            raise GhError(f"gh api failed: {method} {endpoint}", int(status[1]) if status else None)
        return result.stdout

    def get(self, endpoint: str) -> object:
        try:
            return json.loads(self._api("GET", endpoint))
        except ValueError:
            raise GhError(f"gh api returned invalid JSON: GET {endpoint}") from None

    def items(self, endpoint: str, key: str | None = None) -> list:
        output = self._api("GET", endpoint, ("--paginate", "--jq", f".{key}[]" if key else ".[]"))
        try:
            return [json.loads(line) for line in output.splitlines() if line.strip()]
        except ValueError:
            raise GhError(f"gh api returned invalid JSON: GET {endpoint}") from None

    def write(self, method: str, endpoint: str, body: object) -> object:
        output = self._api(method, endpoint, body=body)
        try:
            return json.loads(output) if output.strip() else None
        except ValueError:
            raise GhError(f"gh api returned invalid JSON: {method} {endpoint}") from None


def valid_model(model: object) -> bool:
    # The same rule as actions/setup/configure.py, so every seeded id is one setup accepts.
    return (
        isinstance(model, str)
        and re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", model) is not None
        and re.search(r"(?:^|[-./])claude[-.][A-Za-z0-9]", model) is not None
    )


def valid_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    try:
        # Reading the port raises ValueError when it is not a number or is out of range.
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    )


def gateway_endpoint(url: str) -> str:
    """The harden-runner allowlist entry for a valid https gateway URL."""
    parsed = urlsplit(url)
    return f"{parsed.hostname}:{parsed.port or 443}"


def _sha(value: object) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise BootstrapError("GitHub returned an invalid commit identity.")
    return value


def _field(value: object, *keys: str) -> object:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def require_on_upstream_main(gh: Gh, sha: str) -> None:
    comparison = gh.get(f"repos/{UPSTREAM}/compare/{sha}...main")
    if _field(comparison, "status") not in {"ahead", "identical"} or (
        _field(comparison, "behind_by") != 0
    ):
        raise BootstrapError(NOT_ON_MAIN)


def resolve_tag(gh: Gh, version: str) -> str:
    """Resolve a release tag through the upstream API; local tags are never consulted."""
    if VERSION.fullmatch(version) is None:
        raise BootstrapError("Template version must look like vX.Y.Z.")
    target = _field(gh.get(f"repos/{UPSTREAM}/git/ref/tags/{version}"), "object")
    for _ in range(5):
        kind, sha = _field(target, "type"), _sha(_field(target, "sha"))
        if kind == "commit":
            return sha
        if kind != "tag":
            break
        target = _field(gh.get(f"repos/{UPSTREAM}/git/tags/{sha}"), "object")
    raise BootstrapError("Upstream tag does not resolve to a commit.")


def resolve_pin(gh: Gh, ref: str | None, candidate: str | None, version: str) -> tuple[str, str]:
    if ref is not None:
        sha = resolve_tag(gh, ref)
        message = f"Template {ref} resolves upstream to {sha}."
    else:
        if candidate is None or SHA.fullmatch(candidate) is None:
            raise BootstrapError("A candidate must be a 40-hex commit.")
        if _field(gh.get(f"repos/{UPSTREAM}/commits/{candidate}"), "sha") != candidate:
            raise BootstrapError(NOT_ON_MAIN)
        sha = candidate
        message = f"Template candidate {sha} installs as {version}."
    require_on_upstream_main(gh, sha)
    return sha, message


def render_consumer(sha: str, version: str) -> dict[str, bytes]:
    files = {}
    source = ROOT / "consumer"
    for path in sorted(source.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            text = text.replace("__TEMPLATE_SHA__", sha).replace("__TEMPLATE_VERSION__", version)
            files[path.relative_to(source).as_posix()] = text.encode()
    return files


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def read_tree(gh: Gh, repo: str, commit: str) -> tuple[str, dict[str, str]]:
    tree = gh.get(f"repos/{repo}/git/trees/{commit}?recursive=1")
    entries = _field(tree, "tree")
    if _field(tree, "truncated") is not False or not isinstance(entries, list):
        raise BootstrapError("The repository tree is too large to read.")
    files = {
        entry["path"]: entry["sha"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "blob"
    }
    return _sha(_field(tree, "sha")), files


def read_blob(gh: Gh, repo: str, sha: str) -> bytes:
    content = _field(gh.get(f"repos/{repo}/git/blobs/{sha}"), "content")
    if not isinstance(content, str):
        raise BootstrapError("GitHub returned an invalid file.")
    return base64.b64decode(content)


def load_yaml_bytes(data: bytes, name: str) -> object:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "document.yml"
        path.write_bytes(data)
        try:
            return load_yaml(path)
        except YamlError:
            raise BootstrapError(f"{name} is not valid YAML.") from None


def parse_config(data: bytes) -> dict:
    raw = load_yaml_bytes(data, ".agents/config.yml")
    if not isinstance(raw, dict) or validate(raw, load_schema("config")):
        raise BootstrapError(
            ".agents/config.yml is not a valid agent configuration; fix it or pass --force."
        )
    return raw


def model_values(config: dict) -> tuple[dict[str, str], list[str]]:
    values, errors = {}, []
    for role, name in MODEL_VARIABLES.items():
        model = _field(config, "models", role)
        if valid_model(model):
            values[name] = model
        else:
            errors.append(f"models.{role} is not an Anthropic-family gateway model id.")
    return values, errors


@dataclass(frozen=True)
class Target:
    repo: str
    default_branch: str
    head: str
    tree_sha: str
    files: dict[str, str]
    admin: bool
    owner_type: object


def read_target(gh: Gh, repo: str) -> Target:
    info = gh.get(f"repos/{repo}")
    branch = _field(info, "default_branch")
    if not isinstance(branch, str):
        raise BootstrapError("Cannot read the target repository.")
    head = _sha(_field(gh.get(f"repos/{repo}/git/ref/heads/{quote(branch)}"), "object", "sha"))
    tree_sha, files = read_tree(gh, repo, head)
    admin = _field(info, "permissions", "admin") is True
    return Target(repo, branch, head, tree_sha, files, admin, _field(info, "owner", "type"))


def file_content(gh: Gh, target: Target, path: str) -> bytes | None:
    sha = target.files.get(path)
    return read_blob(gh, target.repo, sha) if sha else None


@dataclass(frozen=True)
class App:
    slug: str
    client_id: str


def resolve_app(gh: Gh, slug: str | None, client_id: str | None) -> App:
    """Confirm a declared App identity with public lookups; installations are never listed."""
    if not slug and not client_id:
        raise BootstrapError(APP_IDENTITY_UNKNOWN)
    if not slug:
        raise BootstrapError(APP_SLUG_MISSING)
    if not client_id:
        raise BootstrapError(APP_CLIENT_ID_MISSING)
    login = f"{slug}[bot]"
    try:
        bot = gh.get(f"users/{_quote(login)}")
    except GhError as error:
        raise BootstrapError(
            APP_BOT_MISSING if error.status == 404 else APP_BOT_LOOKUP_FAILED
        ) from None
    found = _field(bot, "login")
    if (
        _field(bot, "type") != "Bot"
        or not isinstance(found, str)
        or found.casefold() != login.casefold()
    ):
        raise BootstrapError(APP_BOT_MISSING)
    # Private Apps, the usual adopter case, answer GET /apps/{slug} with 404; only cross-check.
    try:
        public = gh.get(f"apps/{_quote(slug)}")
    except GhError as error:
        if error.status != 404:
            raise BootstrapError(APP_LOOKUP_FAILED) from None
        public = None
    if public is not None and (
        str(_field(public, "slug")).casefold() != slug.casefold()
        or _field(public, "client_id") != client_id
    ):
        raise BootstrapError(APP_MISMATCH)
    return App(found.removesuffix("[bot]"), client_id)


def read_environments(gh: Gh, repo: str) -> tuple[dict[str, list | None], dict[str, object]]:
    """Map each environment to its custom patterns (None without them) and its policy object."""
    environments, policies = {}, {}
    for item in gh.items(f"repos/{repo}/environments?per_page=100", "environments"):
        name = item.get("name")
        if not isinstance(name, str):
            continue
        patterns = None
        policies[name] = item.get("deployment_branch_policy")
        if _field(item, "deployment_branch_policy", "custom_branch_policies") is True:
            patterns = [
                (policy.get("name"), policy.get("type", "branch"))
                for policy in gh.items(
                    f"repos/{repo}/environments/{_quote(name)}"
                    "/deployment-branch-policies?per_page=100",
                    "branch_policies",
                )
            ]
        environments[name] = patterns
    return environments, policies


def pattern_is_safe(pattern: object) -> bool:
    """A deployment pattern is safe only if it provably matches no agent or pull ref."""
    if not isinstance(pattern, str):
        return False
    glob = next((index for index, char in enumerate(pattern) if char in GLOB_CHARACTERS), None)
    if glob is None:
        return AGENT_REF.fullmatch(pattern) is None
    literal = pattern[:glob]
    return not any(
        literal.startswith(prefix) or prefix.startswith(literal) for prefix in AGENT_REF_PREFIXES
    )


def environment_is_safe(patterns: list | None) -> bool:
    return bool(patterns) and all(pattern_is_safe(name) for name, _ in patterns)


def read_variables(gh: Gh, repo: str) -> dict[str, str]:
    return {
        item["name"]: str(item.get("value", ""))
        for item in gh.items(f"repos/{repo}/actions/variables?per_page=30", "variables")
        if isinstance(item.get("name"), str)
    }


def token_readonly(gh: Gh, repo: str) -> bool:
    permissions = gh.get(f"repos/{repo}/actions/permissions/workflow")
    return _field(permissions, "default_workflow_permissions") == "read"


def ruleset_body(target: str) -> dict:
    branch = target == "branch"
    rules = ["creation", "update", "deletion"] + (["non_fast_forward"] if branch else [])
    return {
        "name": RULESET_NAMES[target],
        "target": target,
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": role, "actor_type": "RepositoryRole", "bypass_mode": "always"}
            for role in HUMAN_ROLE_IDS
        ],
        "conditions": {
            "ref_name": {"include": ["~ALL"], "exclude": ["refs/heads/agent/**"] if branch else []}
        },
        "rules": [{"type": rule} for rule in rules],
    }


def ruleset_shape(data: dict) -> object:
    try:
        return {
            "name": data["name"],
            "target": data["target"],
            "enforcement": data["enforcement"],
            "bypass_actors": sorted(
                (actor["actor_type"], actor["actor_id"], actor.get("bypass_mode", "always"))
                for actor in data.get("bypass_actors") or []
            ),
            "include": sorted(data["conditions"]["ref_name"]["include"]),
            "exclude": sorted(data["conditions"]["ref_name"].get("exclude") or []),
            "rules": sorted(rule["type"] for rule in data["rules"]),
        }
    except (AttributeError, KeyError, TypeError):
        return None


def read_rulesets(gh: Gh, repo: str) -> dict[str, dict]:
    try:
        listing = gh.items(f"repos/{repo}/rulesets?per_page=100")
    except GhError:
        raise BootstrapError(RULESETS_UNAVAILABLE) from None
    found = {}
    for entry in listing:
        if entry.get("name") in RULESET_NAMES.values() and isinstance(entry.get("id"), int):
            found[entry["name"]] = gh.get(f"repos/{repo}/rulesets/{entry['id']}")
    return found


def workflow_secrets(workflows: dict[str, object], llm_only: bool) -> list[tuple[str | None, str]]:
    """Secret names each job reads, keyed by the environment that supplies them."""
    needed = set()
    for workflow in workflows.values():
        jobs = _field(workflow, "jobs")
        for job in jobs.values() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict):
                continue
            environment = job.get("environment")
            environment = (
                _field(environment, "name") if isinstance(environment, dict) else environment
            )
            for name in _secret_names(job) | _secret_names(_field(workflow, "env")):
                if name != "*" and not (llm_only and name == SYSTEM_ONE_KEY):
                    needed.add((environment if isinstance(environment, str) else None, name))
    return sorted(needed, key=lambda item: (item[0] or "", item[1]))


def _quote(value: str) -> str:
    return quote(value, safe="")


def _shown(value: str) -> str:
    return json.dumps(value)


@dataclass
class Mutation:
    description: str
    perform: Callable[[], str]


@dataclass
class Plan:
    lines: list[str] = field(default_factory=list)
    mutations: list[Mutation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, description: str, perform: Callable[[], str]) -> None:
        self.mutations.append(Mutation(description, perform))


def plan_labels(gh: Gh, repo: str, names: list[str], plan: Plan) -> None:
    existing = {
        label["name"].casefold(): label["name"]
        for label in gh.items(f"repos/{repo}/labels?per_page=100")
        if isinstance(label.get("name"), str)
    }
    for name in names:
        current = existing.get(name.casefold())
        if current == name:
            continue
        if current is None:

            def create(name=name) -> str:
                gh.write("POST", f"repos/{repo}/labels", {"name": name, "color": "ededed"})
                return f"gh api -X DELETE repos/{repo}/labels/{_quote(name)}"

            plan.add(f"create label {name}", create)
        else:

            def rename(name=name, current=current) -> str:
                gh.write("PATCH", f"repos/{repo}/labels/{_quote(current)}", {"new_name": name})
                return (
                    f"gh api -X PATCH repos/{repo}/labels/{_quote(name)} "
                    f"-f new_name={shlex.quote(current)}"
                )

            plan.add(f"rename label {current} to {name}", rename)


def plan_environments(
    gh: Gh, repo: str, default_branch: str, environments: dict, policies: dict, plan: Plan
) -> list[str]:
    """Plan the four agent environments and return every environment that will be safe."""
    wanted = (default_branch, "branch")
    for name in ENVIRONMENTS:
        endpoint = f"repos/{repo}/environments/{_quote(name)}"
        patterns = environments.get(name)
        if patterns is None:
            existed = name in environments
            previous = json.dumps({"deployment_branch_policy": policies.get(name)})

            def put(endpoint=endpoint, existed=existed, previous=previous) -> str:
                policy = {"protected_branches": False, "custom_branch_policies": True}
                gh.write("PUT", endpoint, {"deployment_branch_policy": policy})
                if existed:
                    return f"echo {shlex.quote(previous)} | gh api -X PUT {endpoint} --input -"
                return f"gh api -X DELETE {endpoint}"

            verb = "restrict environment" if existed else "create environment"
            plan.add(f"{verb} {name} to custom deployment branch policies", put)
            patterns = []
        if [pattern for pattern in patterns if pattern != wanted]:
            plan.errors.append(
                f"Environment {name} has deployment policies other than {default_branch}; "
                "remove them and rerun."
            )
        elif not patterns:

            def allow(endpoint=endpoint) -> str:
                created = gh.write(
                    "POST",
                    f"{endpoint}/deployment-branch-policies",
                    {"name": default_branch, "type": "branch"},
                )
                policy = _field(created, "id")
                return f"gh api -X DELETE {endpoint}/deployment-branch-policies/{policy}"

            plan.add(f"allow deployments to {name} from {default_branch}", allow)
    safe = {name for name in ENVIRONMENTS if environment_is_safe([wanted])}
    safe |= {
        name
        for name, patterns in environments.items()
        if name not in ENVIRONMENTS and environment_is_safe(patterns)
    }
    return sorted(safe)


GATEWAY_KINDS = ("databricks", "litellm")
# Rules a previous value must pass before a rollback command may print it.
VARIABLE_RULES = {
    "AGENT_GATEWAY_URL": valid_https_url,
    "AGENT_SYSTEM_ONE_URL": valid_https_url,
    "AGENT_GATEWAY_KIND": lambda value: value in GATEWAY_KINDS,
    **{name: valid_model for name in MODEL_VARIABLES.values()},
}


def restorable(name: str, value: str) -> bool:
    return not SECRET_LOOKING.search(value) and VARIABLE_RULES.get(name, lambda _: True)(value)


def plan_variables(
    gh: Gh, repo: str, existing: dict[str, str], wanted: dict[str, str], plan: Plan
) -> None:
    endpoint = f"repos/{repo}/actions/variables"
    for name, value in wanted.items():
        if existing.get(name) == value:
            continue
        if name not in existing:

            def create(name=name, value=value) -> str:
                gh.write("POST", endpoint, {"name": name, "value": value})
                return f"gh api -X DELETE {endpoint}/{name}"

            plan.add(f"set variable {name} = {_shown(value)}", create)
        elif not restorable(name, existing[name]):
            plan.errors.append(
                f"Variable {name} holds a value bootstrap cannot restore on rollback; "
                "correct or delete it manually, then rerun."
            )
        else:
            previous = existing[name]

            def update(name=name, value=value, previous=previous) -> str:
                gh.write("PATCH", f"{endpoint}/{name}", {"name": name, "value": value})
                return f"gh api -X PATCH {endpoint}/{name} -f value={shlex.quote(previous)}"

            plan.add(f"update variable {name} = {_shown(value)}", update)


def plan_rulesets(gh: Gh, repo: str, rulesets: dict[str, dict], plan: Plan) -> None:
    for target, name in RULESET_NAMES.items():
        body = ruleset_body(target)
        current = rulesets.get(name)
        if current is not None and ruleset_shape(current) == ruleset_shape(body):
            continue
        if current is None:

            def create(body=body) -> str:
                created = gh.write("POST", f"repos/{repo}/rulesets", body)
                return f"gh api -X DELETE repos/{repo}/rulesets/{_field(created, 'id')}"

            plan.add(f"create ruleset {name}", create)
        else:
            endpoint = f"repos/{repo}/rulesets/{current.get('id')}"
            previous = {key: current.get(key) for key in body}

            def update(body=body, endpoint=endpoint, previous=previous) -> str:
                gh.write("PUT", endpoint, body)
                return (
                    f"echo {shlex.quote(json.dumps(previous))} | gh api -X PUT {endpoint} --input -"
                )

            plan.add(f"update ruleset {name}", update)


def detect_commands(files: dict[str, str]) -> dict[str, str]:
    for marker, commands in COMMAND_DEFAULTS:
        if marker in files:
            return commands
    return {}


def with_commands(data: bytes, commands: dict[str, str]) -> bytes:
    text = data.decode()
    for name, value in commands.items():
        text = re.sub(
            rf'^(  {name}: )""$',
            lambda match, value=value: match[1] + json.dumps(value),
            text,
            flags=re.M,
        )
    return text.encode()


def plan_files(
    gh: Gh, target: Target, rendered: dict[str, bytes], force: bool
) -> tuple[dict[str, bytes], list[str]]:
    changes, notes = {}, []
    for path, data in rendered.items():
        if path == CONTRIBUTING_SECTION:
            current = file_content(gh, target, "CONTRIBUTING.md") or b""
            heading = data.decode().splitlines()[0]
            if heading not in current.decode(errors="replace").splitlines():
                changes["CONTRIBUTING.md"] = (
                    current.rstrip(b"\n") + b"\n\n" + data if current else data
                )
            continue
        if path == ".agents/config.yml":
            data = with_commands(data, detect_commands(target.files))
        existing = target.files.get(path)
        if existing == blob_sha(data):
            continue
        if existing is None or force:
            changes[path] = data
        else:
            notes.append(f"Kept existing {path}; pass --force to replace it.")
    return changes, notes


def plan_proposal(
    gh: Gh, target: Target, changes: dict[str, bytes], label: str, version: str, plan: Plan
) -> None:
    """File changes land on a bootstrap branch with a draft pull request, never the default."""
    if not changes:
        return
    repo, branch = target.repo, f"bootstrap/agent-repo-template-{version}"
    refs = gh.items(f"repos/{repo}/git/matching-refs/heads/{branch}")
    existing = [ref for ref in refs if ref.get("ref") == f"refs/heads/{branch}"]

    def open_pull() -> str:
        body = (
            f"Installs agent-repo-template {label}. Every role starts in dry-run mode.\n\n"
            + "".join(f"- `{path}`\n" for path in sorted(changes))
        )
        pull = gh.write(
            "POST",
            f"repos/{repo}/pulls",
            {
                "title": f"Install agent-repo-template {version}",
                "head": branch,
                "base": target.default_branch,
                "body": body,
                "draft": True,
            },
        )
        return f"gh pr close {_field(pull, 'number')} --repo {repo}"

    if existing:
        _, branch_files = read_tree(gh, repo, _sha(_field(existing[0], "object", "sha")))
        expected = {**target.files, **{path: blob_sha(data) for path, data in changes.items()}}
        if branch_files != expected:
            plan.errors.append(
                f"Branch {branch} already exists with other content; "
                "merge or delete it, then rerun."
            )
            return
        owner = repo.split("/", 1)[0]
        if gh.items(f"repos/{repo}/pulls?state=open&head={_quote(owner)}:{_quote(branch)}"):
            return
    else:

        def create_branch() -> str:
            tree = gh.write(
                "POST",
                f"repos/{repo}/git/trees",
                {
                    "base_tree": target.tree_sha,
                    "tree": [
                        {"path": path, "mode": "100644", "type": "blob", "content": data.decode()}
                        for path, data in sorted(changes.items())
                    ],
                },
            )
            commit = gh.write(
                "POST",
                f"repos/{repo}/git/commits",
                {
                    "message": f"Install agent-repo-template {version}\n\nTemplate: {label}\n",
                    "tree": _sha(_field(tree, "sha")),
                    "parents": [target.head],
                },
            )
            gh.write(
                "POST",
                f"repos/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": _sha(_field(commit, "sha"))},
            )
            return f"gh api -X DELETE repos/{repo}/git/refs/heads/{branch}"

        plan.add(
            f"create branch {branch} with {len(changes)} file(s) from {target.default_branch}",
            create_branch,
        )
    plan.add(f"open draft pull request from {branch} into {target.default_branch}", open_pull)


@dataclass(frozen=True)
class InstallOptions:
    repo: str
    ref: str | None
    candidate: str | None
    version: str
    force: bool = False
    gateway_url: str | None = None
    gateway_kind: str | None = None
    system_one_url: str | None = None
    app_slug: str | None = None
    app_client_id: str | None = None


def _require_admin(gh: Gh, repo: str) -> Target:
    if not isinstance(_field(gh.get("user"), "login"), str):
        raise BootstrapError("gh is not authenticated; run gh auth login.")
    target = read_target(gh, repo)
    if not target.admin:
        raise BootstrapError("Bootstrap needs repository admin access.")
    return target


def fix_endpoints(url: str, previous_url: str | None, current: str | None) -> str:
    """Seed the allowlist, or swap the old gateway entry for the new one and keep the rest."""
    endpoint = gateway_endpoint(url)
    if current is None:
        return " ".join([endpoint, *(f"{host}:443" for host in FIX_ENDPOINT_HOSTS)])
    entries = current.split()
    if previous_url is not None and valid_https_url(previous_url):
        old = gateway_endpoint(previous_url)
        entries = [endpoint if entry == old else entry for entry in entries]
    if endpoint not in entries:
        entries.insert(0, endpoint)
    return " ".join(entries)


def _gateway(name: str, flag: str, given: str | None, existing: dict, plan: Plan) -> str | None:
    value = given if given is not None else existing.get(name)
    if value is None:
        plan.errors.append(f"Pass {flag}; the repository has no {name} variable yet.")
    return value


def plan_install(gh: Gh, options: InstallOptions) -> Plan:
    plan = Plan()
    target = _require_admin(gh, options.repo)
    repo = target.repo
    sha, message = resolve_pin(gh, options.ref, options.candidate, options.version)
    plan.lines.append(message)
    rulesets = read_rulesets(gh, repo)
    rendered = render_consumer(sha, options.version)
    changes, plan.notes = plan_files(gh, target, rendered, options.force)

    def landed(path: str) -> bytes:
        # An existing empty file is validated as itself, so compare against None, not truthiness.
        for content in (changes.get(path), file_content(gh, target, path)):
            if content is not None:
                return content
        return rendered[path]

    config = parse_config(landed(".agents/config.yml"))
    labels = _field(load_yaml_bytes(landed(".agents/labels.yml"), ".agents/labels.yml"), "labels")
    if not isinstance(labels, list) or not all(isinstance(name, str) for name in labels):
        raise BootstrapError(".agents/labels.yml must hold a labels list.")
    llm_only = config.get("system_one") is None
    try:
        app = resolve_app(gh, options.app_slug, options.app_client_id)
    except BootstrapError as exc:
        plan.errors.append(str(exc))
        app = None
    environments, policies = read_environments(gh, repo)
    existing = read_variables(gh, repo)
    readonly = token_readonly(gh, repo)

    plan_labels(gh, repo, labels, plan)
    safe = plan_environments(gh, repo, target.default_branch, environments, policies, plan)
    models, errors = model_values(config)
    plan.errors += errors
    wanted: dict[str, str] = {}
    if "AGENTS_ENABLED" not in existing:
        wanted["AGENTS_ENABLED"] = "true"
    if app is not None:
        wanted["AGENT_BOT_LOGIN"] = f"{app.slug}[bot]"
        wanted["AGENT_APP_CLIENT_ID"] = app.client_id
        plan.lines.append(f"GitHub App {app.slug} has client id {app.client_id}.")
    kind = _gateway("AGENT_GATEWAY_KIND", "--gateway-kind", options.gateway_kind, existing, plan)
    url = _gateway("AGENT_GATEWAY_URL", "--gateway-url", options.gateway_url, existing, plan)
    if kind is not None and kind not in GATEWAY_KINDS:
        plan.errors.append("The gateway kind must be databricks or litellm.")
    elif kind is not None:
        wanted["AGENT_GATEWAY_KIND"] = kind
    if url is not None and not valid_https_url(url):
        plan.errors.append("The gateway URL must be an https URL without credentials or query.")
        url = None
    elif url is not None:
        wanted["AGENT_GATEWAY_URL"] = url
    if not llm_only:
        system_one = _gateway(
            "AGENT_SYSTEM_ONE_URL", "--system-one-url", options.system_one_url, existing, plan
        )
        if system_one is not None and not valid_https_url(system_one):
            plan.errors.append("The System One URL must be an https URL without credentials.")
        elif system_one is not None:
            wanted["AGENT_SYSTEM_ONE_URL"] = system_one
    wanted.update(models)
    if url is not None:
        wanted["AGENT_FIX_ALLOWED_ENDPOINTS"] = fix_endpoints(
            url, existing.get("AGENT_GATEWAY_URL"), existing.get("AGENT_FIX_ALLOWED_ENDPOINTS")
        )
    wanted["AGENT_SAFE_ENVIRONMENTS"] = "\n".join(safe)
    wanted["AGENT_CI_DEFAULT_TOKEN_READONLY"] = "true" if readonly else "false"
    plan_variables(gh, repo, existing, wanted, plan)
    plan_rulesets(gh, repo, rulesets, plan)
    plan_proposal(gh, target, changes, sha, options.version, plan)

    workflows = {
        path: load_yaml_bytes(data, path)
        for path, data in rendered.items()
        if path.startswith(".github/workflows/")
    }
    plan.notes.append(f"Mode: {'LLM-only (no System One)' if llm_only else 'System One'}.")
    areas = sorted({path.split("/", 1)[0] for path in target.files if "/" in path})
    areas = [name for name in areas if not name.startswith(".")]
    if areas:
        plan.notes.append(
            "Suggested area paths for .agents/areas.yml: "
            + ", ".join(f"{name}/**" for name in areas)
        )
    if not readonly:
        plan.notes.append(
            "The default workflow token can write. Bootstrap never changes it; to make it "
            f"read-only run: gh api -X PUT repos/{repo}/actions/permissions/workflow "
            "-f default_workflow_permissions=read"
        )
    plan.notes.append("Set these secrets yourself; bootstrap never reads or writes secret values:")
    for environment, name in workflow_secrets(workflows, llm_only):
        scope = f" --env {environment}" if environment else ""
        plan.notes.append(f"  gh secret set {name} --repo {repo}{scope}")
    return plan


def plan_sync_models(gh: Gh, repo: str) -> Plan:
    plan = Plan()
    target = _require_admin(gh, repo)
    content = file_content(gh, target, ".agents/config.yml")
    if content is None:
        raise BootstrapError("Missing .agents/config.yml on the default branch.")
    models, plan.errors = model_values(parse_config(content))
    if not plan.errors:
        plan_variables(gh, repo, read_variables(gh, repo), models, plan)
    return plan


def journal_path(repo: str) -> Path:
    return Path.home() / ".agent-repo-template/journal" / f"{repo.replace('/', '__')}.jsonl"


class Journal:
    """Append-only record of completed admin writes, so a rerun can report and roll back."""

    def __init__(self, path: Path):
        self.path = path

    def pending(self) -> list[dict]:
        if not self.path.is_file():
            return []
        entries = [json.loads(line) for line in self.path.read_text().splitlines() if line]
        done = []
        for entry in entries:
            if entry.get("event") == "complete":
                done = []
            elif entry.get("event") == "done":
                done.append(entry)
        return done

    def record(self, event: str, **fields: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"event": event, "at": datetime.now(UTC).isoformat(), **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def execute(plan: Plan, repo: str, *, preview: bool, journal: Journal) -> int:
    for line in plan.lines:
        print(line)
    if plan.mutations:
        print(f"{'Planned' if preview else 'Changes'} for {repo}:")
        for mutation in plan.mutations:
            print(f"  - {mutation.description}")
    else:
        print("No changes.")
    for note in plan.notes:
        print(note)
    for error in plan.errors:
        print(f"ERROR: {error}")
    if plan.errors:
        print("Bootstrap made no changes.")
        return 1
    if preview or not plan.mutations:
        return 0
    earlier = journal.pending()
    if earlier:
        print(f"Resuming: {len(earlier)} mutation(s) completed by an earlier run.")
    journal.record("start", repo=repo)
    for mutation in plan.mutations:
        try:
            rollback = mutation.perform()
        except BootstrapError:
            journal.record("failed", description=mutation.description)
            print(f"Bootstrap stopped: {mutation.description} failed.")
            print("Rollback commands, newest first:")
            for entry in reversed(journal.pending()):
                print(f"  {entry['rollback']}")
            print("Rerun the same command to resume.")
            return 1
        journal.record("done", description=mutation.description, rollback=rollback)
    journal.record("complete", repo=repo)
    print("Bootstrap complete.")
    return 0


def app_link(gh: Gh, owner: str) -> str:
    kind = _field(gh.get(f"users/{_quote(owner)}"), "type")
    base = (
        f"{GITHUB_WEB}/organizations/{_quote(owner)}/settings/apps/new"
        if kind == "Organization"
        else f"{GITHUB_WEB}/settings/apps/new"
    )
    query = {
        "name": f"{owner} repository agent",
        "url": f"{GITHUB_WEB}/{UPSTREAM}",
        "public": "false",
        "webhook_active": "false",
        **APP_PERMISSIONS,
    }
    return f"{base}?{urlencode(query)}"
