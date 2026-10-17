import pytest


@pytest.mark.parametrize(
    "text",
    [
        "before <!-- agent:triage run=1 --> after",
        "before <!-- multiline\nmarker --> after",
        "before <!-- unfinished",
    ],
)
def test_comments_are_stripped(text):
    from agent_repo.render import neutralize

    result = neutralize(text, 200)
    assert "<!--" not in result
    assert "agent:triage" not in result


def test_mentions_are_broken():
    from agent_repo.render import neutralize

    assert neutralize("@octocat", 100) == "@\u200boctocat"


def test_fence_cannot_be_closed_inside():
    from agent_repo.render import fenced

    result = fenced("```\nattack\n``````", 200)
    opening, *inside, closing = result.splitlines()
    assert opening == closing
    assert len(opening) > max(len(line) for line in inside if set(line) == {"`"})


@pytest.mark.parametrize("limit", [0, 1, 20, -1])
def test_cap_is_applied(limit):
    from agent_repo.render import fenced, neutralize

    assert len(neutralize("@octocat" * 50, limit)) <= max(0, limit)
    assert len("\n".join(fenced("x" * 100, limit).splitlines()[1:-1])) <= max(0, limit)


def test_unmatched_comment_closer_is_neutralized():
    from agent_repo.render import neutralize

    result = neutralize("report --> pending", 100)
    assert "-->" not in result
    assert "report" in result and "pending" in result
