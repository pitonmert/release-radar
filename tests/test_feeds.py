
import pytest

from release_radar import feeds


@pytest.mark.parametrize(("entry", "expected"), [
    ({"published_parsed": (2026, 9, 19, 0, 0, 0, 0, 0, 0)}, "19.09.2026"),
    ({}, None),
    ({"updated_parsed": (2026, 9, 19, 0, 0, 0, 0, 0, 0)}, None),
    ({"published_parsed": (2026, 13, 40)}, None),
])
def test_publication_date_does_not_invent_a_date(entry, expected):
    assert feeds.entry_publication_date(entry) == expected
