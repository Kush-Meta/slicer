"""Does macOS actually keep a content-protected window out of our captures?

An app marks a window as never-capture by setting NSWindow.sharingType to
NSWindowSharingNone. Password managers and banking apps rely on it. macOS 15
changed WindowServer so all visible windows composite into a single framebuffer
that ScreenCaptureKit reads directly, and there are open reports that the flag
is no longer honoured under that architecture.

If it is not honoured, Slicer cannot delegate sensitive-window exclusion to the
operating system, and must maintain its own denylist as the primary defence.

This is a decision, not a detail, so it is a script that can be re-run on every
major OS release rather than an assumption written down once.

Run:  ./.venv/bin/python scripts/verify_content_protection.py
"""

from __future__ import annotations

import os
import platform
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSColor, NSFont, NSMakeRect, NSScreen, NSTextField, NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSDate, NSRunLoop

NSWindowSharingNone = 0
NSStatusWindowLevel = 25

# A string no ordinary screen would contain, so finding it is unambiguous.
MARKER = "PROTECTEDMARKERZQX"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def _make_window(x: int, y: int, w: int, h: int, protected: bool) -> NSWindow:
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, w, h), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    if protected:
        window.setSharingType_(NSWindowSharingNone)
    window.setLevel_(NSStatusWindowLevel)
    window.setBackgroundColor_(NSColor.whiteColor())
    window.setOpaque_(True)

    label = NSTextField.labelWithString_(MARKER)
    label.setFont_(NSFont.boldSystemFontOfSize_(44))
    label.setTextColor_(NSColor.blackColor())
    label.setFrame_(NSMakeRect(20, h / 2 - 34, w - 40, 68))
    window.contentView().addSubview_(label)
    window.makeKeyAndOrderFront_(None)
    return window


def _pump(seconds: float) -> None:
    loop = NSRunLoop.currentRunLoop()
    deadline = time.time() + seconds
    while time.time() < deadline:
        loop.runMode_beforeDate_("kCFRunLoopDefaultMode",
                                 NSDate.dateWithTimeIntervalSinceNow_(0.02))


def _marker_visible_in_capture(x: int, y: int, w: int, h: int) -> tuple[bool, str]:
    from slicer.capture import capture_region
    from slicer.ocr import recognize

    capture = capture_region(x, y, w, h, stability_check=False)
    try:
        result = recognize(capture.path)
        text = " ".join(line.text for line in result.lines)
        # OCR of large bold text is reliable, but accept a partial match so a
        # single misread character cannot produce a false "protected" result.
        found = MARKER in text.replace(" ", "") or MARKER[:12] in text.replace(" ", "")
        return found, text[:120]
    finally:
        try:
            os.unlink(capture.path)
        except OSError:
            pass


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    screen = NSScreen.mainScreen().frame()
    w, h = 720, 140
    # Screen coordinates for screencapture -R are measured from the top-left;
    # NSWindow places from the bottom-left. Put it well inside both.
    x = int(screen.size.width / 2 - w / 2)
    top_y = 220
    window_y = int(screen.size.height - top_y - h)

    print(f"\n{BOLD}window content protection{RESET}  "
          f"{DIM}macOS {platform.mac_ver()[0]} ({platform.machine()}){RESET}\n")

    # Control: an ordinary window must be captured, or the test proves nothing.
    control = _make_window(x, window_y, w, h, protected=False)
    _pump(1.2)
    control_seen, control_text = _marker_visible_in_capture(x, top_y, w, h)
    control.orderOut_(None)
    _pump(0.4)

    if not control_seen:
        print(f"  [{YELLOW} inconclusive {RESET}] the control window was not captured either.")
        print(f"       {DIM}Capture may be blocked entirely. Saw: {control_text!r}{RESET}\n")
        return 2
    print(f"  [{GREEN}   control   {RESET}] an ordinary window IS captured, as expected")

    # The real question.
    protected = _make_window(x, window_y, w, h, protected=True)
    _pump(1.2)
    leaked, leak_text = _marker_visible_in_capture(x, top_y, w, h)
    protected.orderOut_(None)
    _pump(0.2)

    print()
    if leaked:
        print(f"  [{RED}   LEAKED    {RESET}] a window marked NSWindowSharingNone "
              f"{BOLD}appeared in our capture{RESET}")
        print(f"       {DIM}read back: {leak_text!r}{RESET}")
        print(f"\n  {BOLD}Consequence:{RESET} the OS flag cannot be relied on. Slicer must")
        print("  maintain its own application denylist as the primary defence, and")
        print("  refuse to capture those apps itself.\n")
        return 1

    print(f"  [{GREEN}  PROTECTED  {RESET}] the protected window was excluded from the capture")
    print(f"       {DIM}read back: {leak_text!r}{RESET}")
    print(f"\n  {BOLD}Consequence:{RESET} the OS flag is honoured on this version. Slicer's own")
    print("  denylist remains a defence in depth, not the only line. Re-run this")
    print("  on every major macOS release.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
