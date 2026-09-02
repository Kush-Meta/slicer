"""Region picker: coordinate conversion and the selection state machine.

Dragging is driven directly rather than through synthesized mouse events, so
this runs without Accessibility permission and without a human. What it cannot
cover is that the overlay looks right, which needs eyes.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSMakeRect, NSScreen                    # noqa: E402

from slicer import picker                                  # noqa: E402

MAIN = NSScreen.mainScreen().frame()
HEIGHT = MAIN.size.height


def test_bottom_left_origin_becomes_top_left():
    """AppKit measures y upward from the bottom; screencapture measures down."""
    region = picker.to_capture_space(NSMakeRect(100, 100, 200, 50), MAIN)
    assert (region.x, region.w, region.h) == (100, 200, 50)
    assert region.y == HEIGHT - 150      # 100 from the bottom, 50 tall


def test_a_rect_at_the_top_of_the_screen_has_y_zero():
    region = picker.to_capture_space(NSMakeRect(0, HEIGHT - 50, 200, 50), MAIN)
    assert region.y == 0


def test_round_trip_through_a_drag():
    picker.begin(300, 400, MAIN)
    picker.drag_to(500, 500)
    region = picker.finish()
    assert region is not None
    assert (region.x, region.w, region.h) == (300, 200, 100)
    assert region.y == HEIGHT - 500


def test_dragging_up_and_left_still_yields_a_positive_rect():
    picker.begin(500, 500, MAIN)
    picker.drag_to(300, 400)             # backwards drag
    region = picker.finish()
    assert (region.x, region.w, region.h) == (300, 200, 100)
    assert region.y == HEIGHT - 500


def test_a_stray_click_is_not_a_selection():
    picker.begin(400, 400, MAIN)
    picker.drag_to(402, 401)
    assert picker.finish() is None


def test_region_formats_for_screencapture():
    assert picker.Region(10, 20, 30, 40).as_argument() == "10,20,30,40"


def test_a_capture_remembers_where_it_came_from():
    """Re-capture after a scroll needs the original rectangle.

    Deliberately not a live screen capture: what is on screen is not the test's
    business, and a region that happens to be blank would fail the capture
    validity check for reasons unrelated to what is being asserted. The live
    path is exercised by `slicer doctor`, where nondeterminism is acceptable.
    """
    from slicer.capture import Capture
    capture = Capture(path="/tmp/x.png", width=640, height=400,
                      origin_x=10, origin_y=20, region=(10, 20, 320, 200))
    assert capture.region == (10, 20, 320, 200)
    assert capture.origin_known


def test_a_capture_without_a_region_refuses_to_recapture():
    from slicer.capture import Capture, CaptureError
    capture = Capture(path="/tmp/x.png", width=10, height=10, origin_known=False)
    try:
        capture.recapture()
    except CaptureError as exc:
        assert "where it came from" in str(exc)
        return
    raise AssertionError("recapture succeeded without a region")


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
