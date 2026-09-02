"""Environment and latency checks.

These are the experiments from the pre-mortem, run against this machine rather
than assumed. Anything that would silently produce a broken reading later
should fail loudly here instead.
"""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import time

from .narrator import time_to_first_audio

OK, WARN, BAD = "\033[32m  ok  \033[0m", "\033[33m warn \033[0m", "\033[31m fail \033[0m"
DIM, RESET, BOLD = "\033[2m", "\033[0m", "\033[1m"

FIRST_WORD_BUDGET_MS = 900


def run() -> int:
    print(f"\n{BOLD}slicer doctor{RESET}\n")
    failures = 0

    version = platform.mac_ver()[0]
    major = int(version.split(".")[0]) if version else 0
    _line(OK, "macOS", f"{version} ({platform.machine()})")
    if major >= 15:
        _line(WARN, "window content protection",
              "macOS 15+ composites all windows into one framebuffer. Verify that\n"
              f"       {DIM}windows marked 'do not capture' really are excluded before relying on it.{RESET}")

    try:
        import Vision  # noqa: F401
        _line(OK, "Vision framework", "available")
    except ImportError:
        _line(BAD, "Vision framework", "missing - pip install pyobjc-framework-Vision")
        failures += 1

    # Capture, and prove the pixels are real rather than a permission failure.
    try:
        from .capture import capture_region
        start = time.perf_counter()
        cap = capture_region(0, 0, 400, 300, stability_check=False)
        elapsed = (time.perf_counter() - start) * 1000
        _line(OK, "screen capture", f"{cap.width}x{cap.height}px, scale {cap.scale}x  {DIM}{elapsed:.0f}ms{RESET}")
        os.unlink(cap.path)
    except Exception as exc:
        _line(BAD, "screen capture", str(exc).split("\n")[0])
        print(f"       {DIM}Grant Screen Recording to your terminal, then fully quit and reopen it.{RESET}")
        failures += 1

    ocr_ms = _check_ocr()
    if ocr_ms is None:
        failures += 1

    say_ms = _check_say()

    if ocr_ms is not None and say_ms is not None:
        budget = 200 + ocr_ms + say_ms          # capture + recognize + speech start
        status = OK if budget <= FIRST_WORD_BUDGET_MS else WARN
        _line(status, "first-word budget",
              f"~{budget:.0f}ms of {FIRST_WORD_BUDGET_MS}ms "
              f"{DIM}(capture ~200 + ocr {ocr_ms:.0f} + speech start {say_ms:.0f}){RESET}")

    print()
    if failures:
        print(f"{BOLD}{failures} check(s) failed.{RESET}\n")
    else:
        print(f"{DIM}All checks passed.{RESET}\n")
    return 1 if failures else 0


def _check_ocr() -> float | None:
    try:
        from .ocr import recognize
        path = _sample_image()
        recognize(path)                      # warm the framework
        start = time.perf_counter()
        result = recognize(path)
        elapsed = (time.perf_counter() - start) * 1000
        os.unlink(path)
        if not result.lines:
            _line(BAD, "recognition", "found no text in a known-good image")
            return None
        _line(OK, "recognition",
              f"{len(result.lines)} lines, confidence {result.mean_confidence:.2f}  "
              f"{DIM}{elapsed:.0f}ms warm{RESET}")
        return elapsed
    except Exception as exc:
        _line(BAD, "recognition", str(exc)[:70])
        return None


def _check_say() -> float | None:
    """Separate fixed startup cost from speech duration by least squares.

    A single short utterance cannot tell them apart - most of the wall clock is
    the word itself. Four lengths and a line fit give an intercept that is the
    real cost of beginning to speak.
    """
    try:
        points: list[tuple[int, float]] = []
        for words in (1, 4, 8, 14):
            phrase = " ".join(["one"] * words)
            points.append((words, min(time_to_first_audio(phrase) for _ in range(2))))

        n = len(points)
        sx = sum(w for w, _ in points)
        sy = sum(ms for _, ms in points)
        sxy = sum(w * ms for w, ms in points)
        sxx = sum(w * w for w, _ in points)
        denominator = n * sxx - sx * sx
        if denominator == 0:
            return None
        per_word = (n * sxy - sx * sy) / denominator
        startup = max((sy - per_word * sx) / n, 0.0)
        _line(OK, "speech", f"startup {startup:.0f}ms, {per_word:.0f}ms per word "
                            f"{DIM}(fitted over {n} lengths){RESET}")
        return startup
    except Exception as exc:
        _line(BAD, "speech", str(exc)[:70])
        return None


def _sample_image() -> str:
    from AppKit import (
        NSBitmapImageRep, NSColor, NSDeviceRGBColorSpace, NSFont, NSFontAttributeName,
        NSForegroundColorAttributeName, NSGraphicsContext, NSMakePoint, NSMakeRect,
        NSRectFill, NSString,
    )
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 800, 200, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    NSColor.whiteColor().setFill()
    NSRectFill(NSMakeRect(0, 0, 800, 200))
    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(28),
             NSForegroundColorAttributeName: NSColor.blackColor()}
    NSString.stringWithString_("Reading order is the hard problem.").drawAtPoint_withAttributes_(
        NSMakePoint(40, 100), attrs)
    NSGraphicsContext.restoreGraphicsState()
    fd, path = tempfile.mkstemp(prefix="slicer-doctor-", suffix=".png")
    os.close(fd)
    rep.representationUsingType_properties_(4, {}).writeToFile_atomically_(path, True)
    return path


def _line(status: str, name: str, detail: str) -> None:
    print(f"  [{status}] {name:<24} {detail}")
