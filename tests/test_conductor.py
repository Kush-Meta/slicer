"""Pipeline behaviour: what the Conductor does when a stage cannot deliver.

The degradation ladder says every path either speaks or says why it cannot.
These check the "says why" half, which is the half that fails silently.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.capture import CaptureError, capture_file      # noqa: E402
from slicer.conductor import Conductor                     # noqa: E402
from tests import fixtures                                 # noqa: E402


def test_a_blank_region_is_reported_not_silently_empty():
    blank = fixtures.render([], width=600, height=400)
    try:
        Conductor().prepare(capture_file(blank))
    except CaptureError as exc:
        assert "no text" in str(exc).lower()
        assert exc.remedy, "an error with no remedy leaves the user stuck"
        return
    raise AssertionError("a blank region produced a reading")


def test_navigation_is_reported_in_the_notes():
    path, _ = fixtures.sidebar_and_body(
        ["Home", "About", "Pricing", "Docs", "Blog"],
        ["The body text begins here and continues.",
         "A second sentence of real content follows."])
    reading = Conductor().prepare(capture_file(path))
    assert any("navigation" in note for note in reading.notes)


def test_a_reading_carries_stage_timings():
    path, _ = fixtures.single_column(["A line of text to read aloud."])
    reading = Conductor().prepare(capture_file(path))
    assert {"ocr", "layout", "edit"} <= set(reading.timings.stages)
    assert reading.timings.to_first_word() > 0


def test_every_utterance_is_grounded_in_its_block():
    """The end-to-end form of the invariant, over a whole realistic page."""
    from slicer.editor import assert_grounded
    path, _ = fixtures.header_two_column(
        "A Full Width Headline",
        ["Reading order is the hard", "problem here, not the text"],
        ["A second column of body", "text that should be read"])
    reading = Conductor().prepare(capture_file(path))
    by_id = {b.id: b for b in reading.slice.blocks}
    for utterance in reading.utterances:
        assert_grounded(utterance.text, by_id[utterance.block_id])


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
