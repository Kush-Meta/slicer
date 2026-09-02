"""Drag a region and get back exactly where it was.

`screencapture -i` gives a good selection experience and then refuses to say
which rectangle the user chose. That single missing fact blocks everything
interesting: re-capturing the same region after a scroll, highlighting the
block being read, and saving a region to re-run later. So Slicer owns the
picker.

Coordinate systems are the whole difficulty here. AppKit places windows in a
global space whose origin is the bottom-left of the main display, with y
increasing upward. `screencapture -R` wants the top-left of the main display
with y increasing downward. Everything returned by this module is already in
screencapture's space, so no caller has to think about it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSBezierPath, NSColor, NSCursor, NSEvent, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSMakePoint, NSMakeRect, NSScreen, NSString,
    NSGraphicsContext, NSView, NSWindow, NSWindowStyleMaskBorderless,
)
from Foundation import NSDate, NSRunLoop

NSStatusWindowLevel = 25
NSCompositingOperationClear = 0
ESCAPE = 53

# Smaller than this and it was a stray click, not a selection.
MIN_SELECTION = 8


@dataclass(frozen=True)
class Region:
    """A rectangle in screencapture coordinates: top-left origin, points."""

    x: int
    y: int
    w: int
    h: int

    def as_argument(self) -> str:
        return f"{self.x},{self.y},{self.w},{self.h}"


class _State:
    def __init__(self) -> None:
        self.origin: tuple[float, float] | None = None
        self.current: tuple[float, float] | None = None
        self.done = False
        self.cancelled = False
        self.screen_frame = None


_state = _State()


class SelectionView(NSView):
    """Dims its screen and draws the rectangle being dragged."""

    def acceptsFirstResponder(self) -> bool:
        return True

    def mouseDown_(self, event) -> None:
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        _state.origin = (point.x, point.y)
        _state.current = (point.x, point.y)
        _state.screen_frame = self.window().frame()
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event) -> None:
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        _state.current = (point.x, point.y)
        self.setNeedsDisplay_(True)

    def mouseUp_(self, event) -> None:
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        _state.current = (point.x, point.y)
        _state.done = True

    def keyDown_(self, event) -> None:
        if event.keyCode() == ESCAPE:
            _state.cancelled = True
            _state.done = True

    def drawRect_(self, rect) -> None:
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.32).setFill()
        NSBezierPath.fillRect_(self.bounds())

        selection = _local_rect()
        if selection is None or not _same_screen(self.window().frame()):
            return

        # Punch the selection back to full brightness, so the content stays
        # legible while it is being chosen.
        context = NSGraphicsContext.currentContext()
        context.saveGraphicsState()
        context.setCompositingOperation_(NSCompositingOperationClear)
        NSBezierPath.fillRect_(selection)
        context.restoreGraphicsState()

        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.27, 0.75, 0.78, 1.0).setStroke()
        border = NSBezierPath.bezierPathWithRect_(selection)
        border.setLineWidth_(1.5)
        border.stroke()

        label = f"{int(selection.size.width)} x {int(selection.size.height)}"
        attributes = {
            NSFontAttributeName: NSFont.monospacedSystemFontOfSize_weight_(12, 0.0),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        NSString.stringWithString_(label).drawAtPoint_withAttributes_(
            NSMakePoint(selection.origin.x + 6,
                        selection.origin.y + selection.size.height + 6),
            attributes,
        )


def _same_screen(frame) -> bool:
    return (_state.screen_frame is not None
            and abs(frame.origin.x - _state.screen_frame.origin.x) < 1
            and abs(frame.origin.y - _state.screen_frame.origin.y) < 1)


def _local_rect():
    if _state.origin is None or _state.current is None:
        return None
    (x0, y0), (x1, y1) = _state.origin, _state.current
    return NSMakeRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def begin(x: float, y: float, screen_frame) -> None:
    """Start a selection. Exposed so the state machine can be tested."""
    _state.__init__()
    _state.origin = (x, y)
    _state.current = (x, y)
    _state.screen_frame = screen_frame


def drag_to(x: float, y: float) -> None:
    _state.current = (x, y)


def finish() -> Region | None:
    """End a selection and return it in capture space, or None if too small."""
    rect = _local_rect()
    if rect is None or rect.size.width < MIN_SELECTION or rect.size.height < MIN_SELECTION:
        return None
    return to_capture_space(rect, _state.screen_frame)


def select_region(timeout: float = 120.0) -> Region | None:
    """Show the overlay and return the chosen region, or None if cancelled.

    Must be called on the main thread: it drives the AppKit run loop.
    """
    _state.__init__()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    windows = []
    for screen in NSScreen.screens():
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen.frame(), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        window.setLevel_(NSStatusWindowLevel)
        window.setOpaque_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setIgnoresMouseEvents_(False)
        # The overlay must never appear in our own captures.
        window.setSharingType_(0)
        view = SelectionView.alloc().initWithFrame_(
            NSMakeRect(0, 0, screen.frame().size.width, screen.frame().size.height)
        )
        window.setContentView_(view)
        window.makeKeyAndOrderFront_(None)
        windows.append((window, screen))

    app.activateIgnoringOtherApps_(True)
    NSCursor.crosshairCursor().push()

    loop = NSRunLoop.currentRunLoop()
    deadline = time.time() + timeout
    while not _state.done and time.time() < deadline:
        event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            0xFFFFFFFF, NSDate.dateWithTimeIntervalSinceNow_(0.02),
            "kCFRunLoopDefaultMode", True,
        )
        if event is not None:
            app.sendEvent_(event)
        loop.runMode_beforeDate_("kCFRunLoopDefaultMode",
                                 NSDate.dateWithTimeIntervalSinceNow_(0.005))

    NSCursor.pop()
    for window, _ in windows:
        window.orderOut_(None)
    # Let the compositor drop the overlay before anyone captures the screen.
    for _ in range(8):
        loop.runMode_beforeDate_("kCFRunLoopDefaultMode",
                                 NSDate.dateWithTimeIntervalSinceNow_(0.02))

    if _state.cancelled or _state.origin is None:
        return None

    rect = _local_rect()
    if rect is None or rect.size.width < MIN_SELECTION or rect.size.height < MIN_SELECTION:
        return None

    screen_frame = _state.screen_frame
    for window, screen in windows:
        if _same_screen(window.frame()):
            screen_frame = screen.frame()
            break
    return to_capture_space(rect, screen_frame)


def to_capture_space(rect, screen_frame) -> Region:
    """AppKit's bottom-left global space to screencapture's top-left space."""
    main_top = max(s.frame().origin.y + s.frame().size.height for s in NSScreen.screens())
    global_x = screen_frame.origin.x + rect.origin.x
    global_y = screen_frame.origin.y + rect.origin.y
    top_left_y = main_top - (global_y + rect.size.height)
    return Region(int(round(global_x)), int(round(top_left_y)),
                  int(round(rect.size.width)), int(round(rect.size.height)))
