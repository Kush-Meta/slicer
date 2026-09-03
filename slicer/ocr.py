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
    source,
    *,
    fast: bool = False,
    languages: tuple[str, ...] = ("en-US",),
    top_fraction: float | None = None,
) -> OcrResult:
    """Recognize text in an image file, returning lines with pixel boxes.

    Boxes use an origin at the top-left of the image, so they compose directly
    with capture rectangles. Vision reports normalized boxes measured up from
    the bottom; the flip happens here so no caller has to know that.

    `source` is either a path or a CGImage. Live captures pass the image, so
    nothing is encoded or parsed on the way to recognition; files are still
    accepted for fixtures and for replaying a saved capture.

    `top_fraction` restricts recognition to that share of the image measured
    from the top, using Vision's region of interest. Cost scales with the area
    examined - on a full-screen window the top quarter recognizes in 183ms
    against 463ms for the whole thing - which is what makes it possible to
    start speaking before the whole slice has been read.

    Note that `fast` is a false economy for this: it is eight times quicker and
    measurably wrong, mangling "prose" into "PTose" and "rotate(cert)" into
    "rotatel cert I". Restricting the *area* buys speed without touching
    accuracy; dropping the recognition level does not.
    """
    Vision, Quartz = _load_vision()

    if isinstance(source, str):
        from Foundation import NSURL  # noqa: PLC0415

        handle = Quartz.CGImageSourceCreateWithURL(
            NSURL.fileURLWithPath_(source), None)
        if handle is None:
            raise OcrError(f"could not open image: {source}")
        image = Quartz.CGImageSourceCreateImageAtIndex(handle, 0, None)
        if image is None:
            raise OcrError(f"could not decode image: {source}")
    else:
        # A CGImage straight from the capture, never written to disk. Encoding
        # a full-screen PNG costs ~32ms, which is most of a capture's budget
        # once the capture itself is 9ms.
        image = source

    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(RECOGNITION_FAST if fast else RECOGNITION_ACCURATE)
    request.setUsesLanguageCorrection_(not fast)
    request.setRecognitionLanguages_(list(languages))
    if top_fraction is not None:
        # Vision measures its region of interest from the bottom-left, so the
        # top of the image is the far end of the unit square.
        share = max(0.05, min(1.0, top_fraction))
        request.setRegionOfInterest_(
            Quartz.CGRectMake(0.0, 1.0 - share, 1.0, share))

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
