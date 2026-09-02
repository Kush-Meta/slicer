"""Blocks to speakable text, and the check that keeps it honest.

The invariant: every content word Slicer speaks must trace back to a token that
recognition actually found on the screen. assert_grounded enforces it, and it
is called on every block on every reading - not in tests, in the live path.

Today nothing here could violate it, because normalization is rule-based. That
is exactly why it goes in now: when a model joins this stage the check is
already load-bearing, and the first ungrounded word raises instead of being
spoken in a confident voice.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .blocks import Block, BlockKind, tokenize


class UngroundedSpeech(RuntimeError):
    """Text was about to be spoken that did not come from the screen."""


# Words normalization may introduce that were never on screen: the scaffolding
# of speech rather than content. Deliberately short - every addition widens the
# hole the invariant exists to close.
SCAFFOLD = frozenset({
    "dot", "slash", "colon", "link", "to", "at", "percent", "dollars",
    "and", "the", "of", "code", "block", "in", "line", "lines", "table",
    "row", "column", "heading", "list", "item", "skipped", "unclear",
})

_URL_RE = re.compile(r"\bhttps?://[^\s]+|\bwww\.[^\s]+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_EMOJI_RUN_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]+"
)


@dataclass
class Utterance:
    """One thing to say, in two parts that are never mixed.

    `prefix` is Slicer speaking in its own voice - an announcement, a count, a
    hedge. It may contain words that were never on screen, which is exactly why
    it is a separate field: the listener can tell narration from content, and
    the grounding check applies only to `text`.

    `text` is the screen, and every word in it is grounded.
    """

    block_id: str
    text: str
    kind: BlockKind
    confidence: float
    prefix: str = ""
    # Set when the text was altered in a way the listener should know about.
    note: str = ""

    @property
    def spoken(self) -> str:
        return f"{self.prefix} {self.text}".strip() if self.prefix else self.text


def to_speech(block: Block, *, min_confidence: float = 0.0) -> Utterance | None:
    """Render one block as something worth hearing, or None if it should not be read."""
    if block.skipped:
        return None

    prefix, note = "", ""
    if block.kind == BlockKind.CODE:
        text, prefix, note = _code_summary(block)
    else:
        text = _normalize(block.healed_text)

    if not text.strip():
        return None

    if block.confidence < min_confidence:
        # Hedging is narration, so it goes in the prefix rather than being
        # woven into words the listener will take as screen content.
        prefix = (prefix + " unclear,").strip()
        note = note or f"low confidence ({block.confidence:.2f})"

    # The invariant applies to content only. Narration is Slicer's own voice.
    assert_grounded(text, block)
    return Utterance(block_id=block.id, text=text, kind=block.kind,
                     confidence=block.confidence, prefix=prefix, note=note)


def assert_grounded(spoken: str, block: Block,
                    extra_allowed: frozenset[str] = frozenset()) -> None:
    """Raise unless every content word in `spoken` came from this block.

    Counts are checked, not just membership, so normalization cannot quietly
    duplicate content either.
    """
    available = block.token_multiset()
    for token in tokenize(spoken):
        if token in SCAFFOLD or token in extra_allowed:
            continue
        if available.get(token, 0) <= 0:
            raise UngroundedSpeech(
                f"block {block.id}: the word {token!r} is not on the screen. "
                f"Source text: {block.text[:120]!r}"
            )
        available[token] -= 1


# --------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _URL_RE.sub(_speak_url, text)
    text = _EMOJI_RUN_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _speak_url(match: re.Match[str]) -> str:
    """A URL read character by character is unbearable; read the host only."""
    url = match.group(0)
    host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/")[0]
    host = host.removeprefix("www.")
    return "link to " + host.replace(".", " dot ").replace("-", " ")


def _code_summary(block: Block) -> tuple[str, str, str]:
    """Announce code rather than reading its punctuation aloud.

    Returns (content, narration, note). The line count is generated, so it
    belongs to narration; the preview is real screen text and stays content.
    """
    count = len(block.lines)
    preview = " ".join(block.lines[0].text.strip().split()[:6])
    return preview, f"code block, {count} lines, beginning", "code read as a summary"
