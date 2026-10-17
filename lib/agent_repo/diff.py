import fnmatch
import re
import shlex


def _path(value: str, *, strip_prefix: bool = True) -> str | None:
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return None
    if len(parts) != 1 or parts[0] == "/dev/null":
        return None
    path = parts[0]
    return path[2:] if strip_prefix and path.startswith(("a/", "b/")) else path


def diff_index(diff: str) -> dict[str, set[int]]:
    """Index right-side lines represented by added or context diff rows."""
    result: dict[str, set[int]] = {}
    right_path = None
    right_line = 0
    in_hunk = False

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            try:
                parts = shlex.split(line[len("diff --git ") :], posix=True)
            except ValueError:
                right_path = None
                continue
            right_path = _path(parts[1]) if len(parts) == 2 else None
        elif line.startswith("rename to "):
            right_path = _path(line[len("rename to ") :], strip_prefix=False)
        elif line.startswith("+++ "):
            right_path = _path(line[4:])
            in_hunk = False
        elif match := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line):
            right_line = int(match[1])
            in_hunk = right_path is not None
        elif in_hunk and line.startswith((" ", "+")):
            result.setdefault(right_path, set()).add(right_line)
            right_line += 1
    return result


def _diff_paths(diff: str) -> set[str]:
    paths = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            try:
                parts = shlex.split(line[len("diff --git ") :], posix=True)
            except ValueError:
                continue
            if len(parts) == 2:
                paths.update(path for value in parts if (path := _path(value)) is not None)
        elif line.startswith(("rename from ", "rename to ")):
            path = _path(line.split(" ", 2)[2], strip_prefix=False)
            if path is not None:
                paths.add(path)
    return paths


def summary_only(diff: str, config) -> tuple[bool, str]:
    maximum = config.raw["limits"]["review_max_lines"]
    line_count = len(diff.splitlines())
    if line_count > maximum:
        return True, f"Diff has {line_count} lines; review_max_lines is {maximum}."
    paths = _diff_paths(diff)
    sensitive = [
        area.name
        for area in config.areas
        if area.sensitive
        and any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in area.paths)
    ]
    if sensitive:
        names = ", ".join(dict.fromkeys(sensitive))
        return True, f"Sensitive area ({names}) requires maintainer review: needs-maintainer."
    return False, ""
