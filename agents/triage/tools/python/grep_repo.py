import os

from agent_repo.repo_tools import repository_reader
from omnigent_client.tools import tool


@tool
def grep_repo(
    pattern: str, glob: str = "**/*", max_results: int = 200
) -> list[tuple[str, int, str]]:
    """Search repository text within the shared byte budget."""
    return repository_reader(os.environ.get("GITHUB_WORKSPACE", "")).grep(
        pattern, glob, max_results
    )
