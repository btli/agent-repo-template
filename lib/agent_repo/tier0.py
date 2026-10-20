import re
from dataclasses import dataclass

from agent_repo.event import Event
from agent_repo.github import GitHub

_TITLE_STOPWORDS = frozenset(
    {
        "a",
        "able",
        "an",
        "and",
        "app",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "not",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "were",
        "when",
        "with",
    }
)


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    similarity: float


def missing_fields(body: str, required: tuple[str, ...]) -> list[str]:
    body = _without_fenced_code(body)
    sections = {
        match.group(1).strip(): body[match.end() : next_start].strip()
        for match, next_start in _heading_spans(body)
    }
    return [
        field for field in required if sections.get(field, "").casefold() in {"", "_no response_"}
    ]


def has_field_heading(body: str, fields: tuple[str, ...]) -> bool:
    body = _without_fenced_code(body)
    return any(match.group(1).strip() in fields for match, _ in _heading_spans(body))


def _without_fenced_code(body: str) -> str:
    lines = []
    fence_character = None
    fence_length = 0
    for line in body.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        line_ending = line[len(content) :]
        if fence_character is not None:
            closing_fence = rf"[ \t]{{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*"
            if re.fullmatch(closing_fence, content):
                fence_character = None
                fence_length = 0
            lines.append(line_ending)
            continue

        opening_fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", content)
        if opening_fence:
            marker = opening_fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            lines.append(line_ending)
        else:
            lines.append(line)
    return "".join(lines)


def _heading_spans(body: str):
    headings = list(re.finditer(r"^###\s+(.+?)\s*$", body, re.MULTILINE))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        yield heading, end


def security_hit(text: str) -> bool:
    return (
        re.search(
            r"\b(?:CVE-\d{4}-\d{4,}|remote\s+code\s+execution|exploit(?:s|ed|ation)?)\b",
            text,
            re.IGNORECASE,
        )
        is not None
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def lexical_similarity(a: str, b: str) -> float:
    left = _tokens(a)
    right = _tokens(b)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def duplicate_candidates(gh: GitHub, event: Event, k: int = 5) -> list[Candidate]:
    k = min(k, 5)
    if k <= 0:
        return []
    title_terms = list(dict.fromkeys(re.findall(r"[a-z0-9]+", event.title.casefold())))
    terms = sorted(
        (term for term in title_terms if len(term) >= 3 and term not in _TITLE_STOPWORDS),
        key=len,
        reverse=True,
    )[:4]
    if not terms:
        return []
    query = f"repo:{event.repo} is:issue is:open"
    query += " (" + " OR ".join(terms) + ")"
    result = gh.request(
        "GET",
        "/search/issues",
        params={"q": query, "per_page": 100},
    )
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return []
    candidates = []
    source = f"{event.title}\n{event.body}"
    for item in result["items"]:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        number = item.get("number")
        title = item.get("title")
        if type(number) is not int or number == event.number or not isinstance(title, str):
            continue
        body = item.get("body")
        candidate_text = f"{title}\n{body if isinstance(body, str) else ''}"
        similarity = lexical_similarity(source, candidate_text)
        if similarity > 0:
            candidates.append(Candidate(number, title, similarity))
    candidates.sort(key=lambda candidate: (-candidate.similarity, candidate.number))
    return candidates[:k]
