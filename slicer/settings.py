"""Preferences that survive a restart.

A screen reader whose voice, rate and verbosity have to be re-specified on
every invocation is a demo. These are the settings a user picks once and then
never thinks about again, so they live in a file rather than in flags.

Precedence is the usual one and is worth stating because it is easy to get
backwards: an explicit flag beats the file, the file beats the built-in
default, and nothing is written back unless the user asked for it to be.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .ipc import RUNTIME_DIR, ensure_runtime_dir

SETTINGS_PATH = os.path.join(RUNTIME_DIR, "settings.json")


@dataclass
class Settings:
    voice: str | None = None
    rate: int | None = None
    verbosity: str = "low"
    highlight: bool = True
    follow: bool = False
    speech: str | None = None       # None means "whichever is available"

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: str = SETTINGS_PATH) -> "Settings":
        """Read saved settings. A missing or damaged file is not an error.

        A corrupt settings file must never stop the tool from speaking, so it
        is ignored rather than reported - the defaults are always usable.
        """
        try:
            with open(path) as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in stored.items() if k in known})

    def save(self, path: str = SETTINGS_PATH) -> bool:
        try:
            ensure_runtime_dir()
            with open(path, "w") as handle:
                json.dump(asdict(self), handle, indent=2, sort_keys=True)
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False

    # -- precedence --------------------------------------------------------

    def overridden_by(self, args) -> "Settings":
        """A copy with any explicitly given flags applied over the top."""
        merged = Settings(**asdict(self))
        for name in ("voice", "rate", "speech"):
            value = getattr(args, name, None)
            if value is not None:
                setattr(merged, name, value)
        # argparse gives verbosity a default, so it cannot be distinguished
        # from an explicit choice by None. The caller passes the parser default
        # so an unchanged value leaves the stored preference alone.
        verbosity = getattr(args, "verbosity", None)
        if verbosity is not None and verbosity != _DEFAULT_VERBOSITY:
            merged.verbosity = verbosity
        for name in ("highlight", "follow"):
            if getattr(args, name, False):
                setattr(merged, name, True)
        return merged

    def describe(self) -> list[tuple[str, str]]:
        return [
            ("voice", self.voice or "system default"),
            ("rate", f"{self.rate} words per minute" if self.rate else "system default"),
            ("verbosity", self.verbosity),
            ("highlight", "on" if self.highlight else "off"),
            ("follow scrolling", "on" if self.follow else "off"),
            ("speech backend", self.speech or "in-process where available"),
        ]


_DEFAULT_VERBOSITY = "low"
