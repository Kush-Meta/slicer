"""The interactive loop: key decoding and command dispatch.

Driven without a terminal by feeding a pipe and using a narrator that records
instead of speaking, so the loop's logic is testable without audio or a tty.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.blocks import Block, BlockKind, Box, Slice, TextLine   # noqa: E402
from slicer.editor import Verbosity                                # noqa: E402
from slicer.interactive import Session, read_key                   # noqa: E402
from slicer.narrator import Narrator                               # noqa: E402
from slicer.navigator import Command                               # noqa: E402


class RecordingNarrator(Narrator):
    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []

    def _say(self, text: str, epoch: int) -> bool:
        if epoch != self._epoch:
            return False
        self.spoken.append(text)
        return True


def _block(ident: str, kind: BlockKind, text: str) -> Block:
    return Block(id=ident, kind=kind, lines=[
        TextLine(id=f"l{ident}", text=text, confidence=1.0, box=Box(0, 0, 100, 20))])


PAGE = [
    _block("1", BlockKind.HEADING, "Quarterly Results"),
    _block("2", BlockKind.BODY, "Revenue grew across every region."),
    _block("3", BlockKind.HEADING, "Reliability"),
    _block("4", BlockKind.BODY, "Two incidents, both the same cause."),
]


def _session() -> tuple[Session, RecordingNarrator]:
    narrator = RecordingNarrator()
    slice_ = Slice(blocks=list(PAGE), width=800, height=600)
    return Session(slice_, narrator, verbosity=Verbosity.LOW, label="Safari"), narrator


def _feed(keys: str):
    """A readable stream backed by a real pipe, so select() works on it."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, keys.encode())
    os.close(write_fd)
    return os.fdopen(read_fd, "r")


# -- key decoding ----------------------------------------------------------

def test_a_plain_key_is_returned_as_itself():
    with _feed("h") as stream:
        assert read_key(stream) == "h"


def test_an_arrow_key_is_kept_whole():
    """Three bytes. Reading one at a time turns Down into Escape plus junk."""
    with _feed("\x1b[B") as stream:
        assert read_key(stream) == "\x1b[B"


def test_a_bare_escape_is_not_mistaken_for_an_arrow():
    with _feed("\x1b") as stream:
        assert read_key(stream) == "\x1b"


def test_consecutive_keys_decode_independently():
    with _feed("\x1b[Ah") as stream:
        assert read_key(stream) == "\x1b[A"
        assert read_key(stream) == "h"


# -- dispatch --------------------------------------------------------------

def _settle(session: Session) -> None:
    if session._speaking:
        session._speaking.join(timeout=3)


def test_opening_announces_the_target_then_reads():
    session, narrator = _session()
    session.open()
    _settle(session)
    assert "Safari" in narrator.spoken[0]
    assert "Quarterly Results" in " ".join(narrator.spoken)


def test_jumping_to_a_heading_speaks_it():
    session, narrator = _session()
    session.dispatch(Command.NEXT_HEADING)
    _settle(session)
    assert "Reliability" in " ".join(narrator.spoken)


def test_quit_ends_the_session():
    session, _ = _session()
    assert session.dispatch(Command.QUIT) is False


def test_stop_silences_but_continues():
    session, _ = _session()
    assert session.dispatch(Command.STOP) is True


def test_a_new_command_supersedes_whatever_was_speaking():
    """The whole point of the epoch mechanism, exercised through the loop."""
    session, narrator = _session()
    session.dispatch(Command.SAY_ALL)
    session.dispatch(Command.FIRST)
    _settle(session)
    # The superseded say-all must not have run to completion behind the move.
    assert narrator.spoken, "nothing was spoken at all"
    assert "Quarterly Results" in narrator.spoken[-1]


def test_failed_navigation_is_spoken_not_silent():
    session, narrator = _session()
    session.dispatch(Command.LAST)
    _settle(session)
    narrator.spoken.clear()
    session.dispatch(Command.NEXT_HEADING)
    _settle(session)
    assert any("no more headings" in s for s in narrator.spoken)


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
