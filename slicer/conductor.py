"""The state machine that runs a reading.

Deliberately not a model. This is a soft real-time audio loop: a language model
in the control path adds unpredictable latency to every transition and makes
failures irreproducible. Judgement belongs in the parse and planning stages;
sequencing belongs here, where it can be single-stepped.

The degradation ladder is implemented in `read`: every path either speaks, or
says why it cannot. Failing silently is the one outcome that is not allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import capture as capture_mod
from . import telemetry
from .blocks import Slice
from .capture import Capture, CaptureError
from .editor import UngroundedSpeech, Utterance, to_speech
from .layout import LayoutConfig, build_slice
from .narrator import Narrator
from .ocr import OcrError, recognize

# Blocks below this are read with a spoken hedge rather than as fact.
LOW_CONFIDENCE = 0.45
# Below this a block is not read at all; it is counted and reported.
MIN_CONFIDENCE = 0.30


@dataclass
class Reading:
    slice: Slice
    utterances: list[Utterance]
    timings: telemetry.Timings
    notes: list[str] = field(default_factory=list)
    dropped: int = 0

    @property
    def word_count(self) -> int:
        return sum(len(u.text.split()) for u in self.utterances)


class Conductor:
    def __init__(self, narrator: Narrator | None = None,
                 layout_config: LayoutConfig | None = None,
                 *, fast_ocr: bool = False):
        self.narrator = narrator or Narrator()
        self.layout_config = layout_config or LayoutConfig()
        self.fast_ocr = fast_ocr

    # -- pipeline ----------------------------------------------------------

    def prepare(self, source: Capture) -> Reading:
        """Capture to speakable utterances. Raises only on L4 conditions."""
        timings = telemetry.Timings()
        notes: list[str] = []

        with timings.stage("ocr"):
            result = recognize(source.path, fast=self.fast_ocr)
        if not result.lines:
            raise CaptureError(
                "no text found in the captured region",
                remedy="Check the region contains text, or try a larger selection.",
            )

        with timings.stage("layout"):
            slice_ = build_slice(
                result, self.layout_config,
                origin_x=source.origin_x, origin_y=source.origin_y, scale=source.scale,
            )
        notes.extend(slice_.degraded)
        if not source.stable:
            notes.append("content was still changing when captured")

        with timings.stage("edit"):
            utterances, dropped = self._render(slice_)

        if not utterances:
            raise CaptureError(
                f"nothing readable: {dropped} blocks were below the confidence floor",
                remedy="The text may be too small or low contrast. Try zooming in.",
            )
        if dropped:
            notes.append(f"{dropped} block(s) skipped below the confidence floor")
        skipped = len(slice_.skipped())
        if skipped:
            notes.append(f"{skipped} block(s) classified as navigation")

        return Reading(slice=slice_, utterances=utterances, timings=timings,
                       notes=notes, dropped=dropped)

    def _render(self, slice_: Slice) -> tuple[list[Utterance], int]:
        utterances: list[Utterance] = []
        dropped = 0
        for block in slice_.readable():
            if block.confidence < MIN_CONFIDENCE:
                dropped += 1
                continue
            try:
                utterance = to_speech(block, min_confidence=LOW_CONFIDENCE)
            except UngroundedSpeech as exc:
                # The invariant held and something tried to speak an invented
                # word. Drop the block rather than the guarantee.
                telemetry.record({"event": "ungrounded", "detail": str(exc)})
                dropped += 1
                continue
            if utterance is not None:
                utterances.append(utterance)
        return utterances, dropped

    # -- speaking ----------------------------------------------------------

    def read(self, source: Capture, *, on_progress=None) -> Reading:
        reading = self.prepare(source)
        epoch = self.narrator.new_epoch()
        with reading.timings.stage("speak"):
            for progress in self.narrator.read(reading.utterances, epoch):
                if on_progress:
                    on_progress(progress)
        telemetry.record({
            "event": "reading",
            "blocks": len(reading.utterances),
            "words": reading.word_count,
            "stages": reading.timings.stages,
            "notes": reading.notes,
        })
        return reading


def capture_for(region: str | None, file: str | None) -> Capture:
    """Resolve the three ways a reading can be aimed."""
    if file:
        return capture_mod.capture_file(file)
    if region:
        try:
            x, y, w, h = (int(part) for part in region.split(","))
        except ValueError:
            raise CaptureError(f"region must be x,y,w,h - got {region!r}") from None
        return capture_mod.capture_region(x, y, w, h)
    return capture_mod.capture_interactive()
