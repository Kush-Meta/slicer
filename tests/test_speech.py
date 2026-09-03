"""Speech backends.

Deliberately almost silent: the parts worth testing are rate mapping, voice
resolution, cancellation and the pause contract, none of which need audio. The
one test that does speak uses a cancelled utterance, so nothing is heard.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.narrator import Narrator                       # noqa: E402
from slicer.speech import (                                # noqa: E402
    AV_RATE_DEFAULT, WORDS_PER_MINUTE_DEFAULT, AVSpeechBackend, SayBackend,
    backend_named, default_backend,
)


def test_in_process_speech_is_the_default():
    assert default_backend().name == "avspeech"


def test_only_the_in_process_backend_can_pause_mid_sentence():
    """The whole reason for the change: say(1) kills, it does not pause."""
    assert AVSpeechBackend().supports_pause is True
    assert SayBackend().supports_pause is False


def test_the_narrator_reports_which_it_has():
    assert Narrator(backend=AVSpeechBackend()).can_pause_mid_sentence is True
    assert Narrator(backend=SayBackend()).can_pause_mid_sentence is False


def test_backends_can_be_chosen_by_name():
    assert backend_named("say").name == "say"
    assert backend_named("avspeech").name == "avspeech"
    try:
        backend_named("espeak")
    except ValueError:
        return
    raise AssertionError("an unknown backend name was accepted")


# -- rate ------------------------------------------------------------------

def test_the_default_rate_matches_between_backends():
    """--rate should mean the same thing whichever backend is in use."""
    assert AVSpeechBackend()._rate(int(WORDS_PER_MINUTE_DEFAULT)) == AV_RATE_DEFAULT


def test_rate_is_monotonic_and_clamped():
    backend = AVSpeechBackend()
    assert backend._rate(90) < backend._rate(175) < backend._rate(300)
    assert 0.0 <= backend._rate(10_000) <= 1.0
    assert 0.0 <= backend._rate(1) <= 1.0


def test_no_rate_means_the_default():
    assert AVSpeechBackend()._rate(None) == AV_RATE_DEFAULT


# -- voices ----------------------------------------------------------------

def test_voices_are_discoverable():
    names = AVSpeechBackend().voice_names()
    assert len(names) > 10, "suspiciously few voices"
    assert all(isinstance(n, str) and n for n in names)


def test_a_known_voice_resolves_and_an_unknown_one_does_not():
    backend = AVSpeechBackend()
    known = backend.voice_names()[0]
    assert backend.has_voice(known)
    assert backend._voice(known) is not None
    assert not backend.has_voice("NotARealVoiceName")
    assert backend._voice("NotARealVoiceName") is None


def test_voice_lookup_ignores_case():
    backend = AVSpeechBackend()
    known = backend.voice_names()[0]
    assert backend._voice(known.upper()) is not None
    assert backend._voice(known.lower()) is not None


# -- cancellation ----------------------------------------------------------

def test_a_superseded_utterance_is_never_spoken():
    """Silent by construction: still_current is false before it starts."""
    backend = AVSpeechBackend()
    spoken = backend.speak("This must never be heard.", voice=None, rate=None,
                           still_current=lambda: False)
    assert spoken is False


def test_consecutive_utterances_each_actually_speak():
    """Regression: a reused synthesizer spoke once and then silently never again.

    AVSpeechSynthesizer needs a run loop to reset between utterances when it is
    reused. Without one it reported isSpeaking for the first utterance and never
    afterwards, so every later call blocked until its timeout and returned
    having made no sound. Nothing raised; the reading was simply silent.

    Detected by length: real speech takes longer for more words, while a
    timeout takes the same time regardless. The old behaviour would make these
    two durations equal.
    """
    import time
    backend = AVSpeechBackend()
    backend.speak("one", voice=None, rate=None, still_current=lambda: True)

    def duration(phrase: str) -> float:
        start = time.perf_counter()
        backend.speak(phrase, voice=None, rate=None, still_current=lambda: True)
        return time.perf_counter() - start

    short = duration("one")
    longer = duration("one two three four five six seven eight")
    assert longer > short * 1.5, (
        f"a longer phrase took {longer:.2f}s against {short:.2f}s for a short one - "
        "speech is probably not starting at all")


def test_stop_is_safe_when_nothing_is_speaking():
    AVSpeechBackend().stop()      # must not raise
    SayBackend().stop()


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
