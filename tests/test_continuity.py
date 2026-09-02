"""Reading past the fold, without depending on real scroll events.

Scrolling is simulated by cropping successive windows out of one tall page,
which is exactly what a scroll looks like to the pipeline: overlapping captures
of the same region. That makes the resume logic testable deterministically,
including the two failures that matter - a paragraph read twice, and one never
read at all.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.capture import capture_file                    # noqa: E402
from slicer.conductor import Conductor                     # noqa: E402
from slicer.continuity import Advance                      # noqa: E402
from slicer.narrator import Narrator                       # noqa: E402
from tests import fixtures                                 # noqa: E402

PARAGRAPHS = [
    "Alpha the opening paragraph introduces the subject clearly.",
    "Bravo the second paragraph develops it with further detail.",
    "Charlie the third paragraph turns to a different aspect.",
    "Delta the fourth paragraph considers some counterarguments.",
    "Echo the fifth paragraph returns to the original thread.",
    "Foxtrot the sixth paragraph begins drawing things together.",
    "Golf the seventh paragraph summarises what has been covered.",
    "Hotel the eighth and final paragraph closes the discussion.",
]

VIEWPORT = 260
STEP = 170          # less than the viewport, so captures overlap


class SilentNarrator(Narrator):
    """Speaks instantly, so continuity can be tested without audio."""

    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []

    def _say(self, text: str, epoch: int) -> bool:
        if epoch != self._epoch:
            return False
        self.spoken.append(text)
        return True


class CropScroller:
    """Simulates scrolling by cropping down a tall page."""

    def __init__(self, source, page: str, page_height: int, width: int):
        self.page, self.page_height, self.width = page, page_height, width
        self.offset = 0
        self.screens = 1
        self.crops: list[str] = []

    def advance(self) -> Advance:
        # Model real scrolling: the last step clamps to the bottom rather than
        # being refused, so content in the final partial screen is still shown.
        limit = self.page_height - VIEWPORT
        if self.offset >= limit:
            return Advance(None, "reached the bottom of the page")
        self.offset = min(self.offset + STEP, limit)
        self.screens += 1
        path = fixtures.crop(self.page, 0, self.offset, self.width, VIEWPORT)
        self.crops.append(path)
        return Advance(capture_file(path))


def _run(paragraphs=PARAGRAPHS):
    page, height = fixtures.tall_page(paragraphs, width=760)
    first = fixtures.crop(page, 0, 0, 760, VIEWPORT)
    narrator = SilentNarrator()
    conductor = Conductor(narrator=narrator)
    reading = conductor.read_continuous(
        capture_file(first),
        scroller_factory=lambda src: CropScroller(src, page, height, 760),
    )
    return narrator, reading


def test_every_paragraph_is_read():
    narrator, reading = _run()
    spoken = " ".join(narrator.spoken)
    missing = [p.split()[0] for p in PARAGRAPHS if p.split()[0] not in spoken]
    assert not missing, f"never read: {missing}"


def test_no_paragraph_is_read_twice():
    narrator, _ = _run()
    spoken = " ".join(narrator.spoken)
    for paragraph in PARAGRAPHS:
        marker = paragraph.split()[0]
        assert spoken.count(marker) == 1, f"{marker} was read {spoken.count(marker)} times"


def test_paragraphs_are_read_in_order():
    narrator, _ = _run()
    spoken = " ".join(narrator.spoken)
    positions = [spoken.find(p.split()[0]) for p in PARAGRAPHS]
    assert positions == sorted(positions), "content was read out of order"


def test_reaching_the_end_is_reported():
    _, reading = _run()
    assert any("end" in note or "bottom" in note for note in reading.notes), reading.notes


def test_a_content_change_stops_the_reading():
    """Navigating away mid-reading must stop, not narrate whatever is now there."""
    page, height = fixtures.tall_page(PARAGRAPHS, width=760)
    other, _ = fixtures.tall_page(
        ["Something entirely different now fills the screen.",
         "None of this relates to what was being read before.",
         "A completely unrelated document has replaced it."], width=760)
    first = fixtures.crop(page, 0, 0, 760, VIEWPORT)

    class SwapScroller(CropScroller):
        def advance(self):
            self.screens += 1
            if self.screens > 2:
                return Advance(capture_file(fixtures.crop(other, 0, 0, 760, VIEWPORT)))
            self.offset += STEP
            return Advance(capture_file(fixtures.crop(page, 0, self.offset, 760, VIEWPORT)))

    narrator = SilentNarrator()
    reading = Conductor(narrator=narrator).read_continuous(
        capture_file(first),
        scroller_factory=lambda src: SwapScroller(src, page, height, 760),
    )
    assert any("content changed" in note for note in reading.notes), reading.notes
    assert "entirely different" not in " ".join(narrator.spoken)


def test_a_capture_without_a_region_cannot_be_continued():
    from slicer.capture import CaptureError
    path, _ = fixtures.single_column(["A line of text."])
    try:
        Conductor(narrator=SilentNarrator()).read_continuous(capture_file(path))
    except CaptureError as exc:
        assert "where it came from" in str(exc)
        return
    raise AssertionError("a file capture was continued without a region")


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mpass\033[0m  {name}")
        except Exception as exc:
            failed.append(name)
            print(f"  \033[31mFAIL\033[0m  {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
