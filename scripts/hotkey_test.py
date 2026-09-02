"""Minimal isolated check: does the global hotkey actually fire?

Registers cmd-ctrl-R and pumps the application event queue, printing on every
press. Nothing else - no capture, no speech, no daemon - so a failure here is
unambiguously about hotkey delivery.

Run:  ./.venv/bin/python scripts/hotkey_test.py
Then press cmd-ctrl-R a few times. Ctrl-C to stop.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from Foundation import NSDate, NSRunLoop

from slicer.hotkey import register_read_hotkey

NSEventMaskAny = 0xFFFFFFFFFFFFFFFF

presses = {"n": 0}


def on_press() -> None:
    presses["n"] += 1
    print(f"\r  \033[32mPRESSED\033[0m  {presses['n']} time(s)          ", flush=True)


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()

    try:
        description = register_read_hotkey(on_press)
    except Exception as exc:              # noqa: BLE001
        print(f"  registration FAILED: {exc}")
        return 2
    print("  (registration returned success - if nothing prints below when you")
    print("   press it, the problem is event delivery, not registration)")

    print(f"\n  registered \033[36m{description}\033[0m")
    print("  press it now, from this or any other app. ctrl-C to stop.\n")

    loop = NSRunLoop.currentRunLoop()
    started = time.time()
    try:
        while True:
            event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(0.05),
                "kCFRunLoopDefaultMode", True,
            )
            if event is not None:
                app.sendEvent_(event)
            loop.runMode_beforeDate_(
                "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.01)
            )
            if int(time.time() - started) % 10 == 0 and presses["n"] == 0:
                print(f"\r  waiting... {int(time.time() - started)}s, no presses yet",
                      end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\n  {presses['n']} press(es) received\n")
    return 0 if presses["n"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
