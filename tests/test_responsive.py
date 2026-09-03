"""Speaking before the whole slice has been recognized.

Recognition cost scales with the area examined, so reading the top of a capture
first gets a word out in roughly a fifth of the time while the rest is
recognized on another thread. The risks worth testing are the ones that would
be inaudible: content spoken twice, content never spoken, and the optimisation
running when it cannot pay for itself.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.capture import Capture, capture_file                   # noqa: E402
from slicer.conductor import SPECULATIVE_MIN_HEIGHT, Conductor     # noqa: E402
from slicer.narrator import Narrator                               # noqa: E402
from tests import fixtures                                         # noqa: E402

PARAGRAPHS = [
    "Alpha the opening paragraph introduces the subject plainly.",
    "Bravo the second paragraph develops it with some detail.",
    "Charlie the third paragraph turns to a different aspect.",
    "Delta the fourth paragraph weighs the counterarguments.",
    "Echo the fifth paragraph returns to the original thread.",
    "Foxtrot the sixth paragraph draws the argument together.",
    "Golf the seventh paragraph closes the discussion neatly.",
]


class SilentNarrator(Narrator):
    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []

    def _say(self, text: str, epoch: int) -> bool:
        if epoch != self._epoch:
            return False
        self.spoken.append(text)
        return True


def _tall_capture() -> Capture:
    # Generous leading so the page clears the speculation threshold; the point
    # of the fixture is its height, not its density.
    path, _ = fixtures.tall_page(PARAGRAPHS, width=900, leading=90)
    capture = capture_file(path)
    capture.origin_known = True
    assert capture.height >= SPECULATIVE_MIN_HEIGHT, "fixture is not tall enough"
    return capture


def test_a_small_capture_does_not_speculate():
    """Below the threshold the full parse is already quick; a second pass is waste."""
    path, _ = fixtures.single_column(["A single short line of text."], height=300)
    capture = capture_file(path)
    assert capture.height < SPECULATIVE_MIN_HEIGHT
    assert Conductor(narrator=SilentNarrator()).should_speculate(capture) is False


def test_a_tall_capture_does_speculate():
    capture = _tall_capture()
    assert capture.height >= SPECULATIVE_MIN_HEIGHT
    assert Conductor(narrator=SilentNarrator()).should_speculate(capture) is True


def test_every_paragraph_is_read_exactly_once():
    """The failure that would be inaudible: the speculative block spoken twice."""
    narrator = SilentNarrator()
    Conductor(narrator=narrator).read_responsive(_tall_capture())
    spoken = " ".join(narrator.spoken)
    for paragraph in PARAGRAPHS:
        marker = paragraph.split()[0]
        assert spoken.count(marker) == 1, (
            f"{marker} was read {spoken.count(marker)} times")


def test_content_is_read_in_order():
    narrator = SilentNarrator()
    Conductor(narrator=narrator).read_responsive(_tall_capture())
    spoken = " ".join(narrator.spoken)
    positions = [spoken.find(p.split()[0]) for p in PARAGRAPHS]
    assert positions == sorted(positions), "the splice reordered the reading"


def test_the_speculative_pass_is_timed_separately():
    """So a regression in first-word latency is visible rather than averaged away."""
    reading = Conductor(narrator=SilentNarrator()).read_responsive(_tall_capture())
    assert "speculative" in reading.timings.stages
    assert reading.timings.stages["speculative"] > 0


def test_reading_the_top_first_is_cheaper_than_reading_everything():
    """The premise of the whole optimisation, asserted rather than assumed."""
    from slicer.ocr import recognize
    capture = _tall_capture()
    recognize(capture.source)                       # warm
    import time
    start = time.perf_counter()
    recognize(capture.source)
    full = time.perf_counter() - start
    start = time.perf_counter()
    recognize(capture.source, top_fraction=0.25)
    top = time.perf_counter() - start
    assert top < full, f"top-only ({top*1000:.0f}ms) was not cheaper than full ({full*1000:.0f}ms)"


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
