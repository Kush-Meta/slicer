"""Apple Vision text recognition.

This is the fast lane and, for now, the only lane. It is also the sole origin
of speakable text - see blocks.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .blocks import Box, TextLine

# Vision is expensive to import (~200ms) and not obviously thread-safe to
# initialize twice, so it is loaded once, lazily, behind a lock. A long-running
# Slicer process pays this cost only on the first reading.
_vision_lock = threading.Lock()
_vision = None
_quartz = None


def _load_vision():
    global _vision, _quartz
    with _vision_lock:
        if _vision is None:
            import Quartz  # noqa: PLC0415
            import Vision  # noqa: PLC0415

            _vision, _quartz = Vision, Quartz
    return _vision, _quartz


RECOGNITION_ACCURATE = 0
RECOGNITION_FAST = 1


@dataclass
class OcrResult:
    lines: list[TextLine]
    width: int
    height: int

    @property
    def mean_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


def recognize(
    image_path: str,
    *,
    fast: bool = False,
    languages: tuple[str, ...] = ("en-US",),
) -> OcrResult:
    """Recognize text in an image file, returning lines with pixel boxes.

    Boxes use an origin at the top-left of the image, so they compose directly
    with capture rectangles. Vision reports normalized boxes measured up from
    the bottom; the flip happens here so no caller has to know that.
    """
    Vision, Quartz = _load_vision()
    from Foundation import NSURL  # noqa: PLC0415

    url = NSURL.fileURLWithPath_(image_path)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        raise OcrError(f"could not open image: {image_path}")
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise OcrError(f"could not decode image: {image_path}")

    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(RECOGNITION_FAST if fast else RECOGNITION_ACCURATE)
    request.setUsesLanguageCorrection_(not fast)
    request.setRecognitionLanguages_(list(languages))

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise OcrError(f"recognition failed: {error}")

    lines: list[TextLine] = []
    for index, observation in enumerate(request.results() or []):
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string()).strip()
        if not text:
            continue

        rect = observation.boundingBox()
        px = rect.origin.x * width
        pw = rect.size.width * width
        ph = rect.size.height * height
        # Vision measures up from the bottom; screens measure down from the top.
        py = (1.0 - rect.origin.y - rect.size.height) * height

        lines.append(
            TextLine(
                id=f"l{index}",
                text=text,
                confidence=float(candidate.confidence()),
                box=Box(round(px), round(py), round(pw), round(ph)),
            )
        )

    # Vision returns observations in its own order, which is not reading order.
    # Sort top-to-bottom here only so downstream code has a stable starting
    # point; real ordering happens in order.py.
    lines.sort(key=lambda line: (line.box.y, line.box.x))
    return OcrResult(lines=lines, width=width, height=height)


class OcrError(RuntimeError):
    pass
