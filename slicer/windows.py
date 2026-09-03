"""Finding something to read without being able to see the screen.

Dragging a box is the wrong primary interaction for a screen reader. It assumes
sight twice over: to know where the content is, and to confirm the selection
landed on it. So the aiming model changes - you name a *target* rather than a
rectangle, and the commonest target is simply "the window I am in".

Window bounds come back from CGWindowList in the same coordinate space that
screencapture takes, so a window becomes a region and everything downstream -
re-capture after a scroll, the highlight, continuity - keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

import Quartz
from AppKit import NSWorkspace

# Windows smaller than this are palettes, tooltips and shadows, not content.
MIN_WIDTH = 120
MIN_HEIGHT = 80

# Our own overlays must never be a reading target.
OWN_NAMES = {"Slicer", "Python"}


@dataclass(frozen=True)
class WindowRef:
    number: int
    pid: int
    app: str
    title: str
    x: int
    y: int
    w: int
    h: int

    @property
    def region(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    @property
    def label(self) -> str:
        """What Slicer says out loud when it starts reading this."""
        if self.title and self.title != self.app:
            return f"{self.app}, {self.title}"
        return self.app


def list_windows() -> list[WindowRef]:
    """On-screen content windows, front to back."""
    options = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
    found: list[WindowRef] = []
    for entry in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        # Layer 0 is ordinary application content. Higher layers are menus,
        # panels and overlays - including ours.
        if entry.get("kCGWindowLayer", 0) != 0:
            continue
        if entry.get("kCGWindowAlpha", 1) <= 0:
            continue
        bounds = entry.get("kCGWindowBounds") or {}
        width, height = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            continue
        app = str(entry.get("kCGWindowOwnerName", "") or "")
        if app in OWN_NAMES:
            continue
        found.append(WindowRef(
            number=int(entry.get("kCGWindowNumber", 0)),
            pid=int(entry.get("kCGWindowOwnerPID", 0)),
            app=app,
            title=str(entry.get("kCGWindowName", "") or ""),
            x=int(bounds.get("X", 0)), y=int(bounds.get("Y", 0)),
            w=width, h=height,
        ))
    return found


def frontmost_window() -> WindowRef | None:
    """The window the user is actually working in.

    Prefers a window belonging to the frontmost application, because
    CGWindowList's own ordering can put a background app's window first when
    the active app has none on this display.
    """
    windows = list_windows()
    if not windows:
        return None
    active = NSWorkspace.sharedWorkspace().frontmostApplication()
    if active is not None:
        pid = int(active.processIdentifier())
        for window in windows:
            if window.pid == pid:
                return window
    return windows[0]


def window_under(x: int, y: int) -> WindowRef | None:
    """Topmost window containing a point, for pointer-driven reading."""
    for window in list_windows():
        if window.x <= x < window.x + window.w and window.y <= y < window.y + window.h:
            return window
    return None


def voiceover_running() -> bool:
    """Whether VoiceOver is speaking, so Slicer does not talk over it."""
    try:
        return bool(NSWorkspace.sharedWorkspace().isVoiceOverEnabled())
    except Exception:                     # noqa: BLE001
        return False
