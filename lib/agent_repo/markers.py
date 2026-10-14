import re
from dataclasses import dataclass

_ROLE = r"[a-z_]+"
_RUN_ID = r"[A-Za-z0-9_.-]+"
_MARKER = re.compile(rf"<!-- agent:({_ROLE}) run=({_RUN_ID}) -->")


@dataclass(frozen=True)
class Marker:
    role: str
    run_id: str


def marker(role: str, run_id: str) -> str:
    if not re.fullmatch(_ROLE, role) or not re.fullmatch(_RUN_ID, run_id):
        raise ValueError("invalid marker fields")
    return f"<!-- agent:{role} run={run_id} -->"


def find_markers(text: str) -> list[Marker]:
    return [Marker(role, run_id) for role, run_id in _MARKER.findall(text)]
