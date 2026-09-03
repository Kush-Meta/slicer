"""Navigating a reading by structure.

The cursor is pure logic and gets tested exhaustively, because a navigation bug
is invisible to a user who cannot see the screen - they would simply believe
the content was not there.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.blocks import Block, BlockKind, Box, Slice, TextLine   # noqa: E402
from slicer.editor import UngroundedSpeech, Verbosity, spell       # noqa: E402
from slicer.navigator import (                                     # noqa: E402
    Command, Cursor, Navigator, command_for,
)


def _block(ident: str, kind: BlockKind, text: str) -> Block:
    return Block(id=ident, kind=kind, lines=[
        TextLine(id=f"l{ident}", text=text, confidence=1.0, box=Box(0, 0, 100, 20))])


PAGE = [
    _block("1", BlockKind.HEADING, "Quarterly Results"),
    _block("2", BlockKind.BODY, "Revenue grew across every region this quarter."),
    _block("3", BlockKind.TABLE_ROW, "North 4,318"),
    _block("4", BlockKind.TABLE_ROW, "South 2,901"),
    _block("5", BlockKind.HEADING, "Reliability"),
    _block("6", BlockKind.CODE, "def rotate(cert):"),
    _block("7", BlockKind.BODY, "The certificate now rotates automatically."),
]


def _nav(blocks=None, label: str = "") -> Navigator:
    # `blocks or PAGE` would treat an empty reading as "use the default",
    # which is exactly the case the empty-reading test needs.
    chosen = PAGE if blocks is None else blocks
    slice_ = Slice(blocks=list(chosen), width=800, height=600)
    return Navigator(slice_, verbosity=Verbosity.LOW, label=label)


# -- cursor ----------------------------------------------------------------

def test_stepping_forward_and_back():
    cursor = Cursor(blocks=list(PAGE))
    assert cursor.move(1) and cursor.index == 1
    assert cursor.move(-1) and cursor.index == 0


def test_the_cursor_stops_at_the_edges():
    cursor = Cursor(blocks=list(PAGE))
    assert not cursor.move(-1), "moved before the first block"
    cursor.go(len(PAGE) - 1)
    assert not cursor.move(1), "moved past the last block"


def test_jumping_to_the_next_heading_skips_intervening_blocks():
    cursor = Cursor(blocks=list(PAGE))
    assert cursor.seek_kind(BlockKind.HEADING)
    assert cursor.current.text == "Reliability"


def test_jumping_backwards_finds_the_earlier_heading():
    cursor = Cursor(blocks=list(PAGE), index=6)
    assert cursor.seek_kind(BlockKind.HEADING, backward=True)
    assert cursor.current.text == "Reliability"


def test_seeking_a_kind_that_is_not_there_does_not_move():
    cursor = Cursor(blocks=list(PAGE), index=0)
    assert not cursor.seek_kind(BlockKind.CAPTION)
    assert cursor.index == 0, "a failed jump moved the cursor anyway"


def test_an_empty_reading_has_no_position():
    cursor = Cursor(blocks=[])
    assert cursor.current is None
    assert not cursor.move(1)


# -- commands --------------------------------------------------------------

def test_a_failed_jump_says_so_rather_than_going_silent():
    """Silence after a keypress is indistinguishable from a crash."""
    nav = _nav()
    nav.cursor.go(len(PAGE) - 1)
    spoken = nav.handle(Command.NEXT_HEADING)
    assert spoken and "no more headings" in spoken[0].spoken


def test_moving_speaks_the_block_it_landed_on():
    nav = _nav()
    spoken = nav.handle(Command.NEXT_HEADING)
    assert "Reliability" in spoken[0].spoken


def test_where_am_i_reports_kind_and_position():
    nav = _nav(label="Safari, Quarterly Review")
    nav.cursor.go(2)
    where = nav.handle(Command.WHERE)[0].spoken
    assert "table row" in where and "3 of 7" in where and "Safari" in where


def test_the_opening_says_what_was_aimed_at():
    opening = _nav(label="Safari").opening()
    assert "Safari" in opening[0].spoken
    assert "7 blocks" in opening[0].spoken
    assert "2 headings" in opening[0].spoken


def test_an_empty_reading_is_announced_not_silent():
    assert "nothing readable" in _nav(blocks=[]).opening()[0].spoken


def test_say_all_reads_from_the_cursor_to_the_end():
    nav = _nav()
    nav.cursor.go(5)
    spoken = " ".join(u.spoken for u in nav.handle(Command.SAY_ALL))
    assert "rotates automatically" in spoken
    assert "Quarterly Results" not in spoken, "say all went backwards"


def test_repeat_does_not_move():
    nav = _nav()
    nav.cursor.go(3)
    nav.handle(Command.REPEAT)
    assert nav.cursor.index == 3


# -- key bindings ----------------------------------------------------------

def test_bindings_match_screen_reader_convention():
    """H and T with shift reversing are the same in NVDA, JAWS and VoiceOver."""
    assert command_for("h") is Command.NEXT_HEADING
    assert command_for("H") is Command.PREVIOUS_HEADING
    assert command_for("t") is Command.NEXT_TABLE
    assert command_for("T") is Command.PREVIOUS_TABLE
    assert command_for("a") is Command.SAY_ALL
    assert command_for("\x1b[B") is Command.NEXT      # down arrow
    assert command_for("\x1b[A") is Command.PREVIOUS  # up arrow


def test_an_unbound_key_is_ignored_rather_than_guessed():
    assert command_for("z") is None


# -- spelling --------------------------------------------------------------

def test_spelling_reads_characters():
    spoken = spell(_block("x", BlockKind.BODY, "Hi there")).spoken
    assert spoken.startswith("spelling,")
    assert "H," in spoken and "space," in spoken


def test_spelling_is_checked_against_the_source():
    """Token membership is meaningless once words become letters, so the check
    is stronger: the letters rejoined must reproduce the source exactly."""
    from slicer.editor import assert_spelling_grounded
    assert_spelling_grounded("a, b, c,", "abc")
    try:
        assert_spelling_grounded("a, b, d,", "abc")
    except UngroundedSpeech:
        return
    raise AssertionError("spelling that does not match the source was allowed")


def test_narration_carries_no_content():
    """Feedback is a fact about the reading, not a claim about the screen."""
    from slicer.editor import Utterance
    narration = Utterance.narration("no more headings")
    assert narration.text == "" and narration.block_id == ""
    assert narration.spoken == "no more headings"


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
