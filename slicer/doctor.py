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
    """How long until speech actually starts.

    Only the in-process backend can answer this directly: it exposes
    `isSpeaking`, so the moment audio begins is observable. `say` offers no
    such signal, and inferring its startup by fitting a line through several
    utterance lengths turned out to be unreliable - the same method produced
    145ms and 580ms depending only on which words were used. So `say` is
    reported as what can actually be measured about it: the whole round trip
    for one short word, startup and pronunciation together.
    """
    from .speech import AVSpeechBackend, SayBackend  # noqa: PLC0415

    in_process: float | None = None
    try:
        backend = AVSpeechBackend()
        in_process = min(time_to_first_audio("one", backend=backend) for _ in range(3))
        _line(OK, "speech (in-process)",
              f"{in_process:.0f}ms to first audio, measured  "
              f"{DIM}pauses mid-sentence{RESET}")
    except Exception as exc:                          # noqa: BLE001
        _line(WARN, "speech (in-process)", str(exc)[:60])

    try:
        say = SayBackend()
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            say.speak("one", voice=None, rate=None, still_current=lambda: True)
            timings.append((time.perf_counter() - start) * 1000)
        _line(OK, "speech (say, fallback)",
              f"{min(timings):.0f}ms round trip for one word  "
              f"{DIM}start not separable; cannot pause{RESET}")
        if in_process is None:
            in_process = min(timings)
    except Exception as exc:                          # noqa: BLE001
        _line(WARN, "speech (say, fallback)", str(exc)[:60])

    return in_process


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
