import fnmatch
import json
import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_STATUSES = {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
_NO_NEWLINE = "\\ No newline at end of file"
_NEEDS_MAINTAINER = "File list or patch could not be reviewed safely; needs-maintainer."


def _valid_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character in value for character in "\x00\r\n")
        and not value.startswith("/")
    )


def _validate_files(files: object) -> list[dict]:
    if not isinstance(files, list):
        raise ValueError("invalid pull request files")
    validated = []
    filenames = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("invalid pull request file")
        filename = entry.get("filename")
        previous = entry.get("previous_filename")
        status = entry.get("status")
        additions = entry.get("additions")
        deletions = entry.get("deletions")
        changes = entry.get("changes")
        if not _valid_path(filename) or filename in filenames:
            raise ValueError("invalid pull request filename")
        if previous is not None and not _valid_path(previous):
            raise ValueError("invalid previous pull request filename")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("invalid pull request file status")
        if any(type(count) is not int or count < 0 for count in (additions, deletions, changes)):
            raise ValueError("invalid pull request change counts")
        if changes != additions + deletions:
            raise ValueError("inconsistent pull request change counts")
        if status in {"renamed", "copied"} and previous is None:
            raise ValueError("moved pull request file has no previous filename")
        if status not in {"renamed", "copied"} and previous is not None:
            raise ValueError("unexpected previous pull request filename")
        if "patch" in entry and not isinstance(entry["patch"], str):
            raise ValueError("invalid pull request patch")
        filenames.add(filename)
        validated.append(entry)
    return validated


def fetch_files(gh, number: int) -> list[dict]:
    """Fetch and validate the structured file list for one pull request."""
    files = gh.paginate(f"pulls/{number}/files", {"per_page": 100})
    return _validate_files(files)


def render_files(files: list[dict]) -> str:
    rendered = [
        {
            key: entry[key]
            for key in (
                "filename",
                "previous_filename",
                "status",
                "additions",
                "deletions",
                "patch",
            )
            if key in entry
        }
        for entry in _validate_files(files)
    ]
    return json.dumps(rendered, indent=2, ensure_ascii=False) + "\n"


def _parse_patch(patch: str) -> tuple[dict[int, None], bool]:
    index: dict[int, None] = {}
    lines = [line.removesuffix("\r") for line in patch.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    old_remaining = new_remaining = 0
    right_line = 0
    in_hunk = False
    saw_hunk = False
    invalid = False
    for line in lines:
        if in_hunk and old_remaining == 0 and new_remaining == 0:
            in_hunk = False
        if line.startswith("@@"):
            if in_hunk:
                invalid = True
            match = _HUNK.fullmatch(line)
            if match is None:
                invalid = True
                in_hunk = False
                continue
            saw_hunk = True
            right_line = int(match[3])
            old_remaining = int(match[2] or "1")
            new_remaining = int(match[4] or "1")
            in_hunk = old_remaining > 0 or new_remaining > 0
        elif line == _NO_NEWLINE and saw_hunk:
            continue
        elif not in_hunk:
            invalid = True
        elif line.startswith(" "):
            old_remaining -= 1
            new_remaining -= 1
            index[right_line] = None
            right_line += 1
        elif line.startswith("+"):
            new_remaining -= 1
            index[right_line] = None
            right_line += 1
        elif line.startswith("-"):
            old_remaining -= 1
        else:
            invalid = True
        if old_remaining < 0 or new_remaining < 0:
            invalid = True
            in_hunk = False
    if not saw_hunk or old_remaining or new_remaining:
        invalid = True
    return index, invalid


def diff_index(files: list[dict]) -> dict[str, set[int]]:
    """Index RIGHT-side lines represented by added or context rows in file patches."""
    try:
        validated = _validate_files(files)
    except ValueError:
        return {}
    index: dict[str, set[int]] = {}
    for entry in validated:
        patch = entry.get("patch")
        if patch is None:
            if entry["changes"]:
                return {}
            continue
        if not patch:
            if entry["changes"]:
                return {}
            continue
        lines, invalid = _parse_patch(patch)
        if invalid:
            return {}
        index[entry["filename"]] = set(lines)
    return index


def summary_only(files: list[dict], config) -> tuple[bool, str]:
    try:
        validated = _validate_files(files)
    except ValueError:
        return True, _NEEDS_MAINTAINER
    if len(validated) > 3000:
        return True, "Pull request file list exceeds the API limit; needs-maintainer."
    if len(render_files(validated).encode("utf-8")) > 120 * 1024:
        return True, "Pull request files exceed the prompt budget; needs-maintainer."
    changed_lines = sum(entry["additions"] + entry["deletions"] for entry in validated)
    maximum = config.raw["limits"]["review_max_lines"]
    if changed_lines > maximum:
        return True, f"Pull request changes {changed_lines} lines; review_max_lines is {maximum}."
    paths = [
        path
        for entry in validated
        for path in (entry["filename"], entry.get("previous_filename"))
        if path is not None
    ]
    sensitive = [
        area.name
        for area in config.areas
        if area.sensitive
        and any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in area.paths)
    ]
    if sensitive:
        names = ", ".join(dict.fromkeys(sensitive))
        return True, f"Sensitive area ({names}) requires maintainer review: needs-maintainer."
    for entry in validated:
        patch = entry.get("patch")
        if entry["changes"] and patch is None:
            return True, f"Patch unavailable for {entry['filename']}; needs-maintainer."
        if patch:
            _, invalid = _parse_patch(patch)
            if invalid:
                return True, f"Invalid patch for {entry['filename']}; needs-maintainer."
    return False, ""
