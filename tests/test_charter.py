import re
from pathlib import Path

import pytest
from agent_repo.charter import REQUIRED_HEADINGS, ScopeItem, lint_charter, parse_charter

EXAMPLE = Path("tests/fixtures/charter/example.md")


def test_example_parses_all_seven_sections():
    charter = parse_charter(EXAMPLE.read_text())
    assert charter.mission
    assert len(REQUIRED_HEADINGS) == 7
    assert charter.in_scope
    assert charter.out_of_scope
    assert charter.priorities.keys() == {"P0", "P1", "P2", "P3"}
    assert charter.good_report
    assert charter.support_policy
    assert charter.docs_index


def test_missing_heading_returns_one_named_error():
    text = re.sub(r"(?ms)^## Priorities\n.*?(?=^## |\Z)", "", EXAMPLE.read_text())
    errors = lint_charter(text)
    assert len(errors) == 1
    assert "Priorities" in errors[0]


def test_out_of_scope_doc_link_is_extracted():
    text = EXAMPLE.read_text().replace("Cloud sync", "Cloud sync ([docs](https://x/y))")
    charter = parse_charter(text)
    assert ScopeItem(text="Cloud sync", doc="https://x/y") in charter.out_of_scope


def test_priorities_parse_p0_through_p3_and_require_each():
    charter = parse_charter(EXAMPLE.read_text())
    assert tuple(charter.priorities) == ("P0", "P1", "P2", "P3")
    text = EXAMPLE.read_text().replace("P2:", "P8:")
    assert lint_charter(text) == ["missing priority: P2"]
    with pytest.raises(ValueError, match="P2"):
        parse_charter(text)
