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
