"""Slicer as a menu bar application.

Two reasons this exists, and only one of them is cosmetic.

The visible one: a resident process with no interface is impossible to trust.
There is no way to tell whether it is running, no way to stop it without a
terminal, and no way to see what it just did. A status item fixes all three.

The structural one: `rumps` runs a real `NSApplication` event loop. That is the
loop Carbon dispatches global hotkeys from, and the loop AppKit needs for the
region picker and the highlight. The daemon's hand-rolled loop had to
reimplement that and got it wrong once already - the hotkey registered and
never fired. Handing the main loop to a library that does it properly removes a
class of bug rather than fixing an instance of it.

The daemon keeps its own headless loop for `slicer daemon`, which is still the
right thing for a server or a test. Here it only supplies `start_services` and
`tick`.
"""

from __future__ import annotations

import os
import subprocess
import threading

import rumps

from . import ipc
from .capture import CaptureError
from .daemon import Daemon

IDLE = "◉"
READING = "◐"
BUSY = "◒"

MENU_READ = "Read a region"
MENU_FOLLOW = "Keep reading as it scrolls"
MENU_HIGHLIGHT = "Show the highlight"
MENU_LOG = "Open log"


class SlicerApp(rumps.App):
    def __init__(self, *, voice: str | None = None, rate: int | None = None):
        super().__init__(IDLE, quit_button=None)
        self.daemon = Daemon(voice=voice, rate=rate, highlight=True)
        self.follow = False
        self._reading = False

        self.read_item = rumps.MenuItem(MENU_READ, callback=self.on_read)
        self.follow_item = rumps.MenuItem(MENU_FOLLOW, callback=self.on_toggle_follow)
        self.highlight_item = rumps.MenuItem(MENU_HIGHLIGHT, callback=self.on_toggle_highlight)
        self.highlight_item.state = 1
        self.status_item = rumps.MenuItem("Starting…", callback=None)

        self.menu = [
            self.status_item,
            None,
            self.read_item,
            self.follow_item,
            None,
            self.highlight_item,
            rumps.MenuItem(MENU_LOG, callback=self.on_open_log),
            None,
            rumps.MenuItem("Quit Slicer", callback=self.on_quit),
        ]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # rumps already owns an NSApplication, so the daemon must not create a
        # second one or set the activation policy underneath it.
        if not self.daemon.start_services(become_app=False):
            rumps.alert("Slicer", "Another Slicer is already running.")
            rumps.quit_application()
            return
        hotkey = self.daemon.hotkey
        self.read_item.title = f"{MENU_READ}  ({hotkey})" if hotkey else MENU_READ
        self._set_status(f"Ready · {hotkey}" if hotkey else "Ready · no hotkey")
        self.daemon.on_reading_state = self._on_reading_state

    @rumps.timer(0.02)
    def _pump(self, _) -> None:
        """Drain work the socket threads need done on the main thread."""
        self.daemon.tick()

    # -- menu actions ------------------------------------------------------

    def on_read(self, _) -> None:
        if self._reading:
            self.daemon.narrator.stop()
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            self.daemon.begin_reading(follow=self.follow)
        except CaptureError as exc:
            self._set_status(str(exc)[:60])

    def on_toggle_follow(self, item) -> None:
        self.follow = not self.follow
        item.state = 1 if self.follow else 0

    def on_toggle_highlight(self, item) -> None:
        self.daemon.highlight = not self.daemon.highlight
        item.state = 1 if self.daemon.highlight else 0
        if not self.daemon.highlight:
            self.daemon.on_main(self.daemon._hide_highlight)

    def on_open_log(self, _) -> None:
        path = os.path.join(ipc.RUNTIME_DIR, "daemon.log")
        if not os.path.exists(path):
            rumps.alert("Slicer", "No log yet.")
            return
        subprocess.run(["open", "-a", "Console", path], check=False)

    def on_quit(self, _) -> None:
        self.daemon.shutdown()
        rumps.quit_application()

    # -- state -------------------------------------------------------------

    def _on_reading_state(self, reading: bool, detail: str = "") -> None:
        self._reading = reading
        self.title = READING if reading else IDLE
        self.read_item.title = (
            "Stop reading" if reading
            else (f"{MENU_READ}  ({self.daemon.hotkey})" if self.daemon.hotkey
                  else MENU_READ)
        )
        if detail:
            self._set_status(detail)

    def _set_status(self, text: str) -> None:
        self.status_item.title = text


def main(voice: str | None = None, rate: int | None = None) -> int:
    app = SlicerApp(voice=voice, rate=rate)
    app.start()
    app.run()
    return 0
