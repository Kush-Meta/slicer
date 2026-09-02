"""Run every suite. No external test runner required."""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_slicer.py", "test_narrator.py", "test_conductor.py", "test_golden.py"]

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def main() -> int:
    total = passed = 0
    failures: list[str] = []
    for suite in SUITES:
        print(f"\n{BOLD}{suite}{RESET}")
        proc = subprocess.run([sys.executable, os.path.join(HERE, suite)],
                              capture_output=True, text=True)
        print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(f"{DIM}{proc.stderr.rstrip()}{RESET}")
        last = [ln for ln in proc.stdout.splitlines() if "passed" in ln]
        if last:
            got, _, want = last[-1].partition("/")
            try:
                passed += int(got.strip())
                total += int(want.split()[0])
            except ValueError:
                pass
        if proc.returncode != 0:
            failures.append(suite)

    print(f"\n{BOLD}{'=' * 46}{RESET}")
    colour = RED if failures else GREEN
    print(f"{colour}{passed}/{total} tests passed{RESET} across {len(SUITES)} suites")
    if failures:
        print(f"{RED}failing suites: {', '.join(failures)}{RESET}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
