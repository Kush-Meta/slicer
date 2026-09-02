"""Recognized lines to ordered, typed blocks.

This is the part that makes Slicer different from a tool that reads
top-to-bottom. Vision hands back lines in no particular order; sorting them by
y then x reads a two-column page straight across, which is the single most
recognizable way a reader like this fails.

The method is a recursive XY-cut. Look for a vertical gutter that no line
crosses; if one exists the region is columns, so recurse left then right.
Otherwise look for a horizontal gap and recurse top then bottom. It is
deterministic, needs no model, and cannot hallucinate because it only ever
permutes the lines it was given - which matters more here than marginal
accuracy, since a reader that orders the same screen differently twice loses
trust faster than one that is consistently a little wrong.
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
    # Consecutive lines closer than this join into one paragraph.
    paragraph_gap_mult: float = 1.7
    # A column narrower than this, full of short lines, is navigation.
    chrome_max_width_frac: float = 0.22
    chrome_max_median_words: float = 3.0
    chrome_min_lines: int = 3
    # ...and only beside a sibling column this many times wider.
    chrome_min_sibling_ratio: float = 2.0
    # A line taller than the page median by this much is a heading.
    heading_height_mult: float = 1.28
    heading_max_words: int = 14
    # Code detection scores several weak signals rather than trusting one.
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


# --------------------------------------------------------------------------
# partitioning


def partition(lines: list[TextLine], cfg: LayoutConfig | None = None,
              parent_axis: str | None = None) -> Region:
    cfg = cfg or LayoutConfig()
    box = _bounds(lines)
    region = Region(box=box, parent_axis=parent_axis)

    if len(lines) <= 1:
        region.lines = list(lines)
        return region

    median_h = statistics.median(line.box.h for line in lines) or 1

    gutter = _widest_gap(
        [(line.box.x, line.box.x2) for line in lines],
        minimum=max(cfg.gutter_min_width_frac * max(box.w, 1),
                    cfg.gutter_min_height_mult * median_h),
    )
    if gutter is not None:
        cut = (gutter[0] + gutter[1]) / 2
        left = [ln for ln in lines if ln.box.cx < cut]
        right = [ln for ln in lines if ln.box.cx >= cut]
        if left and right:
            region.axis = "v"
            region.children = [partition(left, cfg, "v"), partition(right, cfg, "v")]
            return region

    band = _widest_gap(
        [(line.box.y, line.box.y2) for line in lines],
        minimum=cfg.row_gap_mult * median_h,
    )
    if band is not None:
        cut = (band[0] + band[1]) / 2
        top = [ln for ln in lines if ln.box.cy < cut]
        bottom = [ln for ln in lines if ln.box.cy >= cut]
        if top and bottom:
            region.axis = "h"
            region.children = [partition(top, cfg, "h"), partition(bottom, cfg, "h")]
            return region

    region.lines = sorted(lines, key=lambda ln: (ln.box.y, ln.box.x))
    return region


def _widest_gap(intervals: list[tuple[int, int]], minimum: float) -> tuple[int, int] | None:
    """Widest gap between merged intervals, if it clears the minimum.

    Only gaps strictly between occupied ranges count, so the ragged edges of a
    text column never look like a column separation.
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
        width = gap_end - gap_start
        if width > best_width:
            best, best_width = (gap_start, gap_end), width
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


def build_slice(result: OcrResult, cfg: LayoutConfig | None = None,
                origin_x: int = 0, origin_y: int = 0, scale: float = 1.0) -> Slice:
    """Ordered, grouped, classified blocks for one capture."""
    cfg = cfg or LayoutConfig()
    if not result.lines:
        return Slice(blocks=[], width=result.width, height=result.height,
                     origin_x=origin_x, origin_y=origin_y, scale=scale)

    tree = partition(result.lines, cfg)
    median_h = statistics.median(line.box.h for line in result.lines) or 1

    blocks: list[Block] = []
    counter = 0
    for leaf, chrome, reason in _walk_leaves(tree, result.width, cfg):
        for group in _group_lines(leaf.lines, median_h, cfg):
            counter += 1
            block = Block(id=f"b{counter}", lines=group)
            if chrome:
                block.kind = BlockKind.CHROME
                block.reason = reason
            else:
                block.kind, block.reason = _classify(group, median_h, cfg)
            blocks.append(block)

    degraded: list[str] = []
    if blocks and all(block.skipped for block in blocks):
        for block in blocks:
            block.kind = BlockKind.BODY
            block.reason = ""
        degraded.append(
            "chrome detection would have skipped every block, so it was ignored"
        )

    return Slice(blocks=blocks, width=result.width, height=result.height,
                 origin_x=origin_x, origin_y=origin_y, scale=scale,
                 degraded=degraded)


def _walk_leaves(region: Region, page_width: int, cfg: LayoutConfig):
    """Yield (leaf, is_chrome, reason) in reading order."""
    if region.is_leaf:
        yield region, False, ""
        return

    # Chrome is a relative judgement, made among siblings: a narrow column is
    # navigation only when there is a substantially wider column beside it to
    # be navigation *for*. Two columns of equal width are a two-column page,
    # and skipping either would be silent data loss.
    widest = max((child.box.w for child in region.children), default=0)
    for child in region.children:
        chrome, reason = ("", "") if region.axis != "v" else _is_chrome(
            child, widest, page_width, cfg
        )
        if chrome:
            for leaf in _leaves(child):
                yield leaf, True, reason
        else:
            yield from _walk_leaves(child, page_width, cfg)


def _leaves(region: Region):
    if region.is_leaf:
        yield region
        return
    for child in region.children:
        yield from _leaves(child)


def _is_chrome(region: Region, widest_sibling: int, page_width: int,
               cfg: LayoutConfig) -> tuple[bool, str]:
    """A narrow column of short lines, set beside a much wider one, is navigation."""
    lines = region.all_lines()
    if len(lines) < cfg.chrome_min_lines:
        return False, ""
    if region.box.w > cfg.chrome_max_width_frac * page_width:
        return False, ""
    # The load-bearing test: something wider must exist to be navigation for.
    if region.box.w * cfg.chrome_min_sibling_ratio > widest_sibling:
        return False, ""
    median_words = statistics.median(len(ln.text.split()) for ln in lines)
    if median_words > cfg.chrome_max_median_words:
        return False, ""
    return True, (
        f"narrow column ({region.box.w}px, {region.box.w / max(page_width, 1):.0%} "
        f"of width) beside one {widest_sibling}px wide, median "
        f"{median_words:.0f} words per line"
    )


def _group_lines(lines: list[TextLine], median_h: float,
                 cfg: LayoutConfig) -> list[list[TextLine]]:
    """Join consecutive lines into paragraphs on vertical proximity."""
    if not lines:
        return []
    groups: list[list[TextLine]] = [[lines[0]]]
    for line in lines[1:]:
        previous = groups[-1][-1]
        gap = line.box.y - previous.box.y2
        if gap <= cfg.paragraph_gap_mult * median_h:
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
    would trip a keyword test, a sentence full of commas would trip a symbol
    test - so code is only declared when several agree.
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
    # Indentation shows up as staggered left edges rather than leading spaces,
    # because recognition strips whitespace.
    if len(group) > 1 and len({line.box.x // 8 for line in group}) > 1:
        signals.append("staggered indentation")

    if len(signals) >= cfg.code_min_signals:
        return BlockKind.CODE, ", ".join(signals)
    return None, ""
