"""Stage timings and reading records.

Everything stays on disk, local, in JSON lines. The dashboard reads this file;
nothing here reaches the network.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

DEFAULT_LOG = os.path.expanduser("~/.slicer/telemetry.jsonl")


@dataclass
class Timings:
    stages: dict[str, float] = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)

    @contextmanager
    def stage(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = (time.perf_counter() - start) * 1000

    @property
    def total_ms(self) -> float:
        return sum(self.stages.values())

    def to_first_word(self) -> float:
        """Everything that has to finish before the first syllable."""
        return sum(v for k, v in self.stages.items() if k != "speak")

    def render(self) -> str:
        parts = [f"{name} {ms:.0f}ms" for name, ms in self.stages.items()]
        return "  ".join(parts) + f"  |  to first word {self.to_first_word():.0f}ms"


def record(event: dict, path: str = DEFAULT_LOG) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"t": time.time(), **event}) + "\n")
    except OSError:
        pass  # telemetry must never break a reading
