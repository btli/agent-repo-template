import importlib

import pytest


@pytest.mark.parametrize(
    "target,want",
    [
        ("grep_repo", "ALLOW"),
        ("read_repo_file", "ALLOW"),
        ("mcp__x__grep_repo", "DENY"),
        ("sys_policy_registry", "DENY"),
    ],
)
def test_exact_tool_allowlist(target, want):
    policy = importlib.import_module("agent_repo.policies").allow_only(
        ["grep_repo", "read_repo_file"]
    )
    assert policy({"type": "tool_call", "target": target})["result"] == want


def test_non_tool_event_abstains():
    policy = importlib.import_module("agent_repo.policies").allow_only([])
    assert policy({"type": "agent_start", "target": "grep_repo"}) is None


def test_denial_does_not_echo_untrusted_target():
    policy = importlib.import_module("agent_repo.policies").allow_only([])
    assert "credential-value" not in str(
        policy({"type": "tool_call", "target": "credential-value"})
    )


def test_agent_environment_drops_unused_provider_credentials():
    from agent_repo.policies import agent_environment

    environ = dict.fromkeys(
        [
            "LLM_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "DATABRICKS_TOKEN",
            "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_HOST",
            "GATEWAY_URL",
        ],
        "sentinel-provider-value",
    )
    assert agent_environment(environ) == {"LLM_API_KEY": "sentinel-provider-value"}
    assert agent_environment(environ, tool=True) == {}
