"""A single global hotkey, registered the narrowly-scoped way.

There are two ways to get a system-wide hotkey on macOS, and the choice is a
privacy decision rather than a technical one.

`NSEvent.addGlobalMonitorForEventsMatchingMask` is the modern API. It requires
Accessibility permission and it delivers *every* keystroke on the system to
this process - the shape of a keylogger, whatever the intent. For a tool that
already captures the screen, asking for that as well is the wrong trade.

Carbon's `RegisterEventHotKey` is formally deprecated and completely stable. It
needs no permission at all, and it delivers exactly one thing: the combination
that was registered. Electron, VS Code and Slack all still use it, because
Apple never shipped a replacement for this specific job.

Two things are easy to get wrong here, and both were:

  * `RegisterEventHotKey` returns a status that is easy not to check. A failed
    registration is silent, so the app reports a working hotkey that will never
    fire. The status is checked here and a failure raises.
  * Registering is not enough. Carbon dispatches hotkeys from inside
    `ReceiveNextEvent`, which a Cocoa process reaches through
    `NSApplication.nextEventMatchingMask`. A run loop that only pumps CFRunLoop
    sources will never dispatch them - the handler installs cleanly and is
    never called. Whoever owns the main loop must pump the *application* event
    queue; see `daemon.run`.
"""

from __future__ import annotations

from struct import unpack
from typing import Callable

# Importing this installs the Carbon event handler on the dispatcher target.
from quickmachotkey import hotKeyHandlers
from quickmachotkey._MinimalHIToolbox import (
    GetEventDispatcherTarget, RegisterEventHotKey, UnregisterEventHotKey,
)
from quickmachotkey.constants import cmdKey, controlKey, kVK_ANSI_R

[_SIGNATURE] = unpack("@I", b"QMHK")

# Command-Control-R. Not Command-Shift-R (browser hard reload), not
# Command-Option-R (several apps' "run"), and R for "read".
DEFAULT_KEY = kVK_ANSI_R
DEFAULT_MODIFIERS = cmdKey | controlKey
HOTKEY_DESCRIPTION = "cmd-ctrl-R"

# The one status worth naming: the combination belongs to something else.
ALREADY_REGISTERED = -9878

_slot = 7100
_registered: list = []


class HotkeyUnavailable(RuntimeError):
    """The combination could not be registered."""


def register_read_hotkey(callback: Callable[[], None],
                         *, virtual_key: int = DEFAULT_KEY,
                         modifiers: int = DEFAULT_MODIFIERS,
                         description: str = HOTKEY_DESCRIPTION) -> str:
    """Register the reading hotkey, or raise. Returns a human description.

    The callback runs on the main thread inside the Carbon event dispatcher, so
    it must return immediately; anything slow belongs on another thread.
    """
    global _slot
    _slot += 1
    hotKeyHandlers[_slot] = callback

    status, reference = RegisterEventHotKey(
        virtual_key, modifiers, (_SIGNATURE, _slot),
        GetEventDispatcherTarget(), 0, None,
    )
    if status != 0 or reference is None:
        hotKeyHandlers.pop(_slot, None)
        if status == ALREADY_REGISTERED:
            raise HotkeyUnavailable(
                f"{description} is already taken by another application"
            )
        raise HotkeyUnavailable(f"could not register {description} (status {status})")

    _registered.append(reference)
    return description


def unregister_all() -> None:
    while _registered:
        try:
            UnregisterEventHotKey(_registered.pop())
        except Exception:                 # noqa: BLE001
            pass
