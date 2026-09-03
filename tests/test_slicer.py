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
    """Two columns of equal width are a two-column page, not two sidebars.

    Only the skip behaviour is asserted. Two narrow columns of two-word items
    are genuinely ambiguous - visually that is also a two-column grid - so the
    reading order is not pinned here. What must never happen is a column being
    silently dropped. Column order on real prose is covered by the golden set.
    """
    short_l = ["Left one", "Left two", "Left three"]
    short_r = ["Right one", "Right two", "Right three"]
    path, expected = fixtures.two_column(short_l, short_r)
    slice_ = layout.build_slice(recognize(path))
    assert slice_.skipped() == [] or slice_.degraded
    read = " ".join(b.text for b in slice_.readable())
    for phrase in expected:
        assert phrase in read, f"{phrase!r} was lost"


def test_ordering_is_deterministic():
    path, _ = fixtures.header_two_column("A Full Width Headline", BODY_L, BODY_R)
    result = recognize(path)
    runs = {" ".join(b.text for b in layout.build_slice(result).readable()) for _ in range(5)}
    assert len(runs) == 1


def test_three_columns_are_all_read():
    """Regression: the third column was silently deleted.

    Binary recursion produced left(left(A,B),C), so C was compared against the
    combined width of A and B and classified as a sidebar. Nothing was spoken
    and nothing was reported.
    """
    # Prose-length lines, deliberately: three columns of two-word items are
    # genuinely grid-shaped and reading them row-wise is defensible. Running
    # text is unambiguously columns, which is what this regression is about.
    columns = [["Alpha begins the first column", "and continues for a while",
                "before the column ends here"],
               ["Bravo opens the middle column", "with its own running text",
                "that fills the middle nicely"],
               ["Charlie holds the last column", "which must not be dropped",
                "as it silently once was"]]
    placed = [fixtures.Placed(t, 40 + c * 320, 60 + i * 36, 16)
              for c, col in enumerate(columns) for i, t in enumerate(col)]
    path = fixtures.render(placed, width=1020, height=260)
    slice_ = layout.build_slice(recognize(path))
    read = " ".join(b.text for b in slice_.readable())
    assert read == " ".join(t for col in columns for t in col)
    assert slice_.skipped() == []


def test_table_is_read_row_wise():
    """Regression: grids were read down each column.

    "Region North South West, Revenue 4,318 2,901 5,144" is technically all the
    text and carries none of the meaning.
    """
    rows = [["Region", "Revenue", "Growth"], ["North", "4,318", "12%"],
            ["South", "2,901", "8%"], ["West", "5,144", "21%"]]
    placed = [fixtures.Placed(cell, 80 + c * 260, 60 + r * 46, 20)
              for r, row in enumerate(rows) for c, cell in enumerate(row)]
    path = fixtures.render(placed, width=900, height=300)
    blocks = layout.build_slice(recognize(path)).readable()
    assert all(b.kind == BlockKind.TABLE_ROW for b in blocks)
    assert [b.text for b in blocks] == [" ".join(row) for row in rows]


def test_a_sidebar_beside_a_paragraph_is_not_a_table():
    """Regression: table detection interleaved navigation with body text.

    A column of one-word links has a median line length of one, which passed
    the median test. It produced "Home The body text begins here" as one row.
    """
    path, _ = fixtures.sidebar_and_body(
        ["Home", "About", "Pricing", "Docs", "Blog"],
        ["The body text begins here and continues.",
         "A second sentence of real content follows."])
    blocks = layout.build_slice(recognize(path)).readable()
    assert not any(b.kind == BlockKind.TABLE_ROW for b in blocks)
    assert not any("Home" in b.text for b in blocks)


def test_over_skip_guard_counts_words_not_blocks():
    """Five one-word links are five blocks but a quarter of the words.

    Counting blocks made a small sidebar look like 83% of the page, which
    tripped the guard and un-skipped the navigation.
    """
    path, _ = fixtures.sidebar_and_body(
        ["Home", "About", "Pricing", "Docs", "Blog"],
        ["The body text begins here and continues.",
         "A second sentence of real content follows."])
    slice_ = layout.build_slice(recognize(path))
    assert len(slice_.skipped()) == 5
    assert slice_.degraded == []


def test_a_column_beside_a_wide_subtree_is_not_deleted():
    """Regression, and the second time this bug class appeared.

    A heading that bridges two columns makes the first cut separate the third
    column as a top-level sibling. Chrome detection then compared its width
    against that sibling's bounding box - which spans the whole rest of the
    page - and deleted it. Comparing median line width instead makes the test
    a property of the content rather than of how the recursion happened to
    split.

    Reading order for this layout is still imperfect: the third column is read
    after the table rather than as part of it. That is a known limitation, and
    a deliberately smaller problem than losing the column entirely.
    """
    rows = [["Region", "Revenue", "Growth"], ["North", "4,318", "12%"],
            ["South", "2,901", "8%"]]
    placed = [fixtures.Placed("Regional Results", 60, 30, 30)]
    placed += [fixtures.Placed(cell, 80 + c * 230, 110 + r * 46, 20)
               for r, row in enumerate(rows) for c, cell in enumerate(row)]
    slice_ = layout.build_slice(recognize(fixtures.render(placed, width=860, height=300)))
    spoken = " ".join(b.text for b in slice_.readable())
    for cell in ["Growth", "12%", "8%", "Region", "North", "4,318"]:
        assert cell in spoken, f"{cell!r} was silently dropped"
    assert slice_.skipped() == []


def test_no_content_is_ever_silently_lost():
    """Whatever the classification, every recognized line is accounted for."""
    for path in [
        fixtures.two_column(BODY_L, BODY_R)[0],
        fixtures.header_two_column("A Full Width Headline", BODY_L, BODY_R)[0],
        fixtures.sidebar_and_body(["Home", "About", "Docs"], BODY_L)[0],
    ]:
        result = recognize(path)
        slice_ = layout.build_slice(result)
        placed = sum(len(b.lines) for b in slice_.blocks)
        assert placed == len(result.lines), f"{len(result.lines) - placed} lines vanished"


# -- the grounding invariant ----------------------------------------------

def test_unicode_normalisation_stays_grounded():
    """Regression: NFKC rewrote characters and the blocks were dropped.

    Normalization turns the "fi" ligature into two letters and a superscript
    two into a digit. Those tokens were not in the raw source, so the invariant
    fired and the Conductor discarded legitimate text - common in PDFs.
    """
    for source, expected in [
        ("The \ufb01rst \ufb02ight was \ufb01ne.", "The first flight was fine."),
        ("Sample\u00b2 here", "Sample2 here"),
    ]:
        assert editor.to_speech(_block([source])).text == expected


def test_fullwidth_text_stays_grounded():
    text = editor.to_speech(_block(["\uff26\uff55\uff4c\uff4c\uff57\uff49\uff44\uff54\uff48"])).text
    assert text == "Fullwidth"


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


def test_degenerate_blocks_do_not_crash():
    for text in ["", "   ", "\u200b"]:
        assert editor.to_speech(_block([text])) is None
    assert editor.to_speech(_block(["x"])).text == "x"


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
