"""The interactive reading loop.

Everything here exists to keep the keyboard responsive while speech is running.
A screen reader that finishes its sentence before accepting the next key is
unusable: skimming means interrupting constantly, and a reader who has to wait
out a paragraph cannot skim at all.

So speech runs on a worker thread and the main thread does nothing but read
keys. Interruption is the narrator's epoch mechanism doing the job it was built
for - a new command supersedes whatever is in flight, and the stale utterances
are discarded rather than spoken late.
"""

from __future__ import annotations

import select
import sys
import termios
import threading
import tty

from .blocks import Slice
from .editor import Utterance, Verbosity
from .narrator import Narrator
from .navigator import Command, Navigator, command_for

# Long enough to catch the rest of an escape sequence, short enough that a bare
# Escape does not feel sticky.
ESCAPE_WINDOW = 0.05

HELP = [
    ("n / down", "next block"),
    ("p / up", "previous block"),
    ("h / H", "next / previous heading"),
    ("t / T", "next / previous table row"),
    ("c / C", "next / previous code block"),
    ("l / L", "next / previous list item"),
    (", / .", "first / last block"),
    ("a", "read everything from here"),
    ("r", "repeat"),
    ("s", "spell it out"),
    ("w", "where am I"),
    ("esc", "stop speaking"),
    ("q", "quit"),
]


def read_key(stream=None) -> str:
    """One keypress, with escape sequences kept whole.

    Arrow keys arrive as three bytes. Reading one at a time would turn a single
    Down into an Escape followed by two stray letters, which is a real bug in
    the naive version of this loop.
    """
    stream = stream or sys.stdin
    first = stream.read(1)
    if first != "\x1b":
        return first
    if not select.select([stream], [], [], ESCAPE_WINDOW)[0]:
        return "\x1b"                     # a bare Escape
    rest = stream.read(1)
    if rest != "[":
        return "\x1b" + rest
    return "\x1b[" + stream.read(1)


class Session:
    """One reading, navigated until the user quits."""

    def __init__(self, slice_: Slice, narrator: Narrator, *,
                 verbosity: Verbosity = Verbosity.LOW, label: str = "",
                 on_block=None):
        self.navigator = Navigator(slice_, verbosity=verbosity, label=label)
        self.narrator = narrator
        self.on_block = on_block
        self._speaking: threading.Thread | None = None

    def say(self, utterances: list[Utterance]) -> None:
        """Speak, superseding anything already in flight."""
        if not utterances:
            return
        epoch = self.narrator.new_epoch()

        def run() -> None:
            for progress in self.narrator.read(utterances, epoch):
                if self.on_block:
                    self.on_block(progress)

        self._speaking = threading.Thread(target=run, daemon=True)
        self._speaking.start()

    def open(self) -> None:
        self.say(self.navigator.opening() + self.navigator.speak_current())

    def dispatch(self, command: Command) -> bool:
        """Apply one command. False means the session should end."""
        if command is Command.QUIT:
            self.narrator.stop()
            return False
        if command is Command.STOP:
            self.narrator.stop()
            return True
        self.say(self.navigator.handle(command))
        return True

    def run(self) -> int:
        """Read keys until quit. Requires a terminal."""
        if not sys.stdin.isatty():
            raise RuntimeError("interactive navigation needs a terminal")
        descriptor = sys.stdin.fileno()
        saved = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            self.open()
            while True:
                key = read_key()
                if not key:
                    continue
                command = command_for(key)
                if command is None:
                    continue              # unbound keys do nothing, silently
                if not self.dispatch(command):
                    return 0
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
            self.narrator.stop()
