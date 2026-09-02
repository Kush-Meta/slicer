"""The highlight that follows the reading.

A translucent rectangle drawn over the block currently being spoken. It is the
one part of Slicer that makes what it is doing legible from across a room: you
watch the box move down the left column and then jump to the top of the right
one, and the reading-order work becomes visible rather than merely audible.

Three properties matter and each is one line that is easy to omit:

  * `ignoresMouseEvents` - the overlay sits above everything, so without this
    it eats every click on the screen.
  * `sharingType = none` - otherwise the highlight appears in Slicer's own
    next capture, and during a following read it would be recognized as
    content. This is a correctness requirement, not cosmetics.
  * `canJoinAllSpaces` - a reading should survive the user switching desktops.

Coordinates arrive as pixels inside a capture and have to become points on the
screen. Two conversions: divide by the capture's scale factor to go from
Retina pixels to points, then flip from screencapture's top-left origin to
AppKit's bottom-left one.
"""

from __future__ import annotations

from AppKit import (
    NSBackingStoreBuffered, NSBezierPath, NSColor, NSMakeRect, NSScreen, NSView,
    NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
)

from .blocks import Box
from .capture import Capture

NSStatusWindowLevel = 25
NSWindowSharingNone = 0

# Drawn a little larger than the text so glyph edges are not clipped by it.
PADDING = 6
CORNER = 5


class _HighlightView(NSView):
    def drawRect_(self, rect) -> None:
        bounds = self.bounds()
        inset = NSMakeRect(1.5, 1.5, bounds.size.width - 3, bounds.size.height - 3)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            inset, CORNER, CORNER
        )
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.27, 0.75, 0.78, 0.16).setFill()
        path.fill()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.27, 0.75, 0.78, 0.95).setStroke()
        path.setLineWidth_(2.0)
        path.stroke()


class Highlight:
    """A single reusable overlay window that moves to each block in turn."""

    def __init__(self) -> None:
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 10, 10), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False,
        )
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setLevel_(NSStatusWindowLevel)
        self._window.setIgnoresMouseEvents_(True)
        self._window.setSharingType_(NSWindowSharingNone)
        self._window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )
        self._window.setContentView_(_HighlightView.alloc().initWithFrame_(
            NSMakeRect(0, 0, 10, 10)
        ))
        self._visible = False

    def show(self, box: Box, capture: Capture) -> None:
        """Move the highlight over `box`, which is in capture pixel space."""
        frame = to_screen_frame(box, capture)
        if frame is None:
            return
        self._window.setFrame_display_(frame, True)
        self._window.contentView().setFrame_(
            NSMakeRect(0, 0, frame.size.width, frame.size.height)
        )
        self._window.contentView().setNeedsDisplay_(True)
        if not self._visible:
            self._window.orderFrontRegardless()
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self._window.orderOut_(None)
            self._visible = False


def to_screen_frame(box: Box, capture: Capture):
    """Capture pixels to an AppKit window frame, or None if unplaceable.

    A capture whose origin is unknown - one read from a file, say - cannot be
    mapped back onto the screen, and the honest answer is to draw nothing
    rather than to draw a box in the wrong place.
    """
    if not capture.origin_known:
        return None
    scale = capture.scale or 1.0

    # Pixels inside the capture -> points on screen, top-left origin.
    x = capture.origin_x + box.x / scale
    y = capture.origin_y + box.y / scale
    w = box.w / scale
    h = box.h / scale

    x, y = x - PADDING, y - PADDING
    w, h = w + PADDING * 2, h + PADDING * 2

    # Top-left origin -> AppKit's bottom-left origin.
    screens = NSScreen.screens()
    if not screens:
        return None
    top = max(s.frame().origin.y + s.frame().size.height for s in screens)
    return NSMakeRect(x, top - (y + h), w, h)
