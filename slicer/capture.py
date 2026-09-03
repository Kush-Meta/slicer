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

# Capturing through the screencapture binary costs ~90ms, nearly all of it
# process spawn. CoreGraphics does the same job in ~9ms in this process.
# CGWindowListCreateImage is marked obsoleted in the macOS 15 SDK - the
# replacement, ScreenCaptureKit, is asynchronous and a much larger change - but
# the C entry point is still present and working. It is used when it works and
# falls back to the binary when it does not, which is checked once and cached.
_in_process_capture: bool | None = None

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
    path: str = ""
    width: int = 0      # pixels
    height: int = 0     # pixels
    # A live capture keeps its pixels in memory: encoding a full-screen PNG
    # costs more than taking the picture. `path` stays for fixtures, for
    # replaying a saved capture, and for anything that wants a file.
    image: object = None
    origin_x: int = 0   # screen points
    origin_y: int = 0
    scale: float = 1.0
    origin_known: bool = True
    stable: bool = True
    # The rectangle this came from, in screencapture space. Present whenever
    # the origin is known, so the same region can be captured again after a
    # scroll and blocks can be mapped back onto the display.
    region: tuple[int, int, int, int] | None = None
    # What Slicer says it is about to read - "Safari, Quarterly Review". For a
    # non-visual user this is the only confirmation that it aimed correctly.
    label: str = ""

    @property
    def source(self):
        """What to hand recognition: the pixels if we have them, else the file."""
        return self.image if self.image is not None else self.path

    def release(self) -> None:
        """Drop a temporary file, if this capture made one."""
        if self.path and self.image is None:
            return                        # a caller-owned file; leave it alone
        if self.path:
            _cleanup(self.path)
            self.path = ""

    def recapture(self) -> "Capture":
        """Capture the same rectangle again."""
        if self.region is None:
            raise CaptureError("this capture did not record where it came from")
        return capture_region(*self.region)


def capture_region(x: int, y: int, w: int, h: int, *, stability_check: bool = True) -> Capture:
    """Capture a screen rectangle given in points."""
    if w <= 0 or h <= 0:
        raise CaptureError(f"region has no area: {w}x{h}")

    image, path = _capture_pixels(x, y, w, h)
    width, height = (_image_dimensions(image) if image is not None
                     else _image_size(path))
    _assert_has_content(image if image is not None else path)

    scale = round(width / w, 3) if w else 1.0
    stable = True
    if stability_check and path:
        stable = _is_stable(["-R", f"{x},{y},{w},{h}"], path)

    return Capture(
        path=path, image=image, width=width, height=height,
        origin_x=x, origin_y=y, scale=scale, origin_known=True, stable=stable,
        region=(x, y, w, h),
    )


def capture_interactive() -> Capture:
    """Let the user drag a region, and remember exactly which one.

    Uses Slicer's own picker rather than `screencapture -i`, because the system
    picker will not say which rectangle was chosen - and without that there is
    no re-capture after a scroll and no highlighting.
    """
    from .picker import select_region  # noqa: PLC0415

    region = select_region()
    if region is None:
        raise CaptureError("selection cancelled")
    return capture_region(region.x, region.y, region.w, region.h)


def capture_window(window=None, *, stability_check: bool = False) -> Capture:
    """Capture a whole window, no selection required.

    This is the primary aiming model for non-visual use: you say "read this
    window" rather than drawing a rectangle around it.
    """
    from .windows import frontmost_window  # noqa: PLC0415

    window = window or frontmost_window()
    if window is None:
        raise CaptureError(
            "no window found to read",
            remedy="Bring an application window to the front and try again.",
        )
    capture = capture_region(*window.region, stability_check=stability_check)
    capture.label = window.label
    return capture


def capture_display(index: int = 0, *, stability_check: bool = False) -> Capture:
    """Capture a whole display, for when the content spans windows."""
    from AppKit import NSScreen  # noqa: PLC0415

    screens = NSScreen.screens()
    if not screens:
        raise CaptureError("no display found")
    screen = screens[min(index, len(screens) - 1)]
    frame = screen.frame()
    top = max(s.frame().origin.y + s.frame().size.height for s in screens)
    x = int(frame.origin.x)
    y = int(top - (frame.origin.y + frame.size.height))
    capture = capture_region(x, y, int(frame.size.width), int(frame.size.height),
                             stability_check=stability_check)
    capture.label = "the screen"
    return capture


def capture_file(path: str) -> Capture:
    """Read an existing image. Used by tests and by the golden set."""
    if not os.path.exists(path):
        raise CaptureError(f"no such file: {path}")
    width, height = _image_size(path)
    return Capture(path=path, width=width, height=height, origin_known=False)


# --------------------------------------------------------------------------


def _capture_pixels(x: int, y: int, w: int, h: int):
    """Fastest available capture. Returns (image, path) - one of them is set."""
    global _in_process_capture
    if _in_process_capture is not False:
        try:
            import Quartz  # noqa: PLC0415

            image = Quartz.CGWindowListCreateImage(
                Quartz.CGRectMake(x, y, w, h),
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID,
                Quartz.kCGWindowImageDefault,
            )
            if image is not None and Quartz.CGImageGetWidth(image) > 0:
                _in_process_capture = True
                return image, ""
        except Exception:                 # noqa: BLE001
            pass
        _in_process_capture = False
    return None, _run_screencapture(["-R", f"{x},{y},{w},{h}"])


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


def _image_dimensions(image) -> tuple[int, int]:
    import Quartz  # noqa: PLC0415
    return Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)


def _assert_has_content(source) -> None:
    """A uniform frame means the pixels never arrived, not that the screen is blank."""
    variance = _luminance_variance(source)
    if variance < UNIFORM_FRAME_VARIANCE:
        raise CaptureError(
            f"capture is a uniform frame (luminance variance {variance:.2f})",
            remedy=_PERMISSION_REMEDY,
        )


def _luminance_variance(source, samples: int = 40) -> float:
    """Sample a grid of pixels and return the standard deviation of luminance."""
    import Quartz  # noqa: PLC0415

    if isinstance(source, str):
        from Foundation import NSURL  # noqa: PLC0415

        handle = Quartz.CGImageSourceCreateWithURL(
            NSURL.fileURLWithPath_(source), None)
        image = Quartz.CGImageSourceCreateImageAtIndex(handle, 0, None)
    else:
        image = source
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
