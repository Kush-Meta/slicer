"""The state machine that runs a reading.

Deliberately not a model. This is a soft real-time audio loop: a language model
in the control path adds unpredictable latency to every transition and makes
failures irreproducible. Judgement belongs in the parse and planning stages;
sequencing belongs here, where it can be single-stepped.

The degradation ladder is implemented in `read`: every path either speaks, or
says why it cannot. Failing silently is the one outcome that is not allowed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from . import capture as capture_mod
from . import telemetry
from .blocks import Block, Slice
from .capture import Capture, CaptureError
from .continuity import Scroller
from .fingerprint import ReadingMemory
from .editor import UngroundedSpeech, Utterance, Verbosity, to_speech
from .layout import LayoutConfig, build_slice
from .narrator import Narrator
from .ocr import OcrError, recognize

# The share of a capture read first, so speaking can begin before the whole
# slice has been recognized. Cost scales with area: on a full-screen window the
# top quarter recognizes in ~155ms against ~459ms for everything.
SPECULATIVE_TOP = 0.25
# Below this height the full parse is already quick enough that a second pass
# would burn work for nothing.
SPECULATIVE_MIN_HEIGHT = 600

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
    # What Slicer says it aimed at, e.g. "Safari, Quarterly Review". The only
    # confirmation a non-visual user gets that it targeted the right thing.
    label: str = ""

    @property
    def word_count(self) -> int:
        return sum(len(u.text.split()) for u in self.utterances)


class Conductor:
    def __init__(self, narrator: Narrator | None = None,
                 layout_config: LayoutConfig | None = None,
                 *, fast_ocr: bool = False,
                 verbosity: Verbosity = Verbosity.LOW):
        self.narrator = narrator or Narrator()
        self.layout_config = layout_config or LayoutConfig()
        self.fast_ocr = fast_ocr
        self.verbosity = verbosity

    # -- pipeline ----------------------------------------------------------

    def prepare(self, source: Capture, *, top_fraction: float | None = None) -> Reading:
        """Capture to speakable utterances. Raises only on L4 conditions."""
        timings = telemetry.Timings()
        notes: list[str] = []

        with timings.stage("ocr"):
            result = recognize(source.source, fast=self.fast_ocr,
                               top_fraction=top_fraction)
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
                       notes=notes, dropped=dropped,
                       label=getattr(source, "label", ""))

    def _render(self, slice_: Slice) -> tuple[list[Utterance], int]:
        utterances: list[Utterance] = []
        dropped = 0
        readable = slice_.readable()
        for position, block in enumerate(readable, 1):
            if block.confidence < MIN_CONFIDENCE:
                dropped += 1
                continue
            try:
                utterance = to_speech(block, min_confidence=LOW_CONFIDENCE,
                                      verbosity=self.verbosity,
                                      index=position, total=len(readable))
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

    def should_speculate(self, source: Capture) -> bool:
        return source.height >= SPECULATIVE_MIN_HEIGHT

    def read_responsive(self, source: Capture, *, on_progress=None) -> Reading:
        """Start speaking before the whole slice has been recognized.

        Recognition cost scales with the area examined, and a full-screen
        window costs ~459ms against ~155ms for its top quarter. Reading the top
        first gets a first word out in about a fifth of the time, and the rest
        of the slice is recognized on another thread while that first block is
        being spoken - which takes seconds, so the handover is never heard.

        Both lanes use accurate recognition. The obvious alternative, dropping
        Vision to its fast level, is eight times quicker and measurably wrong:
        it mangles "prose" into "PTose" and reading order comes out incorrect on
        three of seven golden pages. Speed is bought with area, not accuracy.

        Only the *first* block is spoken speculatively. It is parsed without
        the rest of the page for context, so its reading order is less certain
        than the full parse's - bounding that risk to one block costs almost
        nothing and removes the failure mode entirely.
        """
        if not self.should_speculate(source):
            return self.read(source, on_progress=on_progress)

        deep: dict = {}

        def recognize_everything() -> None:
            try:
                deep["reading"] = self.prepare(source)
            except Exception as exc:      # noqa: BLE001
                deep["error"] = exc

        worker = threading.Thread(target=recognize_everything, daemon=True)
        worker.start()

        memory = ReadingMemory()
        epoch = self.narrator.new_epoch()
        spoken: list[Utterance] = []
        timings = telemetry.Timings()

        with timings.stage("speculative"):
            try:
                head = self.prepare(source, top_fraction=SPECULATIVE_TOP)
                first = head.utterances[:1]
            except Exception:             # noqa: BLE001
                first = []
                head = None

        if first and head is not None:
            by_id = {b.id: b for b in head.slice.blocks}
            for progress in self.narrator.read(first, epoch):
                if on_progress:
                    on_progress(progress)
                spoken.append(progress.utterance)
                block = by_id.get(progress.utterance.block_id)
                if block is not None:
                    memory.remember(block)

        worker.join()
        if "error" in deep:
            if spoken:
                return Reading(slice=head.slice, utterances=spoken,
                               timings=timings, label=source.label,
                               notes=["only the top of the region could be read"])
            raise deep["error"]

        reading = deep["reading"]
        reading.label = source.label
        reading.timings.stages.update(timings.stages)

        start = memory.resume_index(reading.slice.readable())
        remaining = {b.id for b in reading.slice.readable()[start:]}
        pending = [u for u in reading.utterances if u.block_id in remaining]

        if epoch == self.narrator.epoch and pending:
            with reading.timings.stage("speak"):
                for progress in self.narrator.read(pending, epoch):
                    if on_progress:
                        on_progress(progress)
                    spoken.append(progress.utterance)

        reading.utterances = spoken or reading.utterances
        telemetry.record({"event": "responsive_reading",
                          "spoken": len(spoken), "stages": reading.timings.stages})
        return reading

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


    # -- reading past the fold --------------------------------------------

    def read_continuous(self, source: Capture, *, on_progress=None,
                        on_advance=None, scroller_factory=None) -> Reading:
        """Read the region, then keep going as the content scrolls.

        Every spoken block is remembered by fingerprint, so a screenful that
        overlaps the previous one resumes at the first unread block instead of
        repeating it. Reading stops - and always says why - when the content
        stops advancing, when the screen changes to something unrelated, or
        when the screen budget runs out.
        """
        memory = ReadingMemory()
        scroller = (scroller_factory or Scroller)(source)
        current = self.prepare(source)
        epoch = self.narrator.new_epoch()

        notes: list[str] = list(current.notes)
        timings = current.timings
        spoken: list[Utterance] = []

        while True:
            spoken.extend(self._speak_unread(current, memory, epoch, on_progress))
            if epoch != self.narrator.epoch:
                notes.append("reading was superseded")
                break

            advance = scroller.advance()
            if not advance.ok:
                notes.append(advance.reason)
                break

            try:
                nxt = self.prepare(advance.capture)
            except CaptureError as exc:
                notes.append(f"stopped: {exc}")
                break

            blocks = nxt.slice.readable()
            if memory.content_changed(blocks):
                # The screen is showing something else. Reading on would
                # narrate unrelated text in a confident voice.
                notes.append("stopped: the content changed on screen")
                break
            if memory.resume_index(blocks) >= len(blocks):
                notes.append("reached the end of the content")
                break

            for note in nxt.notes:
                if note not in notes:
                    notes.append(note)
            current = nxt
            if on_advance:
                on_advance(scroller.screens)

        telemetry.record({
            "event": "continuous_reading",
            "screens": scroller.screens,
            "blocks_spoken": len(memory),
            "notes": notes,
        })
        return Reading(slice=current.slice, utterances=spoken,
                       timings=timings, notes=notes)

    def _speak_unread(self, reading: Reading, memory: ReadingMemory, epoch: int,
                      on_progress) -> list[Utterance]:
        """Speak whatever on this screenful has not been read yet.

        The resume point is the block *after* the last one recognized, not the
        first unrecognized one, so a sticky header repeated at the top of every
        screen does not send the reading back to the start.
        """
        readable = reading.slice.readable()
        start = memory.resume_index(readable)
        remaining = {block.id for block in readable[start:]}
        pending = [u for u in reading.utterances if u.block_id in remaining]
        if not pending:
            return []

        by_id = {block.id: block for block in reading.slice.blocks}
        spoken: list[Utterance] = []
        for progress in self.narrator.read(pending, epoch):
            if on_progress:
                on_progress(progress)
            memory.remember(by_id[progress.utterance.block_id])
            spoken.append(progress.utterance)
        return spoken


def capture_for(region: str | None, file: str | None, *,
                window: bool = False, screen: bool = False) -> Capture:
    """Resolve the ways a reading can be aimed.

    Window and screen targets exist so the tool can be aimed without sight.
    Dragging a rectangle assumes you can see where the content is and confirm
    the selection landed on it; naming a target assumes neither.
    """
    if file:
        return capture_mod.capture_file(file)
    if window:
        return capture_mod.capture_window()
    if screen:
        return capture_mod.capture_display()
    if region:
        try:
            x, y, w, h = (int(part) for part in region.split(","))
        except ValueError:
            raise CaptureError(f"region must be x,y,w,h - got {region!r}") from None
        return capture_mod.capture_region(x, y, w, h)
    return capture_mod.capture_interactive()
