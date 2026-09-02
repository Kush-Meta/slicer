"""Tests for the parts that would fail silently in production.

Reading order and grounding get the most attention because both fail without
raising: a wrong order is merely spoken, and an invented word sounds exactly
like a real one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer import editor, layout                                    # noqa: E402
from slicer.blocks import Block, BlockKind, Box, TextLine            # noqa: E402
from slicer.capture import CaptureError, _luminance_variance         # noqa: E402
from slicer.ocr import recognize                                     # noqa: E402
from tests import fixtures                                           # noqa: E402

BODY_L = ["Reading order is the hard", "problem here, not the text", "recognition step at all."]
BODY_R = ["A second column of body", "text that should be read", "after the first finishes."]


def _read(path: str) -> str:
    return " ".join(b.text for b in layout.build_slice(recognize(path)).readable())


def _naive(path: str) -> str:
    lines = recognize(path).lines
    return " ".join(l.text for l in sorted(lines, key=lambda l: (l.box.y, l.box.x)))


def _block(texts: list[str], kind: BlockKind = BlockKind.BODY, conf: float = 0.99) -> Block:
    lines = [TextLine(id=f"l{i}", text=t, confidence=conf, box=Box(0, i * 20, 100, 20))
             for i, t in enumerate(texts)]
    return Block(id="b1", lines=lines, kind=kind)


# -- reading order ---------------------------------------------------------

def test_single_column_reads_in_order():
    lines = ["First line of the document.", "Second line follows it.", "Third line ends it."]
    path, expected = fixtures.single_column(lines)
    assert _read(path) == " ".join(expected)


def test_two_columns_read_down_not_across():
    path, expected = fixtures.two_column(BODY_L, BODY_R)
    assert _read(path) == " ".join(expected)
    # The whole point: naive top-to-bottom sorting gets this wrong.
    assert _naive(path) != " ".join(expected)


def test_header_above_two_columns():
    path, expected = fixtures.header_two_column("A Full Width Headline", BODY_L, BODY_R)
    assert _read(path) == " ".join(expected)
    assert _naive(path) != " ".join(expected)


def test_narrow_navigation_column_is_skipped():
    nav = ["Home", "About", "Pricing", "Docs", "Blog"]
    body = ["The body text begins here and continues.",
            "A second sentence of real content follows."]
    path, expected = fixtures.sidebar_and_body(nav, body)
    slice_ = layout.build_slice(recognize(path))
    assert " ".join(b.text for b in slice_.readable()) == " ".join(expected)
    skipped = " ".join(b.text for b in slice_.skipped())
    assert all(item in skipped for item in nav)
    # Every skip carries an auditable reason.
    assert all(b.reason for b in slice_.skipped())


def test_equal_columns_are_not_mistaken_for_navigation():
    """Two narrow columns of short lines are a two-column page, not two sidebars."""
    short_l = ["Left one", "Left two", "Left three"]
    short_r = ["Right one", "Right two", "Right three"]
    path, expected = fixtures.two_column(short_l, short_r)
    slice_ = layout.build_slice(recognize(path))
    assert slice_.skipped() == [] or slice_.degraded
    assert " ".join(b.text for b in slice_.readable()) == " ".join(expected)


def test_ordering_is_deterministic():
    path, _ = fixtures.header_two_column("A Full Width Headline", BODY_L, BODY_R)
    result = recognize(path)
    runs = {" ".join(b.text for b in layout.build_slice(result).readable()) for _ in range(5)}
    assert len(runs) == 1


# -- the grounding invariant ----------------------------------------------

def test_invented_word_is_rejected():
    block = _block(["The quick brown fox."])
    try:
        editor.assert_grounded("The quick brown elephant.", block)
    except editor.UngroundedSpeech:
        return
    raise AssertionError("an invented word was allowed through")


def test_duplicated_word_is_rejected():
    block = _block(["The quick brown fox."])
    try:
        editor.assert_grounded("The quick quick brown fox.", block)
    except editor.UngroundedSpeech:
        return
    raise AssertionError("a duplicated word was allowed through")


def test_grounded_text_passes():
    editor.assert_grounded("The quick brown fox.", _block(["The quick brown fox."]))


def test_line_break_healing_is_grounded():
    """A word split by a line break was on screen in one piece."""
    assert editor.to_speech(_block(["This is inter-", "national law."])).text == \
        "This is international law."


def test_capitalised_join_is_not_healed():
    text = editor.to_speech(_block(["A well-", "Known name follows."])).text
    assert "wellKnown" not in text


# -- editor ----------------------------------------------------------------

def test_url_is_spoken_not_spelled():
    text = editor.to_speech(_block(["See https://www.example.com/docs/page for more."])).text
    assert text == "See link to example dot com for more."


def test_numbers_are_left_intact():
    """macOS speech already pronounces these correctly; rewriting them adds risk."""
    assert "$4,318" in editor.to_speech(_block(["Revenue fell 12% to $4,318."])).text


def test_code_is_summarised_not_read_character_by_character():
    block = _block(["for (i = 0; i < n; i++) {", "  x[i] = y[i] * 2;", "}"], kind=BlockKind.CODE)
    utterance = editor.to_speech(block)
    # The generated count lives in narration, never in grounded content.
    assert utterance.prefix.startswith("code block, 3 lines")
    assert "3" not in utterance.text
    assert utterance.spoken.startswith("code block, 3 lines, beginning for")


def test_narration_is_separate_from_content():
    """Words Slicer generates must never be mixed into grounded screen text."""
    low = _block(["Some faint text here."], conf=0.20)
    utterance = editor.to_speech(low, min_confidence=0.45)
    assert utterance.prefix == "unclear,"
    assert utterance.text == "Some faint text here."
    editor.assert_grounded(utterance.text, low)


def test_prose_is_not_mistaken_for_code():
    """Any single code signal misfires on ordinary prose; several must agree."""
    from slicer.layout import LayoutConfig, _code_score
    cfg = LayoutConfig()
    prose = [
        "Use the function() to return a value here.",
        "The report, which is long, covers costs, revenue, and margins.",
        "We saw two incidents, both caused by the same expired certificate.",
    ]
    for text in prose:
        line = [TextLine(id="l", text=text, confidence=1.0, box=Box(0, 0, 100, 20))]
        assert _code_score(line, text, cfg)[0] is None, text


def test_real_code_is_detected():
    from slicer.layout import LayoutConfig, _code_score
    text = "def rotate(cert): if cert.expires_in() < 30: renew(cert)"
    line = [TextLine(id="l", text=text, confidence=1.0, box=Box(0, 0, 100, 20))]
    kind, reason = _code_score(line, text, LayoutConfig())
    assert kind == BlockKind.CODE and reason


def test_chrome_blocks_are_not_spoken():
    assert editor.to_speech(_block(["Home"], kind=BlockKind.CHROME)) is None


# -- capture ---------------------------------------------------------------

def test_uniform_frame_is_detected_as_a_capture_failure():
    """A revoked permission returns black frames; the API reports success."""
    blank = fixtures.render([], width=400, height=300, dark=True)
    assert _luminance_variance(blank) < 4.0
    real, _ = fixtures.single_column(["Some real text on the screen."])
    assert _luminance_variance(real) >= 4.0


def test_capture_rejects_empty_region():
    from slicer.capture import capture_region
    try:
        capture_region(0, 0, 0, 0)
    except CaptureError:
        return
    raise AssertionError("a zero-area region was accepted")


# -- runner ----------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mpass\033[0m  {name}")
        except Exception as exc:
            failed.append((name, exc))
            print(f"  \033[31mFAIL\033[0m  {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
