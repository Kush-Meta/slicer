"""Reading past the fold.

Anything worth reading is taller than the viewport, so a reading is capture,
scroll, re-capture, resume - and the resume is the hard part. This module owns
the scrolling and the decision about where to pick up.

Scrolling is done by posting scroll wheel events, which macOS gates behind
Accessibility permission. When that permission is missing, Slicer does not
fail: it asks the reader to scroll and waits for the content to change. That
degradation is deliberate. It is also the better interaction in one respect -
the pre-mortem lists "user scrolls while it reads" as a common failure, and a
reader that follows the viewport instead of fighting for it cannot hit it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .capture import Capture, CaptureError

# Scroll less than a full viewport, so consecutive captures always overlap and
# the fingerprints have something to align on.
SCROLL_FRACTION = 0.72
# Stop rather than follow a feed that generates content faster than we read it.
MAX_SCREENS = 40


def accessibility_granted() -> bool:
    """Whether this process may synthesize scroll events."""
    try:
        from ApplicationServices import AXIsProcessTrusted  # noqa: PLC0415
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def scroll_region(region: tuple[int, int, int, int], *, fraction: float = SCROLL_FRACTION,
                  settle: float = 0.35) -> bool:
    """Scroll down over the middle of `region`. False if not permitted.

    The event is aimed at the centre of the region rather than wherever the
    pointer happens to be, so the scroll lands in the content being read.
    """
    if not accessibility_granted():
        return False
    try:
        import Quartz  # noqa: PLC0415
    except ImportError:
        return False

    x, y, w, h = region
    pixels = max(int(h * fraction), 40)
    centre = Quartz.CGPointMake(x + w / 2, y + h / 2)

    # Several smaller steps rather than one jump: momentum scrolling in many
    # apps overshoots a single large delta, and overshoot loses content.
    steps = 6
    per_step = max(pixels // steps, 1)
    for _ in range(steps):
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, -per_step
        )
        Quartz.CGEventSetLocation(event, centre)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.012)

    time.sleep(settle)
    return True


@dataclass
class Advance:
    """The outcome of trying to move to the next screenful."""

    capture: Capture | None
    reason: str = ""
    manual: bool = False

    @property
    def ok(self) -> bool:
        return self.capture is not None


class Scroller:
    """Advances a reading to the next screenful of the same region."""

    def __init__(self, source: Capture):
        if source.region is None:
            raise CaptureError(
                "this capture did not record where it came from, so it cannot be "
                "continued",
                remedy="Select the region with Slicer's picker rather than passing a file.",
            )
        self.region = source.region
        self.screens = 1

    def advance(self) -> Advance:
        if self.screens >= MAX_SCREENS:
            return Advance(None, f"stopped after {MAX_SCREENS} screens")
        self.screens += 1
        if not scroll_region(self.region):
            return Advance(None, "scrolling needs Accessibility permission", manual=True)
        from .capture import capture_region  # noqa: PLC0415
        x, y, w, h = self.region
        return Advance(capture_region(x, y, w, h, stability_check=False))
