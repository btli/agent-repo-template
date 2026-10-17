import re
from dataclasses import dataclass

REQUIRED_HEADINGS = (
    "Mission",
    "In scope",
    "Out of scope",
    "Priorities",
    "A good report includes",
    "Support policy",
    "Docs index",
)


@dataclass(frozen=True)
class ScopeItem:
    text: str
    doc: str | None


@dataclass(frozen=True)
class DocRef:
    title: str
    url: str


@dataclass(frozen=True)
class Charter:
    mission: str
    in_scope: tuple[str, ...]
    out_of_scope: tuple[ScopeItem, ...]
    priorities: dict[str, str]
    good_report: tuple[str, ...]
    support_policy: str
    docs_index: tuple[DocRef, ...]


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end() : end].strip()
    return sections


def _items(text: str) -> tuple[str, ...]:
    return tuple(
        match.group(1).strip() for match in re.finditer(r"^\s*[-*]\s+(.+?)\s*$", text, re.MULTILINE)
    )


def _priorities(text: str) -> dict[str, str]:
    return dict(
        re.findall(r"^[ \t]*[-*]?[ \t]*(P[0-3])[ \t]*:[ \t]*(\S.*?)[ \t]*$", text, re.MULTILINE)
    )


def lint_charter(text: str) -> list[str]:
    sections = _sections(text)
    missing = [heading for heading in REQUIRED_HEADINGS if heading not in sections]
    if missing:
        return [f"missing required heading: {heading}" for heading in missing]
    if not sections["Mission"]:
        return ["Mission must not be empty"]
    present = _priorities(sections["Priorities"])
    return [f"missing priority: P{number}" for number in range(4) if f"P{number}" not in present]


def parse_charter(text: str) -> Charter:
    errors = lint_charter(text)
    if errors:
        raise ValueError("; ".join(errors))
    sections = _sections(text)
    out_of_scope: list[ScopeItem] = []
    for item in _items(sections["Out of scope"]):
        link = re.search(r"\[docs\]\((https?://[^)]+)\)", item)
        clean_text = re.sub(r"\s*\(\[docs\]\(https?://[^)]+\)\)", "", item).strip()
        out_of_scope.append(ScopeItem(clean_text, link.group(1) if link else None))
    docs = tuple(
        DocRef(title, url)
        for title, url in re.findall(r"\[([^]]+)\]\((https?://[^)]+)\)", sections["Docs index"])
    )
    return Charter(
        sections["Mission"],
        _items(sections["In scope"]),
        tuple(out_of_scope),
        _priorities(sections["Priorities"]),
        _items(sections["A good report includes"]),
        sections["Support policy"],
        docs,
    )
