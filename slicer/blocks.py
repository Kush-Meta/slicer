"""The grounded data model.

One rule governs this module: every word Slicer will ever speak originates in a
TextLine produced by recognition, and carries the id of the line it came from.
Models may reorder, label, group and skip these objects. Nothing in the system
is permitted to invent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class BlockKind(str, Enum):
    BODY = "body"
    HEADING = "heading"
    LIST = "list"
    CODE = "code"
    TABLE_ROW = "table_row"
    CHROME = "chrome"       # navigation, toolbars, controls - not content
    CAPTION = "caption"
    UNKNOWN = "unknown"


# Kinds that are not read aloud unless the user asks for them explicitly.
SKIPPED_BY_DEFAULT = frozenset({BlockKind.CHROME})


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def union(self, other: "Box") -> "Box":
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Box(x, y, max(self.x2, other.x2) - x, max(self.y2, other.y2) - y)

    def translated(self, dx: int, dy: int) -> "Box":
        return Box(self.x + dx, self.y + dy, self.w, self.h)


@dataclass(frozen=True)
class TextLine:
    """One recognized line. The only origin of speakable text in the system."""

    id: str
    text: str
    confidence: float
    box: Box

    def tokens(self) -> list[str]:
        return _tokenize(self.text)


@dataclass
class Block:
    """A group of lines that read as one unit."""

    id: str
    lines: list[TextLine]
    kind: BlockKind = BlockKind.UNKNOWN
    # Why this block was classified as it was - shown in the skip log so a
    # user can audit what Slicer decided not to read.
    reason: str = ""

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)

    @property
    def healed_text(self) -> str:
        """Lines joined, with words that a line break split back in one piece.

        This is a declared, deterministic transformation of what is on screen -
        no judgement, no model - so its output counts as grounded. Everything
        downstream reads text from here, and the invariant is checked against
        the tokens of this string.
        """
        out = ""
        for index, line in enumerate(self.lines):
            text = line.text.strip()
            if index == 0:
                out = text
                continue
            # "inter-" + "national" is one word; "well-" + "Known" is not.
            if out.endswith("-") and text[:1].islower():
                out = out[:-1] + text
            else:
                out = out + " " + text
        return out

    @property
    def box(self) -> Box:
        box = self.lines[0].box
        for line in self.lines[1:]:
            box = box.union(line.box)
        return box

    @property
    def confidence(self) -> float:
        """Worst line wins: a block is only as trustworthy as its weakest line."""
        return min((line.confidence for line in self.lines), default=0.0)

    @property
    def skipped(self) -> bool:
        return self.kind in SKIPPED_BY_DEFAULT

    def token_multiset(self) -> dict[str, int]:
        """Every token available to the Editor, with counts.

        The Editor's output is checked against this. A content word that is not
        in here did not come from the screen.
        """
        # A token's allowance is the most times it legitimately appears under
        # either reading of the block - the healed join, or the raw lines. Taking
        # the maximum rather than the sum admits words that a line break split
        # in half without doubling every other word's budget, so the check still
        # catches duplication.
        healed: dict[str, int] = {}
        for token in _tokenize(self.healed_text):
            healed[token] = healed.get(token, 0) + 1
        raw: dict[str, int] = {}
        for line in self.lines:
            for token in line.tokens():
                raw[token] = raw.get(token, 0) + 1
        return {token: max(healed.get(token, 0), raw.get(token, 0))
                for token in set(healed) | set(raw)}


@dataclass
class Slice:
    """A captured region, recognized and organized into reading order."""

    blocks: list[Block]
    width: int
    height: int
    # Screen-space origin of the capture, so block boxes can be mapped back
    # to the display for highlighting.
    origin_x: int = 0
    origin_y: int = 0
    scale: float = 1.0
    degraded: list[str] = field(default_factory=list)

    def readable(self) -> list[Block]:
        return [b for b in self.blocks if not b.skipped]

    def skipped(self) -> list[Block]:
        return [b for b in self.blocks if b.skipped]

    def __iter__(self) -> Iterator[Block]:
        return iter(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric runs. Punctuation is not content."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def tokenize(text: str) -> list[str]:
    return _tokenize(text)
