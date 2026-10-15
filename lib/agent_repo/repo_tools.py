import fcntl
import fnmatch
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

from agent_repo.policies import agent_environment


class ToolError(Exception):
    """A repository read was refused or exceeded its budget."""


class RepoReader:
    def __init__(
        self,
        root: Path,
        per_call_bytes: int = 65536,
        per_run_bytes: int = 524288,
        *,
        budget_file: Path | None = None,
    ):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or per_call_bytes < 1 or per_run_bytes < 1:
            raise ToolError("invalid repository or byte budget")
        self.per_call_bytes = per_call_bytes
        self.per_run_bytes = per_run_bytes
        self._used = 0
        self._lock = threading.Lock()
        self._budget_file = budget_file

    @contextmanager
    def _budget(self):
        with self._lock:
            if self._budget_file is None:
                yield
                return
            descriptor = None
            try:
                temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
                path = self._budget_file
                parent = path.parent.resolve(strict=True)
                if not parent.is_relative_to(temp) or parent.is_relative_to(self.root):
                    raise ToolError("unsafe budget location")
                descriptor = os.open(parent / path.name, os.O_RDWR | os.O_NOFOLLOW)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ToolError("unsafe budget file")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                encoded = os.read(descriptor, 32)
                if not re.fullmatch(rb"(0|[1-9][0-9]{0,8})\n", encoded):
                    raise ToolError("corrupt byte budget")
                self._used = int(encoded)
                if self._used > self.per_run_bytes:
                    raise ToolError("corrupt byte budget")
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    os.write(descriptor, f"{self._used}\n".encode())
                    os.ftruncate(descriptor, os.lseek(descriptor, 0, os.SEEK_CUR))
            except (KeyError, OSError, ValueError):
                raise ToolError("byte budget unavailable") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _charge(self, count: int) -> None:
        if count > self.per_run_bytes - self._used:
            self._used = self.per_run_bytes
            raise ToolError("per-run byte budget exceeded")
        self._used += count

    def _read(self, rel: str, remaining: int, *, search: bool = False) -> tuple[bytes, bool] | None:
        path = Path(rel)
        if (
            path.is_absolute()
            or not path.parts
            or any(p.casefold() in {"..", ".git"} for p in path.parts)
        ):
            raise ToolError("path is outside the readable repository")
        directory = None
        descriptor = None
        try:
            # Never follow a link, including one swapped during traversal.
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
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("only regular repository files are readable")
            if search and info.st_size > self.per_call_bytes:
                self._charge(self.per_call_bytes)
                return None
            if self._used >= self.per_run_bytes:
                raise ToolError("per-run byte budget exceeded")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = stream.read(min(remaining, self.per_run_bytes - self._used) + 1)
            self._charge(len(data))
            truncated = len(data) > remaining
            if truncated and not search:
                raise ToolError("per-call byte budget exceeded")
            return data[:remaining], truncated
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
        with self._budget():
            data, _ = self._read(rel, self.per_call_bytes)
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

        def append(path: str, number: int, line: str) -> bool:
            marker = ("", 0, "[truncated]")
            candidate = (path, number, line)
            encoded = json.dumps(results + [candidate, marker], ensure_ascii=False).encode()
            if len(encoded) > self.per_call_bytes:
                if len(json.dumps(results + [marker]).encode()) <= self.per_call_bytes:
                    results.append(marker)
                return False
            results.append(candidate)
            return len(results) < max_results

        candidates = []
        for directory, folders, files in os.walk(self.root, followlinks=False):
            folders[:] = [
                f
                for f in folders
                if f.casefold() != ".git"
                and not Path(directory, f).is_symlink()
                and f not in {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
            ]
            for name in files:
                path = Path(directory, name)
                rel = path.relative_to(self.root).as_posix()
                if name.casefold() == ".git" or path.is_symlink():
                    continue
                if fnmatch.fnmatchcase(rel, glob) or (
                    glob.startswith("**/") and fnmatch.fnmatchcase(rel, glob[3:])
                ):
                    candidates.append(rel)
        with self._budget():
            for rel in sorted(candidates):
                read = self._read(rel, remaining, search=True)
                if read is None:
                    if not append(rel, 0, "[skipped: file exceeds per-call byte cap]"):
                        return results
                    continue
                data, truncated = read
                remaining -= len(data)
                if b"\0" not in data:
                    for number, line in enumerate(
                        data.decode("utf-8", errors="replace").splitlines(), 1
                    ):
                        if expression.search(line) and not append(rel, number, line):
                            return results
                if truncated or remaining == 0:
                    append(rel, 0, "[truncated: scan byte cap reached]")
                    return results
        return results


def repository_reader(root: str) -> RepoReader:
    clean = agent_environment(dict(os.environ), tool=True)
    os.environ.clear()
    os.environ.update(clean)
    if not root or not os.environ.get("AGENT_REPO_BUDGET_FILE"):
        raise ToolError("repository workspace and shared budget are required")
    return RepoReader(Path(root), budget_file=Path(os.environ["AGENT_REPO_BUDGET_FILE"]))
