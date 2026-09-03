"""Screen-reader behaviour: aiming without sight, and announcing structure.

Everything here exists because the sighted interaction model does not survive
contact with a non-visual user. Dragging a box assumes you can see where the
content is and confirm the selection landed on it; spoken output loses every
structural cue that layout gives a sighted reader for free.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer import layout                                  # noqa: E402
from slicer.blocks import Block, BlockKind, Box, TextLine   # noqa: E402
from slicer.editor import Verbosity, describe, to_speech    # noqa: E402
from slicer.ocr import recognize                            # noqa: E402
from slicer.windows import WindowRef, frontmost_window, list_windows  # noqa: E402
from tests import fixtures                                  # noqa: E402


def _block(kind: BlockKind, text: str = "Some text here.", **kw) -> Block:
    return Block(id="b1", kind=kind, lines=[
        TextLine(id="l", text=text, confidence=1.0, box=Box(0, 0, 100, 20))], **kw)


# -- aiming without sight --------------------------------------------------

def test_a_window_can_be_found_without_a_selection():
    """The primary target for a non-visual user is 'the window I am in'."""
    window = frontmost_window()
    assert window is not None, "no frontmost window found"
    assert window.w > 0 and window.h > 0
    assert window.region == (window.x, window.y, window.w, window.h)


def test_windows_are_reported_with_something_to_say():
    """A listener needs to hear what Slicer aimed at, since they cannot see it."""
    for window in list_windows():
        assert window.label.strip(), "a window with no spoken label"


def test_our_own_overlays_are_never_reading_targets():
    from slicer.windows import OWN_NAMES
    assert all(w.app not in OWN_NAMES for w in list_windows())


def test_a_window_label_prefers_the_document_over_the_app():
    named = WindowRef(1, 1, "Safari", "Quarterly Review", 0, 0, 800, 600)
    plain = WindowRef(1, 1, "Safari", "Safari", 0, 0, 800, 600)
    assert named.label == "Safari, Quarterly Review"
    assert plain.label == "Safari"


# -- announcing structure --------------------------------------------------

def test_a_heading_is_announced_as_one():
    assert describe(_block(BlockKind.HEADING)).startswith("heading")


def test_a_table_row_announces_its_position():
    row = _block(BlockKind.TABLE_ROW, "North 4,318", index_in_group=2, group_size=4)
    assert describe(row) == "row 2 of 4,"


def test_body_text_is_not_announced():
    assert describe(_block(BlockKind.BODY)) == ""


def test_verbosity_off_says_nothing_structural():
    row = _block(BlockKind.TABLE_ROW, "North 4,318", index_in_group=2, group_size=4)
    assert describe(row, Verbosity.OFF) == ""


def test_high_verbosity_adds_position_in_the_reading():
    described = describe(_block(BlockKind.BODY), Verbosity.HIGH, index=3, total=12)
    assert "3 of 12" in described


def test_announcements_are_narration_not_content():
    """The invariant: Slicer's own words never enter the checked text."""
    row = _block(BlockKind.TABLE_ROW, "North 4,318", index_in_group=2, group_size=4)
    utterance = to_speech(row, verbosity=Verbosity.LOW)
    assert utterance.text == "North 4,318"
    assert "row 2 of 4" in utterance.prefix
    assert "row" not in utterance.text
    from slicer.editor import assert_grounded
    assert_grounded(utterance.text, row)


def test_structure_survives_a_real_page():
    """End to end: a rendered page announces its heading and its rows."""
    rows = [["Region", "Revenue"], ["North", "4,318"], ["South", "2,901"]]
    placed = [fixtures.Placed("Regional Results", 60, 30, 30)]
    placed += [fixtures.Placed(cell, 80 + c * 260, 110 + r * 46, 20)
               for r, row in enumerate(rows) for c, cell in enumerate(row)]
    slice_ = layout.build_slice(recognize(fixtures.render(placed, width=760, height=300)))
    readable = slice_.readable()
    spoken = [to_speech(b, verbosity=Verbosity.LOW, index=i, total=len(readable)).spoken
              for i, b in enumerate(readable, 1)]
    assert spoken[0].startswith("heading,")
    assert any(s.startswith("row 1 of 3,") for s in spoken)
    assert any(s.startswith("row 3 of 3,") for s in spoken)


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
