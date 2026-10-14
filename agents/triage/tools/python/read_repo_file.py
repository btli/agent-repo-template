import os

from agent_repo.repo_tools import repository_reader
from omnigent_client.tools import tool


@tool
def read_repo_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read lines of a repository file within the shared byte budget."""
    return repository_reader(os.environ.get("GITHUB_WORKSPACE", "")).read_file(
        path, start_line, max_lines
    )
