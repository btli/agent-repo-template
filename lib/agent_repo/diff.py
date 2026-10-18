import fnmatch
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


def _quoted_path(value: str) -> tuple[str, str] | None:
    if not value.startswith('"'):
        return None
    result = bytearray()
    index = 1
    escapes = {
        '"': b'"',
        "\\": b"\\",
        "a": b"\a",
        "b": b"\b",
        "t": b"\t",
        "n": b"\n",
        "v": b"\v",
        "f": b"\f",
        "r": b"\r",
    }
    while index < len(value):
        char = value[index]
        index += 1
        if char == '"':
            suffix = value[index:]
            return result.decode("utf-8", errors="surrogateescape"), suffix
        if char != "\\":
            result.extend(char.encode("utf-8", errors="surrogateescape"))
            continue
        if index >= len(value):
            return None
        escaped = value[index]
        index += 1
        if escaped in escapes:
            result.extend(escapes[escaped])
        elif escaped in "01234567":
            digits = escaped
            while index < len(value) and len(digits) < 3 and value[index] in "01234567":
                digits += value[index]
                index += 1
            result.append(int(digits, 8))
        else:
            return None
    return None


def _path(value: str, *, strip_prefix: bool = True) -> str | None:
    value = value.removesuffix("\r")
    if value.startswith('"'):
        parsed = _quoted_path(value)
        if parsed is None or (parsed[1] and not parsed[1].startswith("\t")):
            return None
        path = parsed[0]
    else:
        path = value.partition("\t")[0]
    if not path:
        return None
    if path == "/dev/null":
        return None
    if strip_prefix and path.startswith(("a/", "b/")):
        path = path[2:]
    return path or None


def _git_header_paths(line: str) -> tuple[str | None, str | None] | None:
    value = line.removeprefix("diff --git ")
    tokens = []
    while value:
        value = value.lstrip(" ")
        if not value:
            break
        if value.startswith('"'):
            parsed = _quoted_path(value)
            if parsed is None:
                return None
            token, suffix = parsed
            tokens.append(token)
            value = suffix.lstrip(" ")
        else:
            token, separator, value = value.partition(" ")
            tokens.append(token)
            if not separator:
                value = ""
        if len(tokens) > 2:
            return None
    if len(tokens) != 2:
        return None
    left = _path(tokens[0])
    right = _path(tokens[1])
    if left is None or right is None:
        return None
    return left, right


def _diff_lines(diff: str) -> list[str]:
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _parse_diff(diff: str) -> tuple[dict[str, set[int]], set[str], bool]:
    index: dict[str, set[int]] = {}
    paths: set[str] = set()
    invalid = False
    file_open = False
    file_header_paths = False
    left_header_seen = right_header_seen = False
    hunk_started = False
    left_path = None
    right_path = None
    renamed_from = None
    renamed_to = None
    in_hunk = False
    old_remaining = new_remaining = 0
    right_line = 0

    def finish_file() -> None:
        nonlocal invalid, file_open, file_header_paths, left_header_seen, right_header_seen
        nonlocal hunk_started
        nonlocal left_path, right_path
        nonlocal renamed_from, renamed_to
        if not file_open:
            return
        if file_header_paths:
            if not left_header_seen or not right_header_seen:
                invalid = True
            if left_path is not None:
                paths.add(left_path)
            if right_path is not None:
                paths.add(right_path)
        elif renamed_from is not None and renamed_to is not None:
            paths.update((renamed_from, renamed_to))
        else:
            header_paths = _git_header_paths(current_header)
            if header_paths is None:
                invalid = True
            else:
                paths.update(header_paths)
        file_open = False
        file_header_paths = False
        left_header_seen = right_header_seen = False
        hunk_started = False
        left_path = right_path = None
        renamed_from = renamed_to = None

    current_header = ""
    for line in _diff_lines(diff):
        if in_hunk:
            if old_remaining == 0 and new_remaining == 0:
                in_hunk = False
            else:
                if line.startswith(" "):
                    old_remaining -= 1
                    new_remaining -= 1
                    if right_path is not None:
                        index.setdefault(right_path, set()).add(right_line)
                    right_line += 1
                elif line.startswith("+"):
                    new_remaining -= 1
                    if right_path is not None:
                        index.setdefault(right_path, set()).add(right_line)
                    right_line += 1
                elif line.startswith("-"):
                    old_remaining -= 1
                elif line.startswith("\\"):
                    continue
                else:
                    invalid = True
                    in_hunk = False
                if old_remaining < 0 or new_remaining < 0:
                    invalid = True
                    in_hunk = False
                if in_hunk:
                    continue

        if line.startswith("diff --git "):
            finish_file()
            current_header = line
            file_open = True
        elif line.startswith("--- ") and file_open:
            if hunk_started or left_header_seen:
                invalid = True
                continue
            parsed = _path(line[4:])
            if parsed is None and line[4:].partition("\t")[0] != "/dev/null":
                invalid = True
            left_path = parsed
            file_header_paths = True
            left_header_seen = True
        elif line.startswith("+++ ") and file_open:
            if hunk_started or right_header_seen:
                invalid = True
                continue
            parsed = _path(line[4:])
            if parsed is None and line[4:].partition("\t")[0] != "/dev/null":
                invalid = True
            right_path = parsed
            file_header_paths = True
            right_header_seen = True
        elif line.startswith("rename from ") and file_open:
            renamed_from = _path(line.removeprefix("rename from "), strip_prefix=False)
            if renamed_from is None:
                invalid = True
        elif line.startswith("rename to ") and file_open:
            renamed_to = _path(line.removeprefix("rename to "), strip_prefix=False)
            if renamed_to is None:
                invalid = True
        elif line.startswith("@@"):
            hunk_started = True
            match = _HUNK.match(line)
            if match is None or right_path is None:
                invalid = True
                continue
            right_line = int(match[3])
            old_remaining = int(match[2] or "1")
            new_remaining = int(match[4] or "1")
            in_hunk = old_remaining > 0 or new_remaining > 0
    if in_hunk and (old_remaining or new_remaining):
        invalid = True
    finish_file()
    return index, paths, invalid


def diff_index(diff: str) -> dict[str, set[int]]:
    """Index right-side lines represented by added or context diff rows."""
    index, _, invalid = _parse_diff(diff)
    return {} if invalid else index


def summary_only(diff: str, config) -> tuple[bool, str]:
    maximum = config.raw["limits"]["review_max_lines"]
    line_count = len(_diff_lines(diff))
    if line_count > maximum:
        return True, f"Diff has {line_count} lines; review_max_lines is {maximum}."
    byte_count = len(diff.encode("utf-8"))
    if byte_count > 120 * 1024:
        return True, f"Diff has {byte_count} bytes; prompt budget is 122880 bytes."
    _, paths, invalid = _parse_diff(diff)
    if invalid:
        return True, "Diff paths could not be parsed unambiguously; needs-maintainer."
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
