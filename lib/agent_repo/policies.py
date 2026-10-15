from collections.abc import Callable


def allow_only(allowed: list[str]) -> Callable[[dict], dict | None]:
    names = frozenset(allowed)

    def evaluate(event: dict) -> dict | None:
        if event.get("type") != "tool_call":
            return None
        target = event.get("target")
        if isinstance(target, str) and target in names:
            return {"result": "ALLOW"}
        return {"result": "DENY", "reason": "tool is not allowlisted"}

    return evaluate


def agent_environment(environ: dict[str, str], *, tool: bool = False) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "RUNNER_TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "GITHUB_WORKSPACE",
        "PYTHONPATH",
        "AGENT_REPO_BUDGET_FILE",
    }
    if tool:
        allowed.update({"_AP_RESPONSE_FD", "_AP_RESPONSE_MODE"})
    if not tool:
        allowed.update(
            {
                "LLM_API_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "GATEWAY_URL",
                "DATABRICKS_HOST",
                "DATABRICKS_TOKEN",
                "DATABRICKS_CONFIG_PROFILE",
                "DATABRICKS_CONFIG_FILE",
                "DATABRICKS_CLIENT_ID",
                "DATABRICKS_CLIENT_SECRET",
            }
        )
    return {key: value for key, value in environ.items() if key in allowed or key.startswith("LC_")}
