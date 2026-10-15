import fcntl
import fnmatch
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from agent_repo.policies import agent_environment


class ToolError(Exception):
    """A repository read was refused or exceeded its budget."""


def fixed_tool_errors(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise ToolError("repository tool failed") from None

    return invoke


def response_bytes(result) -> int:
    """Bound both the runtime JSON-string fallback and its outer response envelope."""
    serialized = result if isinstance(result, str) else json.dumps(result)
    return len(json.dumps({"result": serialized}).encode())


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
        self.per_call_bytes = min(per_call_bytes, 65536)
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

    @contextmanager
    def _open(self, rel: str):
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
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ToolError("only regular repository files are readable")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                yield stream
        except (OSError, ValueError):
            raise ToolError("repository file is unavailable or unsafe") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def _chunk(self, stream, size: int, *, line: bool = False) -> bytes:
        if self._used >= self.per_run_bytes:
            if stream.tell() == os.fstat(stream.fileno()).st_size:
                return b""
            raise ToolError("per-run byte budget exceeded")
        size = min(size, self.per_run_bytes - self._used + 1)
        data = stream.readline(size) if line else stream.read(size)
        self._charge(len(data))
        return data

    def _read(self, rel: str, remaining: int) -> tuple[bytes, bool] | None:
        with self._open(rel) as stream:
            if os.fstat(stream.fileno()).st_size > self.per_call_bytes:
                return None
            data = self._chunk(stream, remaining + 1)
        return data[:remaining], len(data) > remaining

    def read_file(self, rel: str, start_line: int = 1, max_lines: int = 400) -> str:
        if start_line < 1 or max_lines < 1 or max_lines > 400:
            raise ToolError("invalid line limits")
        with self._budget(), self._open(rel) as stream:
            skipped = 0
            remaining = self.per_call_bytes
            while skipped < start_line - 1:
                if remaining == 0:
                    raise ToolError("per-call byte budget exceeded")
                data = self._chunk(stream, remaining, line=True)
                remaining -= len(data)
                if not data:
                    return ""
                skipped += data.endswith(b"\n")
            window = bytearray()
            lines = 0
            while remaining > 0 and lines < max_lines:
                data = self._chunk(stream, remaining, line=True)
                remaining -= len(data)
                if not data:
                    break
                window.extend(data)
                lines += data.endswith(b"\n")
            size = os.fstat(stream.fileno()).st_size
            truncated = stream.tell() < size and (
                size > self.per_call_bytes or len(window) == self.per_call_bytes
            )
        text = window.decode("utf-8", errors="replace")
        marker = "\n[truncated: request a later start_line or smaller window]"
        while response_bytes(text + (marker if truncated else "")) > 32768:
            truncated = True
            text = text[: len(text) // 2]
        return text + (marker if truncated else "")

    def grep(
        self, pattern: str, glob: str = "**/*", max_results: int = 200
    ) -> list[tuple[str, int, str]]:
        if max_results < 1 or max_results > 200:
            raise ToolError("invalid result limit")
        results = []
        remaining = self.per_call_bytes

        def append(path: str, number: int, line: str) -> bool:
            marker = ("", 0, "[truncated]")
            candidate = (path, number, line)
            encoded = json.dumps(results + [candidate, marker], ensure_ascii=False).encode()
            if (
                len(encoded) > self.per_call_bytes
                or response_bytes(results + [candidate, marker]) > 32768
            ):
                if (
                    len(json.dumps(results + [marker], ensure_ascii=False).encode())
                    <= self.per_call_bytes
                    and response_bytes(results + [marker]) <= 32768
                ):
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
                read = self._read(rel, remaining)
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
                        if pattern in line and not append(rel, number, line):
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
