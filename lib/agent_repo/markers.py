import re
from dataclasses import dataclass

_MARKER = re.compile(r"<!-- agent:([a-z_]+) run=([A-Za-z0-9_.-]+) -->")


@dataclass(frozen=True)
class Marker:
    role: str
    run_id: str


def marker(role: str, run_id: str) -> str:
    if not re.fullmatch(r"[a-z_]+", role) or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("invalid marker fields")
    return f"<!-- agent:{role} run={run_id} -->"


def find_markers(text: str) -> list[Marker]:
    return [Marker(role, run_id) for role, run_id in _MARKER.findall(text)]
