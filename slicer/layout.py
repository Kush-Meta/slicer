"""Recognized lines to ordered, typed blocks.

This is the part that makes Slicer different from a tool that reads
top-to-bottom. Vision hands back lines in no particular order; sorting them by
y then x reads a two-column page straight across, which is the most
recognizable way a reader like this fails.

The method is a recursive XY-cut. Look for a vertical gutter that no line
crosses; if one exists the region is columns, so recurse left then right.
Otherwise look for a horizontal gap and recurse top then bottom. It is
deterministic and cannot hallucinate, because it only ever permutes the lines
it was given - which matters more here than marginal accuracy, since a reader
that orders the same screen differently twice loses trust faster than one that
is consistently a little wrong.

The cut produces a binary tree, which is then flattened so a chain of splits on
the same axis becomes one n-ary group. Judging a column - is it navigation? are
these cells of a grid? - needs all its siblings visible at once. Comparing
against a binary sibling that was really a nested subtree silently deleted the
third column of a three-column page.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .blocks import Block, BlockKind, Box, Slice, TextLine
from .ocr import OcrResult


@dataclass
class LayoutConfig:
    # A gutter must be at least this fraction of the region width, and this
    # multiple of a line's height, before it counts as a column separation.
    gutter_min_width_frac: float = 0.03
    gutter_min_height_mult: float = 1.5
    # A horizontal gap this much taller than a line ends a band.
    row_gap_mult: float = 0.6
    # How much wider a gutter must become after a horizontal cut before that
    # cut is judged the more significant division. Swept against the whole
    # suite: 1.5 fires on ordinary prose columns and breaks them, 2.0 through
    # 3.0 all pass everything, so 2.5 sits in the middle of the safe range.
    lookahead_gain: float = 2.5
    # Consecutive lines closer than this join into one paragraph.
    paragraph_gap_mult: float = 1.7

    # -- navigation ("chrome") detection.
    # A sidebar is narrow, sits at a margin, holds short labels, and stands
    # beside columns of ordinary width. All four must hold.
    chrome_max_width_frac: float = 0.22
    chrome_max_median_words: float = 3.0
    chrome_min_lines: int = 3
    chrome_min_sibling_ratio: float = 2.0
    # If detection would silence more than this share of the words on screen it
    # has misfired, and reading too much beats silently reading too little.
    chrome_max_skip_frac: float = 0.4

    # -- table detection.
    # A grid read column-wise is noise: "4,318 2,901 5,144" means nothing.
    table_row_align_mult: float = 0.6
    table_min_columns: int = 2
    table_min_rows: int = 2
    # Cells are short; running prose is not. The median is not enough - a
    # sidebar of one-word links beside a paragraph has a median of one - so the
    # longest line in the group is what actually has to be short.
    table_max_median_words: float = 3.0
    table_max_line_words: int = 4
    # Every row of a grid is complete, except possibly the last. Allowing a
    # ragged row anywhere let a heading above a table become "row 1 of 4", and
    # allowing many let a nav column and a body column be read as pairs.
    table_allow_ragged_rows: int = 1

    # A line taller than the page median by this much is a heading.
    heading_height_mult: float = 1.28
    heading_max_words: int = 14
    # Code is declared only when several weak signals agree.
    code_symbol_ratio: float = 0.18
    code_min_signals: int = 3


@dataclass
class Region:
    """A node in the XY-cut tree. Leaves hold lines; branches hold children."""

    box: Box
    lines: list[TextLine] = field(default_factory=list)
    children: list["Region"] = field(default_factory=list)
    axis: str | None = None          # "v" if split into columns, "h" into bands
    parent_axis: str | None = None

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def all_lines(self) -> list[TextLine]:
        if self.is_leaf:
            return list(self.lines)
        out: list[TextLine] = []
        for child in self.children:
            out.extend(child.all_lines())
        return out

    def leaves(self) -> list["Region"]:
        if self.is_leaf:
            return [self]
        out: list[Region] = []
        for child in self.children:
            out.extend(child.leaves())
        return out


# --------------------------------------------------------------------------
# partitioning


def partition(lines: list[TextLine], cfg: LayoutConfig | None = None,
              parent_axis: str | None = None) -> Region:
    cfg = cfg or LayoutConfig()
    return flatten(_partition(lines, cfg, parent_axis))


def _partition(lines: list[TextLine], cfg: LayoutConfig,
               parent_axis: str | None) -> Region:
    box = _bounds(lines)
    region = Region(box=box, parent_axis=parent_axis)

    if len(lines) <= 1:
        region.lines = list(lines)
        return region

    median_h = statistics.median(line.box.h for line in lines) or 1
    gutter_floor = max(cfg.gutter_min_width_frac * max(box.w, 1),
                       cfg.gutter_min_height_mult * median_h)

    gutter = _widest_gap([(line.box.x, line.box.x2) for line in lines],
                         minimum=gutter_floor)
    band = _widest_gap([(line.box.y, line.box.y2) for line in lines],
                       minimum=cfg.row_gap_mult * median_h)

    # A heading above columns merges with whichever column it starts over, and
    # that hides the real gutter: the cut lands between the heading's right
    # edge and the next column instead of between the columns themselves. One
    # step of lookahead settles it - if cutting horizontally first exposes a
    # materially wider gutter underneath, the horizontal division was the more
    # significant one.
    if gutter is not None and band is not None:
        cut = (band[0] + band[1]) / 2
        below = [ln for ln in lines if ln.box.cy >= cut]
        if len(below) > 1:
            revealed = _widest_gap([(ln.box.x, ln.box.x2) for ln in below],
                                   minimum=gutter_floor)
            here = gutter[1] - gutter[0]
            if revealed is not None and (revealed[1] - revealed[0]) > here * cfg.lookahead_gain:
                gutter = None            # take the horizontal cut instead

    if gutter is not None:
        cut = (gutter[0] + gutter[1]) / 2
        left = [ln for ln in lines if ln.box.cx < cut]
        right = [ln for ln in lines if ln.box.cx >= cut]
        if left and right:
            region.axis = "v"
            region.children = [_partition(left, cfg, "v"), _partition(right, cfg, "v")]
            return region

    if band is not None:
        cut = (band[0] + band[1]) / 2
        top = [ln for ln in lines if ln.box.cy < cut]
        bottom = [ln for ln in lines if ln.box.cy >= cut]
        if top and bottom:
            region.axis = "h"
            region.children = [_partition(top, cfg, "h"), _partition(bottom, cfg, "h")]
            return region

    region.lines = sorted(lines, key=lambda ln: (ln.box.y, ln.box.x))
    return region


def flatten(region: Region) -> Region:
    """Collapse chains of same-axis splits into one n-ary group.

    Three columns come out of the binary cut as left(left(A,B),C). Flattened
    they are siblings (A,B,C), which is the only form in which "is this column
    unusually narrow?" has a meaningful answer.
    """
    if region.is_leaf:
        return region
    children: list[Region] = []
    for child in region.children:
        child = flatten(child)
        if not child.is_leaf and child.axis == region.axis:
            children.extend(child.children)
        else:
            children.append(child)
    for child in children:
        child.parent_axis = region.axis
    region.children = children
    return region


def _widest_gap(intervals: list[tuple[int, int]], minimum: float) -> tuple[int, int] | None:
    """Widest gap between merged intervals, if it clears the minimum.

    Only gaps strictly between occupied ranges count, so the ragged right edge
    of a text column never looks like a column separation.
    """
    if len(intervals) < 2:
        return None
    ordered = sorted(intervals)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    best: tuple[int, int] | None = None
    best_width = minimum
    for i in range(len(merged) - 1):
        gap_start, gap_end = merged[i][1], merged[i + 1][0]
        if gap_end - gap_start > best_width:
            best, best_width = (gap_start, gap_end), gap_end - gap_start
    return best


def _bounds(lines: list[TextLine]) -> Box:
    if not lines:
        return Box(0, 0, 0, 0)
    box = lines[0].box
    for line in lines[1:]:
        box = box.union(line.box)
    return box


# --------------------------------------------------------------------------
# blocks


class _Counter:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"b{self.n}"


def build_slice(result: OcrResult, cfg: LayoutConfig | None = None,
                origin_x: int = 0, origin_y: int = 0, scale: float = 1.0) -> Slice:
    """Ordered, grouped, classified blocks for one capture."""
    cfg = cfg or LayoutConfig()
    if not result.lines:
        return Slice(blocks=[], width=result.width, height=result.height,
                     origin_x=origin_x, origin_y=origin_y, scale=scale)

    tree = partition(result.lines, cfg)
    median_h = statistics.median(line.box.h for line in result.lines) or 1

    counter = _Counter()
    blocks = _emit(tree, result.width, median_h, cfg, counter)
    degraded = _guard_against_over_skipping(blocks, cfg)

    return Slice(blocks=blocks, width=result.width, height=result.height,
                 origin_x=origin_x, origin_y=origin_y, scale=scale,
                 degraded=degraded)


def _emit(region: Region, page_width: int, median_h: float,
          cfg: LayoutConfig, counter: _Counter) -> list[Block]:
    """Walk the tree in reading order, producing blocks."""
    if region.is_leaf:
        return [_make_block(group, median_h, cfg, counter)
                for group in _group_lines(region.lines, median_h, cfg)]

    if region.axis == "v":
        rows = _as_table(region, median_h, cfg)
        if rows is not None:
            return _emit_table(rows, counter)

        blocks: list[Block] = []
        for index, child in enumerate(region.children):
            chrome, reason = _is_chrome(child, region.children, index, page_width, cfg)
            child_blocks = _emit(child, page_width, median_h, cfg, counter)
            if chrome:
                for block in child_blocks:
                    block.kind = BlockKind.CHROME
                    block.reason = reason
            blocks.extend(child_blocks)
        return blocks

    blocks = []
    for child in region.children:
        blocks.extend(_emit(child, page_width, median_h, cfg, counter))
    return blocks


def _make_block(group: list[TextLine], median_h: float, cfg: LayoutConfig,
                counter: _Counter) -> Block:
    block = Block(id=counter.next(), lines=group)
    block.kind, block.reason = _classify(group, median_h, cfg)
    return block


# -- tables ----------------------------------------------------------------


def _as_table(region: Region, median_h: float,
              cfg: LayoutConfig) -> list[list[TextLine | None]] | None:
    """Return rows of cells if this vertical group is a grid, else None.

    Rows are clustered from line positions rather than from the shape of the
    recursive cut. An earlier version required every column to hold the same
    number of leaves; that held for synthetic fixtures and broke on the first
    real HTML table, where widest-gap-first recursion left one column's header
    and first data cell merged. The table then fell back to column-wise
    reading - "Region North South West, Revenue 4,318..." - which tells a
    listener nothing.

    The discriminator against a two-column article is cell length: table cells
    are a word or two, prose lines are many. Without it, running text in two
    columns looks exactly like a two-column grid.
    """
    columns = region.children
    if len(columns) < cfg.table_min_columns:
        return None

    column_of: dict[int, int] = {}
    for index, column in enumerate(columns):
        for line in column.all_lines():
            column_of[id(line)] = index

    lines = region.all_lines()
    if not lines:
        return None
    words_per_line = [len(line.text.split()) for line in lines]
    if statistics.median(words_per_line) > cfg.table_max_median_words:
        return None                      # running prose, not cells
    if max(words_per_line) > cfg.table_max_line_words:
        # One long line means at least one column holds prose. A navigation
        # column beside a paragraph passes the median test and must not pass
        # this one, or the two get read as interleaved pairs.
        return None

    tolerance = max(cfg.table_row_align_mult * median_h, 4)
    clusters: list[list[TextLine]] = []
    centres: list[float] = []
    for line in sorted(lines, key=lambda ln: ln.box.cy):
        if clusters and abs(line.box.cy - centres[-1]) <= tolerance:
            clusters[-1].append(line)
            centres[-1] = statistics.mean([ln.box.cy for ln in clusters[-1]])
        else:
            clusters.append([line])
            centres.append(line.box.cy)

    if len(clusters) < cfg.table_min_rows:
        return None

    grid: list[dict[int, TextLine]] = []
    for cluster in clusters:
        row: dict[int, TextLine] = {}
        for line in cluster:
            index = column_of.get(id(line))
            if index is None or index in row:
                return None              # two lines in one cell: not a simple grid
            row[index] = line
        grid.append(row)

    # Only a trailing row may be incomplete. A ragged row at the start or in
    # the middle is evidence this is not a grid at all - most often it is a
    # heading sitting above a table, which was otherwise absorbed and
    # announced as "row 1 of 4".
    for row in grid[:-1]:
        if len(row) != len(columns):
            return None
    if len(grid[-1]) != len(columns) and len(grid) <= cfg.table_min_rows:
        return None

    return [[row.get(index) for index in range(len(columns))] for row in grid]


def _emit_table(rows: list[list[TextLine | None]], counter: _Counter) -> list[Block]:
    """One block per row, cells left to right."""
    blocks: list[Block] = []
    width = len(rows[0]) if rows else 0
    for number, row in enumerate(rows, 1):
        lines = [cell for cell in row if cell is not None]
        if not lines:
            continue
        blocks.append(Block(id=counter.next(), lines=lines, kind=BlockKind.TABLE_ROW,
                            reason=f"row {number} of a {width}-column grid",
                            index_in_group=number, group_size=len(rows)))
    return blocks


# -- navigation ------------------------------------------------------------


def _is_chrome(region: Region, siblings: list[Region], index: int,
               page_width: int, cfg: LayoutConfig) -> tuple[bool, str]:
    """A narrow column of short labels at a margin, beside ordinary content.

    The comparison is against the median *line width* elsewhere, not against a
    sibling's bounding box. That distinction has now caused the same bug twice:
    a sibling is often a subtree spanning several columns, so its box is far
    wider than any real column, and any genuinely narrow column measured
    against it looks like a sidebar and is silently deleted. Text extent is a
    property of the content; a bounding box is an artefact of how the cut
    happened to recurse.
    """
    if index not in (0, len(siblings) - 1):
        return False, ""                 # navigation sits at a margin
    lines = region.all_lines()
    if len(lines) < cfg.chrome_min_lines:
        return False, ""
    if region.box.w > cfg.chrome_max_width_frac * page_width:
        return False, ""

    others: list[int] = []
    for position, sibling in enumerate(siblings):
        if position != index:
            others.extend(line.box.w for line in sibling.all_lines())
    if not others:
        return False, ""

    mine = statistics.median(line.box.w for line in lines)
    reference = statistics.median(others)
    if mine * cfg.chrome_min_sibling_ratio > reference:
        return False, ""

    median_words = statistics.median(len(ln.text.split()) for ln in lines)
    if median_words > cfg.chrome_max_median_words:
        return False, ""
    if any(ln.text.rstrip().endswith((".", "!", "?")) for ln in lines):
        return False, ""                 # navigation labels are not sentences

    return True, (
        f"narrow edge column: lines average {mine:.0f}px against {reference:.0f}px "
        f"elsewhere, median {median_words:.0f} words per line, no sentence punctuation"
    )


def _guard_against_over_skipping(blocks: list[Block], cfg: LayoutConfig) -> list[str]:
    """Refuse to stay quiet about most of the screen.

    Measured in words, not blocks. A sidebar of five one-word links is five
    blocks beside a single paragraph block: by block count that looks like 83%
    of the page suppressed, when it is barely a quarter of the words.
    """
    if not blocks:
        return []
    skipped = [b for b in blocks if b.skipped]
    if not skipped:
        return []
    skipped_words = sum(len(b.text.split()) for b in skipped)
    total_words = sum(len(b.text.split()) for b in blocks)
    if not total_words:
        return []
    fraction = skipped_words / total_words
    if fraction <= cfg.chrome_max_skip_frac:
        return []
    for block in skipped:
        block.kind = BlockKind.BODY
        block.reason = ""
    return [f"navigation detection would have skipped {fraction:.0%} of the "
            f"words on screen, so it was ignored"]


# -- grouping and classification -------------------------------------------


def _group_lines(lines: list[TextLine], median_h: float,
                 cfg: LayoutConfig) -> list[list[TextLine]]:
    """Join consecutive lines into paragraphs on vertical proximity."""
    if not lines:
        return []
    groups: list[list[TextLine]] = [[lines[0]]]
    for line in lines[1:]:
        previous = groups[-1][-1]
        if line.box.y - previous.box.y2 <= cfg.paragraph_gap_mult * median_h:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


_CODE_KEYWORDS = re.compile(
    r"\b(def|class|function|return|import|from|elif|else|for|while|var|let|const|"
    r"public|private|static|void|struct|impl|fn|async|await|try|except|catch|throw)\b"
)
_CALL_PATTERN = re.compile(r"[A-Za-z_][\w.]*\s*\(")
_LINE_TERMINATOR = re.compile(r"[:{};,)]\s*$")


def _classify(group: list[TextLine], median_h: float,
              cfg: LayoutConfig) -> tuple[BlockKind, str]:
    text = " ".join(line.text for line in group)
    words = text.split()

    kind, reason = _code_score(group, text, cfg)
    if kind is not None:
        return kind, reason

    if len(group) == 1:
        height = group[0].box.h
        if height >= cfg.heading_height_mult * median_h and len(words) <= cfg.heading_max_words:
            return BlockKind.HEADING, f"line {height / median_h:.1f}x median height"

    stripped = text.lstrip()
    if stripped[:2] in {"- ", "* "} or (stripped[:1].isdigit() and stripped[1:3] in {". ", ") "}):
        return BlockKind.LIST, "leading list marker"

    return BlockKind.BODY, ""


def _code_score(group: list[TextLine], text: str,
                cfg: LayoutConfig) -> tuple[BlockKind | None, str]:
    """Score several weak signals instead of trusting punctuation density alone.

    Any single signal misfires on ordinary prose - "use the function() here"
    trips a keyword test, a sentence full of commas trips a symbol test - so
    code is only declared when several agree.
    """
    if not text:
        return None, ""

    signals: list[str] = []
    symbols = sum(1 for ch in text if not ch.isalnum() and not ch.isspace())
    ratio = symbols / len(text)
    if ratio > cfg.code_symbol_ratio:
        signals.append(f"{ratio:.0%} symbols")
    if _CODE_KEYWORDS.search(text):
        signals.append("code keyword")
    if _CALL_PATTERN.search(text):
        signals.append("call syntax")
    if any(_LINE_TERMINATOR.search(line.text) for line in group):
        signals.append("statement terminator")
    # Indentation shows as staggered left edges; recognition strips whitespace.
    if len(group) > 1 and len({line.box.x // 8 for line in group}) > 1:
        signals.append("staggered indentation")

    if len(signals) >= cfg.code_min_signals:
        return BlockKind.CODE, ", ".join(signals)
    return None, ""
