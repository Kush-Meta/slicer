"""Screen capture, with the checks that stop silent failures.

Two failure modes from the pre-mortem are handled here rather than downstream,
because by the time recognition sees the image the evidence is gone:

  * Revoked Screen Recording permission returns frames that are uniformly
    black. The API succeeds. Only a pixel-variance check catches it.
  * Content captured mid-animation or before lazy content renders yields
    half-drawn text. Two captures a moment apart disagree; one capture cannot
    tell you anything.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

SCREENCAPTURE = "/usr/sbin/screencapture"

# Below this luminance spread across sampled pixels, the frame carries no
# image. In practice a real screen region is far above it and a permission
# failure is at or near zero.
UNIFORM_FRAME_VARIANCE = 4.0


class CaptureError(RuntimeError):
    """Raised when a capture did not produce usable pixels."""

    def __init__(self, message: str, *, remedy: str = ""):
        super().__init__(message)
        self.remedy = remedy


@dataclass
class Capture:
    path: str
    width: int          # pixels
    height: int         # pixels
    origin_x: int = 0   # screen points; 0 when unknown (interactive capture)
    origin_y: int = 0
    scale: float = 1.0
    origin_known: bool = True
    stable: bool = True


def capture_region(x: int, y: int, w: int, h: int, *, stability_check: bool = True) -> Capture:
    """Capture a screen rectangle given in points."""
    if w <= 0 or h <= 0:
        raise CaptureError(f"region has no area: {w}x{h}")

    path = _run_screencapture(["-R", f"{x},{y},{w},{h}"])
    width, height = _image_size(path)
    _assert_has_content(path)

    scale = round(width / w, 3) if w else 1.0
    stable = True
    if stability_check:
        stable = _is_stable(["-R", f"{x},{y},{w},{h}"], path)

    return Capture(
        path=path, width=width, height=height,
        origin_x=x, origin_y=y, scale=scale, origin_known=True, stable=stable,
    )


def capture_interactive() -> Capture:
    """Let the user drag a region or click a window.

    screencapture does not report which rectangle was chosen, so the screen
    origin is unknown. That only matters for mapping blocks back onto the
    display for highlighting, which v0 does not do.
    """
    path = _run_screencapture(["-i", "-o"], allow_cancel=True)
    width, height = _image_size(path)
    _assert_has_content(path)
    return Capture(
        path=path, width=width, height=height,
        scale=_main_display_scale(), origin_known=False, stable=True,
    )


def capture_file(path: str) -> Capture:
    """Read an existing image. Used by tests and by the golden set."""
    if not os.path.exists(path):
        raise CaptureError(f"no such file: {path}")
    width, height = _image_size(path)
    return Capture(path=path, width=width, height=height, origin_known=False)


# --------------------------------------------------------------------------


def _run_screencapture(args: list[str], *, allow_cancel: bool = False) -> str:
    fd, path = tempfile.mkstemp(prefix="slicer-", suffix=".png")
    os.close(fd)
    proc = subprocess.run(
        [SCREENCAPTURE, "-x", *args, path],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        _cleanup(path)
        raise CaptureError(
            f"screencapture failed: {proc.stderr.strip() or proc.returncode}",
            remedy=_PERMISSION_REMEDY,
        )
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _cleanup(path)
        if allow_cancel:
            raise CaptureError("selection cancelled")
        raise CaptureError("screencapture wrote no image", remedy=_PERMISSION_REMEDY)
    return path


def _image_size(path: str) -> tuple[int, int]:
    import Quartz  # noqa: PLC0415
    from Foundation import NSURL  # noqa: PLC0415

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if source is None:
        raise CaptureError(f"could not open capture: {path}")
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise CaptureError(f"could not decode capture: {path}")
    return Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)


def _assert_has_content(path: str) -> None:
    """A uniform frame means the pixels never arrived, not that the screen is blank."""
    variance = _luminance_variance(path)
    if variance < UNIFORM_FRAME_VARIANCE:
        raise CaptureError(
            f"capture is a uniform frame (luminance variance {variance:.2f})",
            remedy=_PERMISSION_REMEDY,
        )


def _luminance_variance(path: str, samples: int = 40) -> float:
    """Sample a grid of pixels and return the standard deviation of luminance."""
    import Quartz  # noqa: PLC0415
    from Foundation import NSURL  # noqa: PLC0415

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    bytes_per_row = Quartz.CGImageGetBytesPerRow(image)
    bits_per_pixel = Quartz.CGImageGetBitsPerPixel(image)
    if bits_per_pixel % 8:
        return 255.0  # unusual format; do not claim it is blank
    bytes_per_pixel = bits_per_pixel // 8

    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    if data is None:
        return 255.0
    buf = bytes(data)

    values: list[float] = []
    step_x = max(1, width // samples)
    step_y = max(1, height // samples)
    for py in range(0, height, step_y):
        for px in range(0, width, step_x):
            offset = py * bytes_per_row + px * bytes_per_pixel
            if offset + 3 > len(buf):
                continue
            r, g, b = buf[offset], buf[offset + 1], buf[offset + 2]
            values.append(0.2126 * r + 0.7152 * g + 0.0722 * b)

    if len(values) < 2:
        return 255.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _is_stable(args: list[str], first_path: str, delay: float = 0.12) -> bool:
    """Capture again after a beat. Disagreement means the content is still moving."""
    time.sleep(delay)
    try:
        second = _run_screencapture(args)
    except CaptureError:
        return True  # cannot prove instability; do not block the reading
    try:
        return _digest(first_path) == _digest(second)
    finally:
        _cleanup(second)


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _main_display_scale() -> float:
    try:
        import Quartz  # noqa: PLC0415

        display = Quartz.CGMainDisplayID()
        mode = Quartz.CGDisplayCopyDisplayMode(display)
        pixels = Quartz.CGDisplayModeGetPixelWidth(mode)
        points = Quartz.CGDisplayModeGetWidth(mode)
        return round(pixels / points, 3) if points else 1.0
    except Exception:
        return 1.0


def _cleanup(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


_PERMISSION_REMEDY = (
    "Grant Screen Recording to the app running Slicer (your terminal) in\n"
    "  System Settings > Privacy & Security > Screen Recording,\n"
    "then fully quit and reopen it - macOS does not apply the grant to a running process."
)
