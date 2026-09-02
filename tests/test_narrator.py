"""Narrator transport and epoch cancellation.

These were the untested part of the system, because exercising them for real
means making sound and waiting on it. The seam is Narrator._say: a subclass
records what would have been spoken and returns immediately, so the epoch
logic can be driven deterministically.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.blocks import BlockKind                    # noqa: E402
from slicer.editor import Utterance                    # noqa: E402
from slicer.narrator import Narrator, State            # noqa: E402


class FakeNarrator(Narrator):
    """Speaks instantly and records, but keeps the real epoch behaviour."""

    def __init__(self) -> None:
        super().__init__()
        self.spoken: list[str] = []
        self.on_say = None
        self.cut_at: set[str] = set()   # utterances that get interrupted

    def _say(self, text: str, epoch: int) -> bool:
        if epoch != self._epoch:
            return False                # a superseded reading must not be heard
        self.spoken.append(text)
        if self.on_say:
            self.on_say(text)
        if text in self.cut_at:
            return False                # interrupted before it finished
        return epoch == self._epoch


def _utterances(n: int) -> list[Utterance]:
    return [Utterance(block_id=f"b{i}", text=f"block {i}", kind=BlockKind.BODY,
                      confidence=1.0) for i in range(n)]


def test_reads_every_block_in_order():
    narrator = FakeNarrator()
    utterances = _utterances(4)
    epoch = narrator.new_epoch()
    seen = [p.utterance.text for p in narrator.read(utterances, epoch)]
    assert seen == ["block 0", "block 1", "block 2", "block 3"]
    assert narrator.spoken == seen
    assert narrator.state is State.IDLE


def test_a_new_reading_supersedes_the_old_one():
    """The bug this prevents: starting a new reading, then hearing the old one.

    Progress is announced before an utterance is spoken, so the block that was
    in flight when the epoch changed is reported and then correctly never
    reaches the speaker. Four queued blocks after it are dropped entirely.
    """
    narrator = FakeNarrator()
    progress: list[str] = []
    epoch = narrator.new_epoch()
    for step in narrator.read(_utterances(6), epoch):
        progress.append(step.utterance.text)
        if len(progress) == 2:
            narrator.new_epoch()        # a second reading starts
    assert progress == ["block 0", "block 1"]
    assert narrator.spoken == ["block 0"]


def test_stop_discards_queued_blocks():
    """Stop takes effect between the announcement and the speaker."""
    narrator = FakeNarrator()
    progress: list[str] = []
    epoch = narrator.new_epoch()
    for step in narrator.read(_utterances(5), epoch):
        progress.append(step.utterance.text)
        if len(progress) == 1:
            narrator.stop()
    assert progress == ["block 0"]
    assert narrator.spoken == []        # nothing was ever sent to the speaker


def test_results_from_a_stale_epoch_are_never_spoken():
    narrator = FakeNarrator()
    stale = narrator.new_epoch()
    narrator.new_epoch()                # a newer reading owns the output now
    spoken = list(narrator.read(_utterances(3), stale))
    assert spoken == []
    assert narrator.spoken == []


def test_an_interrupted_block_stops_the_reading():
    """A block cut short for no stated reason ends the reading rather than
    silently continuing - the interruption came from somewhere unmodelled."""
    narrator = FakeNarrator()
    narrator.cut_at = {"block 1"}
    epoch = narrator.new_epoch()
    seen = [s.utterance.text for s in narrator.read(_utterances(4), epoch)]
    assert seen == ["block 0", "block 1"]


def test_skip_forward_resumes_after_cutting_a_block_short():
    """Pressing next mid-utterance kills it, and reading must carry on.

    This is the difference skip makes: the same interruption that would end a
    reading instead advances it, because the Conductor knows why it happened.
    """
    narrator = FakeNarrator()
    narrator.cut_at = {"block 1"}
    narrator.on_say = lambda text: narrator.skip(1) if text == "block 1" else None
    epoch = narrator.new_epoch()
    seen = [s.utterance.text for s in narrator.read(_utterances(4), epoch)]
    assert seen == ["block 0", "block 1", "block 2", "block 3"]


def test_skip_backward_repeats_a_block():
    narrator = FakeNarrator()
    seen: list[str] = []
    epoch = narrator.new_epoch()
    calls = {"n": 0}

    def maybe_skip(_text):
        calls["n"] += 1
        if calls["n"] == 2:
            narrator.skip(-1)

    narrator.on_say = maybe_skip
    for step in narrator.read(_utterances(3), epoch):
        seen.append(step.utterance.text)
        if len(seen) > 6:
            break
    assert seen[:3] == ["block 0", "block 1", "block 0"]


def test_epochs_are_monotonic():
    narrator = FakeNarrator()
    epochs = [narrator.new_epoch() for _ in range(5)]
    assert epochs == sorted(epochs) and len(set(epochs)) == 5


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
