import fnmatch
import os
import re
import stat
import threading
from functools import lru_cache
from pathlib import Path


class ToolError(Exception):
    """A repository read was refused or exceeded its budget."""


class RepoReader:
    def __init__(self, root: Path, per_call_bytes: int = 65536, per_run_bytes: int = 524288):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or per_call_bytes < 1 or per_run_bytes < 1:
            raise ToolError("invalid repository or byte budget")
        self.per_call_bytes = per_call_bytes
        self.per_run_bytes = per_run_bytes
        self._used = 0
        self._lock = threading.Lock()

    def _read(self, rel: str, remaining: int) -> bytes:
        path = Path(rel)
        if path.is_absolute() or not path.parts or any(p in {"..", ".git"} for p in path.parts):
            raise ToolError("path is outside the readable repository")
        # Open every component without following links, including during concurrent renames.
        directory = None
        descriptor = None
        try:
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in path.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            descriptor = os.open(
                path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolError("only regular repository files are readable")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = stream.read(min(remaining, self.per_run_bytes - self._used) + 1)
            self._used += len(data)
            if len(data) > remaining:
                raise ToolError("per-call byte budget exceeded")
            if self._used > self.per_run_bytes:
                raise ToolError("per-run byte budget exceeded")
            return data
        except (OSError, ValueError):
            raise ToolError("repository file is unavailable or unsafe") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def read_file(self, rel: str, start_line: int = 1, max_lines: int = 400) -> str:
        if start_line < 1 or max_lines < 1 or max_lines > 400:
            raise ToolError("invalid line limits")
        with self._lock:
            data = self._read(rel, self.per_call_bytes)
        return "".join(
            data.decode("utf-8", errors="replace").splitlines(keepends=True)[
                start_line - 1 : start_line - 1 + max_lines
            ]
        )

    def grep(
        self, pattern: str, glob: str = "**/*", max_results: int = 200
    ) -> list[tuple[str, int, str]]:
        if max_results < 1 or max_results > 200:
            raise ToolError("invalid result limit")
        try:
            expression = re.compile(pattern)
        except re.error:
            raise ToolError("invalid search pattern") from None
        results = []
        remaining = self.per_call_bytes
        with self._lock:
            for directory, folders, files in os.walk(self.root, followlinks=False):
                folders[:] = sorted(
                    f for f in folders if f != ".git" and not Path(directory, f).is_symlink()
                )
                for name in sorted(files):
                    path = Path(directory, name)
                    rel = path.relative_to(self.root).as_posix()
                    if name == ".git" or path.is_symlink():
                        continue
                    if not (
                        fnmatch.fnmatchcase(rel, glob)
                        or (glob.startswith("**/") and fnmatch.fnmatchcase(rel, glob[3:]))
                    ):
                        continue
                    data = self._read(rel, remaining)
                    remaining -= len(data)
                    if b"\0" in data:
                        continue
                    for number, line in enumerate(
                        data.decode("utf-8", errors="replace").splitlines(), 1
                    ):
                        if expression.search(line):
                            results.append((rel, number, line))
                            if len(results) >= max_results:
                                return results
        return results


@lru_cache(maxsize=1)
def repository_reader(root: str) -> RepoReader:
    if not root:
        raise ToolError("repository workspace is required")
    return RepoReader(Path(root))
