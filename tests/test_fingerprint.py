"""Continuity: never read a paragraph twice, never skip one."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.blocks import Block, Box, TextLine                      # noqa: E402
from slicer.fingerprint import ReadingMemory, shingles, similarity  # noqa: E402

P1 = "Latency improved across every service this quarter and the median request now completes quickly."
P2 = "Reliability stayed flat with two incidents both traced to the same expired certificate."
P3 = "The third paragraph introduces an entirely separate subject about hiring and team growth."


def _block(text: str, ident: str = "b") -> Block:
    return Block(id=ident, lines=[TextLine(id="l", text=text, confidence=1.0,
                                           box=Box(0, 0, 100, 20))])


def test_identical_text_matches():
    assert similarity(shingles(P1), shingles(P1)) == 1.0


def test_different_paragraphs_do_not_match():
    # Measured at 0.04; the bar is set well above that so a regression shows.
    assert similarity(shingles(P1), shingles(P3)) < 0.25


def test_a_header_does_not_match_body_text():
    assert similarity(shingles("Quarterly Engineering Review"), shingles(P1)) < 0.25


def test_a_clipped_paragraph_still_matches_the_whole_one():
    """Half a paragraph at the top of the screen is not a new paragraph."""
    half = " ".join(P1.split()[:8])
    assert similarity(shingles(half), shingles(P1)) >= 0.55


def test_recognition_errors_still_match():
    """Word trigrams scored 0.50 here and lost the block. Character grams score 0.84.

    Recognition errors are guaranteed in real use, so a fingerprint that cannot
    survive three of them would re-read paragraphs on every scroll.
    """
    two_errors = P1.replace("median", "rnedian").replace("quarter", "quarler")
    three_errors = (P1.replace("Latency", "Latencv").replace("service", "servlce")
                      .replace("completes", "cornpletes"))
    assert similarity(shingles(two_errors), shingles(P1)) >= 0.75
    assert similarity(shingles(three_errors), shingles(P1)) >= 0.75


def test_damaged_text_still_resumes_correctly():
    """End to end: a scroll where recognition differs slightly must not repeat."""
    memory = ReadingMemory()
    memory.remember(_block(P1))
    memory.remember(_block(P2))
    damaged_p2 = P2.replace("incidents", "lncidents").replace("expired", "explred")
    assert memory.resume_index([_block(damaged_p2), _block(P3)]) == 1


def test_resume_skips_what_was_already_read():
    memory = ReadingMemory()
    memory.remember(_block(P1))
    memory.remember(_block(P2))
    # After scrolling, the screen shows P2 again at the top, then P3.
    after_scroll = [_block(P2), _block(P3)]
    assert memory.resume_index(after_scroll) == 1


def test_nothing_is_skipped_when_the_screen_is_all_new():
    memory = ReadingMemory()
    memory.remember(_block(P1))
    assert memory.resume_index([_block(P2), _block(P3)]) == 0


def test_a_repeated_header_does_not_rewind_the_reading():
    """A sticky header appears on every screen; the last match must win."""
    header = _block("Quarterly Engineering Review", "h")
    memory = ReadingMemory()
    memory.remember(header)
    memory.remember(_block(P1))
    after_scroll = [header, _block(P1), _block(P2)]
    assert memory.resume_index(after_scroll) == 2


def test_content_change_is_detected():
    memory = ReadingMemory()
    memory.remember(_block(P1))
    memory.remember(_block(P2))
    assert memory.content_changed([_block("Totally unrelated navigation menu here now")])


def test_normal_scrolling_is_not_mistaken_for_a_content_change():
    memory = ReadingMemory()
    memory.remember(_block(P1))
    memory.remember(_block(P2))
    assert not memory.content_changed([_block(P2), _block(P3)])


def test_a_first_capture_never_reports_a_change():
    assert not ReadingMemory().content_changed([_block(P1)])


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
