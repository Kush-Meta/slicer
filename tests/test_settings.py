"""Saved preferences, and the precedence between file and flags."""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.settings import Settings                       # noqa: E402


class Args:
    def __init__(self, **kw):
        self.voice = kw.get("voice")
        self.rate = kw.get("rate")
        self.speech = kw.get("speech")
        self.verbosity = kw.get("verbosity", "low")
        self.highlight = kw.get("highlight", False)
        self.follow = kw.get("follow", False)


def _temp() -> str:
    handle, path = tempfile.mkstemp(prefix="slicer-settings-", suffix=".json")
    os.close(handle)
    os.unlink(path)
    return path


def test_defaults_are_usable_with_no_file():
    settings = Settings.load(_temp())
    assert settings.verbosity == "low"
    assert settings.highlight is True
    assert settings.voice is None


def test_settings_survive_a_round_trip():
    path = _temp()
    Settings(voice="Alice", rate=210, verbosity="high", follow=True).save(path)
    loaded = Settings.load(path)
    assert (loaded.voice, loaded.rate, loaded.verbosity, loaded.follow) == \
        ("Alice", 210, "high", True)
    os.unlink(path)


def test_a_corrupt_file_never_stops_the_tool():
    """A bad settings file must not prevent Slicer from speaking."""
    path = _temp()
    with open(path, "w") as handle:
        handle.write("{not json at all")
    assert Settings.load(path).verbosity == "low"
    os.unlink(path)


def test_unknown_keys_from_a_newer_version_are_ignored():
    path = _temp()
    with open(path, "w") as handle:
        json.dump({"voice": "Alice", "future_option": 42}, handle)
    assert Settings.load(path).voice == "Alice"
    os.unlink(path)


def test_a_flag_beats_the_saved_value():
    saved = Settings(voice="Alice", rate=180, verbosity="off")
    merged = saved.overridden_by(Args(voice="Fred", verbosity="high"))
    assert merged.voice == "Fred"
    assert merged.verbosity == "high"
    assert merged.rate == 180, "an unset flag overwrote a saved value"


def test_an_unchanged_default_does_not_overwrite_a_preference():
    """argparse always supplies a verbosity, so it must not look like a choice."""
    saved = Settings(verbosity="high")
    assert saved.overridden_by(Args(verbosity="low")).verbosity == "high"


def test_the_settings_file_is_private():
    path = _temp()
    Settings(voice="Alice").save(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    os.unlink(path)


def test_everything_is_describable():
    for label, value in Settings().describe():
        assert label and value, "a setting with nothing to say about it"


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
