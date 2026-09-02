"""Speech output, playback transport, and the reading cursor.

The important thing here is cancellation. A reading is stamped with an epoch;
every utterance carries it; anything arriving from a superseded epoch is
discarded rather than spoken. Without this, pressing stop kills the current
utterance and then the queue keeps going - the bug where the app appears to
speak after you told it to stop.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .editor import Utterance

SAY = shutil.which("say") or "/usr/bin/say"


class State(str, Enum):
    IDLE = "idle"
    SPEAKING = "speaking"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class Progress:
    index: int
    total: int
    utterance: Utterance


class Narrator:
    """Speaks utterances in order, cancellably."""

    _epochs = itertools.count(1)

    def __init__(self, *, voice: str | None = None, rate: int | None = None):
        self.voice = voice
        self.rate = rate
        self.state = State.IDLE
        self._epoch = 0
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._skip = 0          # +1 next block, -1 previous
        self._pause = threading.Event()
        self._pause.set()       # set means "running"

    # -- transport ---------------------------------------------------------

    def new_epoch(self) -> int:
        """Supersede any reading in flight and return the new epoch."""
        with self._lock:
            self._epoch = next(Narrator._epochs)
            self._kill_locked()
            self._skip = 0
            self._pause.set()
            return self._epoch

    @property
    def epoch(self) -> int:
        return self._epoch

    def stop(self) -> None:
        with self._lock:
            self._epoch = next(Narrator._epochs)   # everything in flight is now stale
            self._kill_locked()
            self.state = State.STOPPED
            self._pause.set()

    def toggle_pause(self) -> bool:
        if self._pause.is_set():
            self._pause.clear()
            self.state = State.PAUSED
            with self._lock:
                self._kill_locked()   # the current utterance restarts on resume
            return True
        self._pause.set()
        self.state = State.SPEAKING
        return False

    def skip(self, delta: int) -> None:
        with self._lock:
            self._skip = delta
            self._kill_locked()

    # -- speaking ----------------------------------------------------------

    def read(self, utterances: list[Utterance], epoch: int):
        """Speak the list, yielding Progress. Returns early if superseded."""
        index = 0
        self.state = State.SPEAKING
        while 0 <= index < len(utterances):
            if epoch != self._epoch:
                return                      # a newer reading owns the output now
            self._pause.wait()
            if epoch != self._epoch:
                return

            utterance = utterances[index]
            yield Progress(index=index, total=len(utterances), utterance=utterance)

            completed = self._say(utterance.spoken, epoch)

            with self._lock:
                skip, self._skip = self._skip, 0
            if skip:
                index = max(0, index + skip)
                continue
            if not completed and self._pause.is_set() and epoch == self._epoch:
                return                      # killed for a reason we did not set
            if completed:
                index += 1
        self.state = State.IDLE

    def _say(self, text: str, epoch: int) -> bool:
        """Speak one utterance. False if it was cut short."""
        args = [SAY]
        if self.voice:
            args += ["-v", self.voice]
        if self.rate:
            args += ["-r", str(self.rate)]
        args += ["--", text]

        with self._lock:
            if epoch != self._epoch:
                return False
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        proc = self._proc
        code = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
        return code == 0

    def _kill_locked(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass


def time_to_first_audio(sample: str = "Slicer.", voice: str | None = None) -> float:
    """Measure spawn-to-exit for a very short utterance, in milliseconds.

    A floor on how fast this machine can begin speaking at all, which is the
    number the 900ms first-word budget has to fit inside.
    """
    args = [SAY] + (["-v", voice] if voice else []) + ["--", sample]
    start = time.perf_counter()
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (time.perf_counter() - start) * 1000
