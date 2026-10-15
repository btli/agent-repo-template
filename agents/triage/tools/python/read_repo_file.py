import os

from agent_repo.repo_tools import fixed_tool_errors, repository_reader
from omnigent_client.tools import tool


@tool
@fixed_tool_errors
def read_repo_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a bounded line window; a truncated marker means more content is available."""
    return repository_reader(os.environ.get("GITHUB_WORKSPACE", "")).read_file(
        path, start_line, max_lines
    )
