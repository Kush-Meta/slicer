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

We use the Carbon route, through `quickmachotkey`, which wraps it properly.
Modifier-only shortcuts are the one thing it cannot express; those need an
event tap, and we would rather not have a shortcut than have that permission.
"""

from __future__ import annotations

from typing import Callable

from quickmachotkey import mask, quickHotKey
from quickmachotkey.constants import cmdKey, controlKey, kVK_ANSI_R

# Command-Control-R. Not Command-Shift-R (browser hard reload), not
# Command-Option-R (several apps' "run"), and R for "read".
HOTKEY_DESCRIPTION = "cmd-ctrl-R"

_registered = []


def register_read_hotkey(callback: Callable[[], None]) -> str:
    """Register the reading hotkey. Returns a human description of it.

    The handler runs on the main thread, in the Carbon event dispatcher, so it
    must return immediately - anything slow belongs on another thread.
    """

    @quickHotKey(virtualKey=kVK_ANSI_R, modifierMask=mask(cmdKey, controlKey))
    def _handler() -> None:
        callback()

    _registered.append(_handler)          # keep it alive; GC would unregister it
    return HOTKEY_DESCRIPTION
