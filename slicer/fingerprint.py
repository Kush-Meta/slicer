"""Recognising text we have already read.

A slice is usually taller than the viewport, so a reading is capture, scroll,
re-capture, resume. The resume is where it goes wrong: read a paragraph twice
and the listener notices immediately; skip one and they may never know it
existed. Both are worse than slightly imperfect ordering.

Blocks are fingerprinted as sets of character 5-grams over their normalized
tokens. Character grams rather than word grams because recognition errors are
guaranteed: a misread word destroys three word-trigrams but only a handful of
character grams. Measured on this corpus, a paragraph with three OCR errors
scores 0.84 against its clean self under character 5-grams and 0.50 under word
trigrams, while two genuinely different paragraphs score 0.04 and a sticky
header against body text scores 0.13. The threshold sits in that gap.

Fingerprints are taken per *line*, not per block. Block boundaries are not
stable across captures: paragraph grouping depends on what else is visible, so
scrolling can merge two blocks into one or split one into two, and block-level
fingerprints then fail to match text that was demonstrably already read. A line
is the same line regardless of what surrounds it.

After a scroll the new lines are matched against everything already spoken, and
reading resumes after the last block whose lines are mostly familiar. Overlap is therefore handled for free, which is why
scrolling deliberately moves less than a full viewport - there must always be
something to align on.

The same fingerprints give the watchdog. If almost nothing in the new capture
matches what came before, the content changed underneath us - a navigation, a
modal, a log that scrolled itself - and the correct response is to stop and say
so rather than read something unrelated in a confident voice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .blocks import Block, tokenize

SHINGLE = 5
# Two blocks are the same text above this similarity. Measured margins on the
# test corpus: true matches with OCR damage score 0.84 and above, unrelated
# text 0.13 and below.
SAME_BLOCK = 0.55
# A block counts as already read once this share of its lines has been.
BLOCK_SEEN = 0.6
# Below this share of familiar lines, the screen is showing something else.
CONTENT_CHANGED = 0.15


def shingles(text: str, n: int = SHINGLE) -> frozenset[str]:
    """Character n-grams over normalized tokens.

    Normalizing through the tokenizer first removes punctuation and case, so
    "Revenue: 4,318." and "revenue 4318" fingerprint alike - which is what we
    want, since recognition is inconsistent about exactly those.
    """
    normalized = " ".join(tokenize(text))
    if not normalized:
        return frozenset()
    if len(normalized) <= n:
        return frozenset({normalized})
    return frozenset(normalized[i:i + n] for i in range(len(normalized) - n + 1))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap, with containment for a block clipped by the viewport.

    A paragraph cut in half by the top of the screen shares all of its shingles
    with the full version but only half the union, so plain Jaccard would call
    it a different block. Containment catches that case.
    """
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    jaccard = intersection / len(a | b)
    containment = intersection / min(len(a), len(b))
    return max(jaccard, containment)


@dataclass
class ReadingMemory:
    """Every line spoken so far, so none is spoken twice or missed."""

    spoken: list[frozenset[str]] = field(default_factory=list)

    def remember(self, block: Block) -> None:
        for line in block.lines:
            fingerprint = shingles(line.text)
            if fingerprint:
                self.spoken.append(fingerprint)

    def has_seen_line(self, text: str, threshold: float = SAME_BLOCK) -> bool:
        fingerprint = shingles(text)
        if not fingerprint:
            return False
        return any(similarity(fingerprint, seen) >= threshold for seen in self.spoken)

    def seen_fraction(self, block: Block, threshold: float = SAME_BLOCK) -> float:
        if not block.lines:
            return 0.0
        seen = sum(1 for line in block.lines
                   if self.has_seen_line(line.text, threshold))
        return seen / len(block.lines)

    def has_seen(self, block: Block, threshold: float = SAME_BLOCK) -> bool:
        return self.seen_fraction(block, threshold) >= BLOCK_SEEN

    def resume_index(self, blocks: list[Block], threshold: float = SAME_BLOCK) -> int:
        """Index of the first block not yet read.

        Matched as an *ordered overlap*, not as set membership. Testing each
        block independently looks reasonable and is wrong: screens are full of
        near-identical lines - table rows, log lines, list items, repeated
        labels - and "Paragraph 1 with some words" scores above threshold
        against "Paragraph 3 with some words" because they differ by one
        character. Every block then looks already-read and the rest of the page
        is silently skipped.

        What continuity actually means is that the tail of what has been spoken
        reappears at the head of what is now on screen. So the longest such
        run is what is looked for, and duplicates cannot fake it unless they
        genuinely appear in the same sequence.
        """
        if not self.spoken or not blocks:
            return 0
        prints = [shingles(block.text) for block in blocks]
        for length in range(min(len(self.spoken), len(prints)), 0, -1):
            tail = self.spoken[-length:]
            if all(similarity(tail[i], prints[i]) >= threshold for i in range(length)):
                return length
        return 0

    def overlap(self, blocks: list[Block], threshold: float = SAME_BLOCK) -> float:
        """Share of these lines that have already been read."""
        lines = [line for block in blocks for line in block.lines]
        if not lines:
            return 0.0
        seen = sum(1 for line in lines if self.has_seen_line(line.text, threshold))
        return seen / len(lines)

    def content_changed(self, blocks: list[Block]) -> bool:
        """True when the new capture has almost nothing in common with the old.

        Only meaningful once something has been read; a first capture has
        nothing to overlap with by definition. Note this asks whether *any*
        familiar line remains, not whether most of the screen is familiar - a
        scroll is supposed to reveal mostly new content.
        """
        if not self.spoken or not blocks:
            return False
        return self.overlap(blocks) < CONTENT_CHANGED

    def __len__(self) -> int:
        return len(self.spoken)
