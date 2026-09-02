"""The highlight's coordinate conversion.

Two transforms have to compose correctly or the box lands somewhere wrong on
screen, which is worse than drawing nothing: Retina pixels to points, and
screencapture's top-left origin to AppKit's bottom-left one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSScreen                                # noqa: E402

from slicer.blocks import Box                              # noqa: E402
from slicer.capture import Capture                         # noqa: E402
from slicer.overlay import PADDING, to_screen_frame        # noqa: E402

TOP = max(s.frame().origin.y + s.frame().size.height for s in NSScreen.screens())


def _capture(scale: float = 1.0, **kw) -> Capture:
    return Capture(path="x", width=800, height=600, origin_x=50, origin_y=200,
                   scale=scale, origin_known=True, **kw)


def test_a_block_maps_to_the_right_place_on_screen():
    frame = to_screen_frame(Box(10, 100, 300, 20), _capture())
    assert frame.origin.x == 50 + 10 - PADDING
    assert frame.origin.y == TOP - (200 + 100 + 20 + PADDING)
    assert frame.size.width == 300 + PADDING * 2


def test_retina_pixels_become_points():
    """A 2x capture has twice the pixels for the same screen area."""
    one = to_screen_frame(Box(10, 100, 300, 20), _capture(1.0))
    two = to_screen_frame(Box(20, 200, 600, 40), _capture(2.0))
    assert (one.origin.x, one.origin.y) == (two.origin.x, two.origin.y)
    assert (one.size.width, one.size.height) == (two.size.width, two.size.height)


def test_a_capture_with_no_known_origin_draws_nothing():
    """Better no highlight than a highlight in the wrong place."""
    unplaceable = Capture(path="x", width=10, height=10, origin_known=False)
    assert to_screen_frame(Box(0, 0, 10, 10), unplaceable) is None


def test_the_highlight_never_appears_in_our_own_captures():
    """Otherwise a following read recognizes the highlight as content."""
    import slicer.overlay as overlay
    source = open(overlay.__file__).read()
    assert "setSharingType_(NSWindowSharingNone)" in source
    assert "setIgnoresMouseEvents_(True)" in source


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mpass\033[0m  {name}")
        except Exception as exc:
            failed.append(name)
            print(f"  \033[31mFAIL\033[0m  {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
