import pytest
from agent_repo.markers import Marker, find_markers, marker


def test_marker_round_trip():
    value = marker("triage", "run-123")
    assert value == "<!-- agent:triage run=run-123 -->"
    assert find_markers(value) == [Marker("triage", "run-123")]


def test_finds_multiple_markers_in_other_text():
    text = f"before {marker('review', 'a1')} between {marker('fix', 'b2')} after"
    assert find_markers(text) == [Marker("review", "a1"), Marker("fix", "b2")]


def test_malformed_markers_are_ignored():
    malformed = "<!-- agent:bad role run=x --> <!-- agent:triage --> <!-- agent:fix run=partial"
    assert find_markers(malformed) == []


@pytest.mark.parametrize(
    ("role", "run_id"),
    [("tri age", "run-1"), ("", "run-1"), ("triage", "x --> y"), ("triage", "")],
)
def test_marker_rejects_invalid_fields(role: str, run_id: str):
    with pytest.raises(ValueError, match="invalid marker fields"):
        marker(role, run_id)


def test_find_markers_ignores_run_ids_outside_allowed_characters():
    assert find_markers("<!-- agent:triage run=a/b -->") == []
