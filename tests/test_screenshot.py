import pytest
from screenshot import selection_to_pixels


@pytest.mark.parametrize("ratio,expected", [
    (1, (20, 30, 100, 60)),
    (1.25, (25, 37, 125, 76)),
    (1.5, (30, 45, 150, 90)),
    (2, (40, 60, 200, 120)),
])
def test_screen_scale_and_reverse_drag(ratio, expected):
    geometry = (0, 0, 500, 300)
    pixels = (int(500 * ratio), int(300 * ratio))
    assert selection_to_pixels((20, 30), (120, 90), geometry, pixels, ratio) == expected
    assert selection_to_pixels((120, 90), (20, 30), geometry, pixels, ratio) == expected


def test_negative_screen_offset_and_clip_edges():
    assert selection_to_pixels((-480, -70), (-380, -10), (-500, -100, 500, 300),
                               (750, 450), 1.5) == (30, 45, 150, 90)
    assert selection_to_pixels((-600, -200), (100, 300), (-500, -100, 500, 300),
                               (750, 450), 1.5) == (0, 0, 750, 450)
    assert selection_to_pixels((20, 30), (20, 30), (0, 0, 500, 300),
                               (750, 450), 1.5) is None
