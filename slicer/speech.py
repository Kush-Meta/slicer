"""Speech backends.

Slicer started on the `say` command because it needed no setup. That cost is
now the thing worth removing: a subprocess per utterance is ~145 ms of spawn
before a word is heard, and - more importantly for a screen reader - a killed
process cannot be resumed, so pausing restarts the block from the beginning.

AVSpeechSynthesizer runs in this process. Measured on an M-series Mac it begins
speaking in ~49 ms, pauses and resumes mid-sentence, and stops instantly. It
also works without an AppKit run loop being pumped, which matters because
`slicer navigate` blocks its main thread reading keys.

The `say` backend stays, as a fallback and because it is the one thing that
will still work if the AVFoundation bindings are missing.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable, Protocol

# say(1)'s default is about 175 words per minute; AVSpeechUtterance uses an
# opaque 0-1 scale whose default is 0.5. Anchoring one to the other is
# approximate by nature - the scale is not documented as linear - but it makes
# --rate mean the same thing whichever backend is in use.
WORDS_PER_MINUTE_DEFAULT = 175.0
AV_RATE_DEFAULT = 0.5

# How often a blocking speak() checks whether it has been superseded.
POLL = 0.005
# How long to wait for speech to begin before assuming it already finished.
START_TIMEOUT = 0.4


class SpeechBackend(Protocol):
    name: str
    supports_pause: bool

    def speak(self, text: str, *, voice: str | None, rate: int | None,
              still_current: Callable[[], bool]) -> bool:
        """Speak, blocking until done. False if it was cut short."""

    def stop(self) -> None: ...
    def pause(self) -> bool: ...
    def resume(self) -> bool: ...


class SayBackend:
    """A subprocess per utterance. Simple, and cannot resume."""

    name = "say"
    supports_pause = False

    def __init__(self) -> None:
        self.binary = shutil.which("say") or "/usr/bin/say"
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def speak(self, text: str, *, voice: str | None, rate: int | None,
              still_current: Callable[[], bool]) -> bool:
        args = [self.binary]
        if voice:
            args += ["-v", voice]
        if rate:
            args += ["-r", str(rate)]
        args += ["--", text]
        with self._lock:
            if not still_current():
                return False
            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = self._proc
        code = proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
        return code == 0

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def has_voice(self, name: str) -> bool:
        result = subprocess.run([self.binary, "-v", "?"],
                                capture_output=True, text=True)
        return any(line.split()[:1] == [name] for line in result.stdout.splitlines())

    def voice_names(self) -> list[str]:
        result = subprocess.run([self.binary, "-v", "?"],
                                capture_output=True, text=True)
        return sorted({line.split()[0] for line in result.stdout.splitlines()
                       if line.strip()})


class AVSpeechBackend:
    """In-process synthesis. Fast to start, and genuinely pausable.

    One instance per utterance, deliberately. A reused AVSpeechSynthesizer
    needs a run loop to reset its state between utterances: without one it
    speaks the first utterance and then never reports `isSpeaking` again, so
    every subsequent call blocks until its timeout. Measured on this machine:

        reused, no run loop        58 ms, then never, never, never
        reused, run loop pumped    37, 5, 7, 6 ms
        fresh instance each time   5, 12, 13, 14 ms

    `slicer navigate` blocks its main thread reading keys and speaks on a
    worker, so there is no run loop to rely on. A fresh instance needs none and
    is the faster of the two anyway. The run loop is still pumped
    opportunistically, which helps where one exists and costs nothing where it
    does not.
    """

    name = "avspeech"
    supports_pause = True

    IMMEDIATE = 0        # AVSpeechBoundaryImmediate

    def __init__(self) -> None:
        import AVFoundation as AV  # noqa: PLC0415

        self._av = AV
        self._lock = threading.Lock()
        self._current = None
        self._voices: dict[str, object] = {}
        for voice in AV.AVSpeechSynthesisVoice.speechVoices() or []:
            self._voices[str(voice.name()).lower()] = voice
        # Constructing one at startup pays the framework's first-use cost now
        # rather than on the first thing the user asks to hear.
        self._warm()

    def _warm(self) -> None:
        try:
            self._av.AVSpeechSynthesizer.alloc().init()
        except Exception:                 # noqa: BLE001
            pass

    # -- speaking ----------------------------------------------------------

    def speak(self, text: str, *, voice: str | None, rate: int | None,
              still_current: Callable[[], bool]) -> bool:
        utterance = self._av.AVSpeechUtterance.speechUtteranceWithString_(text)
        utterance.setRate_(self._rate(rate))
        chosen = self._voice(voice)
        if chosen is not None:
            utterance.setVoice_(chosen)

        synth = self._av.AVSpeechSynthesizer.alloc().init()
        with self._lock:
            if not still_current():
                return False
            self._current = synth
            synth.speakUtterance_(utterance)

        try:
            if not self._await_start(synth, still_current):
                return False
            return self._await_end(synth, still_current)
        finally:
            with self._lock:
                if self._current is synth:
                    self._current = None

    def _await_start(self, synth, still_current) -> bool:
        deadline = time.perf_counter() + START_TIMEOUT
        while not synth.isSpeaking():
            if not still_current():
                self._stop(synth)
                return False
            if time.perf_counter() > deadline:
                return True               # very short utterances can finish first
            self._pump()
            time.sleep(POLL)
        return True

    def _await_end(self, synth, still_current) -> bool:
        while synth.isSpeaking() or synth.isPaused():
            if not still_current():
                self._stop(synth)
                return False
            self._pump()
            time.sleep(POLL)
        return True

    def _pump(self) -> None:
        """Give this thread's run loop a turn, if it has one.

        Helps where a run loop exists - the menu bar app and the daemon - and
        is a cheap no-op on a bare worker thread.
        """
        try:
            from Foundation import NSDate, NSRunLoop  # noqa: PLC0415
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0))
        except Exception:                 # noqa: BLE001
            pass

    def _stop(self, synth) -> None:
        try:
            synth.stopSpeakingAtBoundary_(self.IMMEDIATE)
        except Exception:                 # noqa: BLE001
            pass

    def stop(self) -> None:
        with self._lock:
            synth = self._current
        if synth is not None:
            self._stop(synth)

    def pause(self) -> bool:
        """Pause mid-sentence. Resuming continues from the same word."""
        with self._lock:
            synth = self._current
        if synth is None:
            return False
        try:
            return bool(synth.pauseSpeakingAtBoundary_(self.IMMEDIATE))
        except Exception:                 # noqa: BLE001
            return False

    def resume(self) -> bool:
        with self._lock:
            synth = self._current
        if synth is None:
            return False
        try:
            return bool(synth.continueSpeaking())
        except Exception:                 # noqa: BLE001
            return False

    # -- settings ----------------------------------------------------------

    def _rate(self, words_per_minute: int | None) -> float:
        if not words_per_minute:
            return AV_RATE_DEFAULT
        scaled = AV_RATE_DEFAULT * (words_per_minute / WORDS_PER_MINUTE_DEFAULT)
        return max(0.0, min(1.0, scaled))

    def _voice(self, name: str | None):
        if not name:
            return None
        return self._voices.get(name.lower())

    def time_to_start(self, text: str = "one") -> float:
        """Milliseconds from asking for speech to speech actually beginning.

        Directly observable here, unlike with a subprocess: the synthesizer
        reports isSpeaking, so the moment audio starts can be watched for
        rather than inferred from how long a whole word took.
        """
        utterance = self._av.AVSpeechUtterance.speechUtteranceWithString_(text)
        synth = self._av.AVSpeechSynthesizer.alloc().init()
        start = time.perf_counter()
        synth.speakUtterance_(utterance)
        while not synth.isSpeaking():
            if time.perf_counter() - start > 2.0:
                break
            time.sleep(0.001)
        elapsed = (time.perf_counter() - start) * 1000
        self._stop(synth)
        return elapsed

    def has_voice(self, name: str) -> bool:
        return name.lower() in self._voices

    def voice_names(self) -> list[str]:
        return sorted(str(v.name()) for v in
                      (self._av.AVSpeechSynthesisVoice.speechVoices() or []))


def default_backend() -> SpeechBackend:
    """In-process synthesis where available, `say` otherwise."""
    try:
        return AVSpeechBackend()
    except Exception:                     # noqa: BLE001
        return SayBackend()


def backend_named(name: str) -> SpeechBackend:
    if name == "say":
        return SayBackend()
    if name == "avspeech":
        return AVSpeechBackend()
    raise ValueError(f"unknown speech backend {name!r}")
