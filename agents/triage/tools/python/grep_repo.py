import os

from agent_repo.repo_tools import fixed_tool_errors, repository_reader
from omnigent_client.tools import tool


@tool
@fixed_tool_errors
def grep_repo(
    pattern: str, glob: str = "**/*", max_results: int = 200
) -> list[tuple[str, int, str]]:
    """Search literal text with a shared run budget; line 0 marks skipped or truncated results."""
    return repository_reader(os.environ.get("GITHUB_WORKSPACE", "")).grep(
        pattern, glob, max_results
    )
