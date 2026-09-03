"""Speech output, playback transport, and the reading cursor.

The important thing here is cancellation. A reading is stamped with an epoch;
every utterance carries it; anything arriving from a superseded epoch is
discarded rather than spoken. Without this, pressing stop kills the current
utterance and then the queue keeps going - the bug where the app appears to
speak after you told it to stop.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .editor import Utterance
from .speech import SpeechBackend, default_backend


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

    def __init__(self, *, voice: str | None = None, rate: int | None = None,
                 backend: SpeechBackend | None = None):
        self.voice = voice
        self.rate = rate
        self.backend = backend or default_backend()
        self.state = State.IDLE
        self._epoch = 0
        self._lock = threading.Lock()
        self._skip = 0          # +1 next block, -1 previous
        self._pause = threading.Event()
        self._pause.set()       # set means "running"

    @property
    def can_pause_mid_sentence(self) -> bool:
        """Whether pausing resumes from the same word or restarts the block."""
        return self.backend.supports_pause

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
        """Pause or resume. True if now paused.

        With an in-process backend this suspends mid-sentence and resumes from
        the same word. With `say` there is no such thing - the process is
        killed and the block restarts - which is precisely why the in-process
        backend exists.
        """
        if self._pause.is_set():
            self._pause.clear()
            self.state = State.PAUSED
            if self.backend.supports_pause:
                self.backend.pause()
            else:
                with self._lock:
                    self._kill_locked()
            return True
        self._pause.set()
        self.state = State.SPEAKING
        if self.backend.supports_pause:
            self.backend.resume()
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
        """Speak one utterance. False if it was cut short.

        The seam the tests replace: everything above this line is scheduling,
        everything below is the platform.
        """
        return self.backend.speak(
            text, voice=self.voice, rate=self.rate,
            still_current=lambda: epoch == self._epoch,
        )

    def _kill_locked(self) -> None:
        self.backend.stop()


def time_to_first_audio(sample: str = "one", voice: str | None = None,
                        backend: SpeechBackend | None = None) -> float:
    """Milliseconds until speech actually starts.

    Only a backend that can report when it began speaking gives a real answer.
    A subprocess cannot, so for `say` this measures the whole round trip and
    the caller is expected to label it as such rather than pretend it is a
    startup cost - an earlier version inferred that cost by fitting a line and
    the answer moved by 400ms depending on which words were used.
    """
    backend = backend or default_backend()
    if hasattr(backend, "time_to_start"):
        return backend.time_to_start(sample)
    start = time.perf_counter()
    backend.speak(sample, voice=voice, rate=None, still_current=lambda: True)
    return (time.perf_counter() - start) * 1000
