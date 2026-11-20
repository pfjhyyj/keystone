"""Single byte ranges are parsed without storage or HTTP I/O."""

import pytest

from keystone.core.content_response import byte_range


@pytest.mark.parametrize(
    "value,expected",
    [
        ("bytes=0-0", (0, 0)),
        ("bytes=7-", (7, 9)),
        ("bytes=-3", (7, 9)),
        ("bytes=8-99", (8, 9)),
        ("bytes=-99", (0, 9)),
    ],
)
def test_satisfiable_ranges_are_clamped_to_the_content(value, expected):
    assert byte_range(value, 10) == expected


@pytest.mark.parametrize(
    "value",
    ["bytes=10-", "bytes=4-2", "bytes=-0", "bytes=", "bytes=0-1,4-5", "items=0-1"],
)
def test_unsatisfiable_multipart_and_other_units_are_rejected(value):
    with pytest.raises(ValueError):
        byte_range(value, 10)


def test_empty_content_has_no_satisfiable_range():
    with pytest.raises(ValueError):
        byte_range("bytes=0-", 0)
