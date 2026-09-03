"""Moving through a reading by structure, not just forwards.

A linear reading is a recording. A screen reader is something you *navigate*:
jump to the next heading, step back a paragraph, skim the table rows, spell the
word you did not catch. Without that, a blind user has no way to skim, and
skimming is most of how anyone reads.

The command letters are deliberately not invented. H for next heading and T for
next table are the same in NVDA, JAWS and VoiceOver's QuickNav, with shift
reversing direction, and users have those in their fingers already. Matching
them costs nothing; inventing new ones costs a user their muscle memory.

Two rules that matter more here than they would for a sighted tool:

  * Every move speaks immediately, and interrupts whatever was being said. A
    reader who has to wait out a paragraph cannot skim.
  * Every move gives feedback, including failure. Silence after pressing H is
    indistinguishable from a broken program; "no more headings" is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .blocks import Block, BlockKind, Slice
from .editor import Utterance, Verbosity, spell, to_speech


class Command(str, Enum):
    NEXT = "next"
    PREVIOUS = "previous"
    NEXT_HEADING = "next heading"
    PREVIOUS_HEADING = "previous heading"
    NEXT_TABLE = "next table row"
    PREVIOUS_TABLE = "previous table row"
    NEXT_CODE = "next code block"
    PREVIOUS_CODE = "previous code block"
    NEXT_LIST = "next list item"
    PREVIOUS_LIST = "previous list item"
    FIRST = "first"
    LAST = "last"
    REPEAT = "repeat"
    SPELL = "spell"
    WHERE = "where"
    SAY_ALL = "say all"
    STOP = "stop"
    QUIT = "quit"


# Single letters, matching the conventions users already have. Shift reverses.
KEYS: dict[str, Command] = {
    "n": Command.NEXT, "\x1b[B": Command.NEXT, " ": Command.NEXT,
    "p": Command.PREVIOUS, "\x1b[A": Command.PREVIOUS,
    "h": Command.NEXT_HEADING, "H": Command.PREVIOUS_HEADING,
    "t": Command.NEXT_TABLE, "T": Command.PREVIOUS_TABLE,
    "c": Command.NEXT_CODE, "C": Command.PREVIOUS_CODE,
    "l": Command.NEXT_LIST, "L": Command.PREVIOUS_LIST,
    ",": Command.FIRST, ".": Command.LAST,
    "r": Command.REPEAT, "s": Command.SPELL, "w": Command.WHERE,
    "a": Command.SAY_ALL,
    "\x1b": Command.STOP, "q": Command.QUIT, "\x03": Command.QUIT,
}

KIND_FOR: dict[Command, BlockKind] = {
    Command.NEXT_HEADING: BlockKind.HEADING,
    Command.PREVIOUS_HEADING: BlockKind.HEADING,
    Command.NEXT_TABLE: BlockKind.TABLE_ROW,
    Command.PREVIOUS_TABLE: BlockKind.TABLE_ROW,
    Command.NEXT_CODE: BlockKind.CODE,
    Command.PREVIOUS_CODE: BlockKind.CODE,
    Command.NEXT_LIST: BlockKind.LIST,
    Command.PREVIOUS_LIST: BlockKind.LIST,
}

BACKWARD = {
    Command.PREVIOUS, Command.PREVIOUS_HEADING, Command.PREVIOUS_TABLE,
    Command.PREVIOUS_CODE, Command.PREVIOUS_LIST,
}


@dataclass
class Cursor:
    """A position in a reading. Pure logic, so it can be tested exhaustively."""

    blocks: list[Block]
    index: int = 0

    @property
    def current(self) -> Block | None:
        if not self.blocks or not 0 <= self.index < len(self.blocks):
            return None
        return self.blocks[self.index]

    @property
    def position(self) -> str:
        if not self.blocks:
            return "nothing to read"
        return f"{self.index + 1} of {len(self.blocks)}"

    def move(self, delta: int) -> bool:
        """Step by one. False at the edge, so the caller can say so."""
        target = self.index + delta
        if not 0 <= target < len(self.blocks):
            return False
        self.index = target
        return True

    def seek_kind(self, kind: BlockKind, *, backward: bool = False) -> bool:
        """Jump to the next block of a kind. False if there is not one."""
        span = (range(self.index - 1, -1, -1) if backward
                else range(self.index + 1, len(self.blocks)))
        for candidate in span:
            if self.blocks[candidate].kind == kind:
                self.index = candidate
                return True
        return False

    def go(self, index: int) -> bool:
        if not 0 <= index < len(self.blocks):
            return False
        self.index = index
        return True


NOTHING_THERE = {
    Command.NEXT: "end of the reading",
    Command.PREVIOUS: "start of the reading",
    Command.NEXT_HEADING: "no more headings",
    Command.PREVIOUS_HEADING: "no earlier headings",
    Command.NEXT_TABLE: "no more table rows",
    Command.PREVIOUS_TABLE: "no earlier table rows",
    Command.NEXT_CODE: "no more code blocks",
    Command.PREVIOUS_CODE: "no earlier code blocks",
    Command.NEXT_LIST: "no more list items",
    Command.PREVIOUS_LIST: "no earlier list items",
}


class Navigator:
    """Turns a command into the utterances that answer it."""

    def __init__(self, slice_: Slice, *, verbosity: Verbosity = Verbosity.LOW,
                 label: str = ""):
        self.cursor = Cursor(blocks=slice_.readable())
        self.verbosity = verbosity
        self.label = label

    # -- speaking ----------------------------------------------------------

    def opening(self) -> list[Utterance]:
        """What a listener hears before anything is read.

        A non-visual user has no other way to know what was aimed at or how
        much of it there is.
        """
        blocks = len(self.cursor.blocks)
        if not blocks:
            return [Utterance.narration("nothing readable found")]
        target = f"{self.label}. " if self.label else ""
        headings = sum(1 for b in self.cursor.blocks if b.kind == BlockKind.HEADING)
        summary = f"{target}{blocks} blocks"
        if headings:
            summary += f", {headings} headings"
        return [Utterance.narration(summary + ".")]

    def speak_current(self) -> list[Utterance]:
        block = self.cursor.current
        if block is None:
            return [Utterance.narration("nothing to read")]
        utterance = to_speech(block, verbosity=self.verbosity,
                              index=self.cursor.index + 1,
                              total=len(self.cursor.blocks))
        return [utterance] if utterance else [Utterance.narration("nothing to read")]

    def handle(self, command: Command) -> list[Utterance]:
        """Apply a command and return what to say. Never returns nothing."""
        if command in (Command.STOP, Command.QUIT):
            return []

        if command is Command.REPEAT:
            return self.speak_current()

        if command is Command.WHERE:
            block = self.cursor.current
            kind = block.kind.value.replace("_", " ") if block else "nothing"
            where = f"{kind}, {self.cursor.position}"
            if self.label:
                where += f", in {self.label}"
            return [Utterance.narration(where)]

        if command is Command.SPELL:
            block = self.cursor.current
            if block is None:
                return [Utterance.narration("nothing to spell")]
            return [spell(block)]

        if command is Command.SAY_ALL:
            return self.say_all()

        moved = self._apply_move(command)
        if not moved:
            return [Utterance.narration(NOTHING_THERE.get(command, "nothing there"))]
        return self.speak_current()

    def say_all(self) -> list[Utterance]:
        """Everything from the cursor to the end, as one continuous reading."""
        out: list[Utterance] = []
        total = len(self.cursor.blocks)
        for position in range(self.cursor.index, total):
            utterance = to_speech(self.cursor.blocks[position],
                                  verbosity=self.verbosity,
                                  index=position + 1, total=total)
            if utterance:
                out.append(utterance)
        return out or [Utterance.narration("nothing to read")]

    def _apply_move(self, command: Command) -> bool:
        if command is Command.FIRST:
            return self.cursor.go(0)
        if command is Command.LAST:
            return self.cursor.go(len(self.cursor.blocks) - 1)
        if command in (Command.NEXT, Command.PREVIOUS):
            return self.cursor.move(-1 if command in BACKWARD else 1)
        kind = KIND_FOR.get(command)
        if kind is None:
            return False
        return self.cursor.seek_kind(kind, backward=command in BACKWARD)


def command_for(key: str) -> Command | None:
    return KEYS.get(key)
