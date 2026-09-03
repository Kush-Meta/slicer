"""The resident Slicer process.

Why this exists: recognition costs 484 ms in a cold process and 33 ms in a warm
one. Almost all of the difference is loading the Vision framework, which is
paid once per process. A resident daemon turns every reading after the first
into the warm case, which is the difference between a tool you wait for and one
that answers.

The architecture is forced by one constraint: **AppKit is main-thread only.**
The region picker and the highlight overlay both need the main run loop, so the
main thread cannot be the one blocking on a socket. Therefore:

    main thread    - AppKit run loop, drains a work queue, owns all UI
    socket thread  - accepts connections, parses requests, enqueues work

Anything touching a window is marshalled to the main thread and waited on.
Recognition and speech do not need the main thread and run wherever they land.

Only one reading happens at a time. A second request supersedes the first
rather than queueing behind it, which is what the narrator's epoch mechanism
already does - pressing the hotkey again while it is reading should start the
new reading, not wait for the old one.
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field

from . import ipc, telemetry
from .capture import CaptureError, capture_file, capture_region
from .conductor import Conductor
from .layout import LayoutConfig
from .narrator import Narrator

# How long a UI call may block the socket thread before we give up on it.
MAIN_THREAD_TIMEOUT = 180.0

# NSEventMaskAny: every event type.
NSEventMaskAny = 0xFFFFFFFFFFFFFFFF


@dataclass
class _Work:
    """A callable that must run on the main thread, and somewhere to put it."""

    fn: callable
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as exc:      # noqa: BLE001 - reported to the client
            self.error = exc
        finally:
            self.done.set()


class Daemon:
    def __init__(self, *, voice: str | None = None, rate: int | None = None,
                 highlight: bool = True):
        self.narrator = Narrator(voice=voice, rate=rate)
        self.conductor = Conductor(narrator=self.narrator, layout_config=LayoutConfig())
        self.highlight = highlight
        self.started = time.time()
        self.readings = 0
        self.hotkey: str | None = None
        self.hotkey_presses = 0
        # Set by a host that wants to reflect reading state - the menu bar app
        # changes its icon and its Read/Stop item from here.
        self.on_reading_state = None
        self.main_queue: queue.Queue[_Work] = queue.Queue()
        self.running = True
        self._server: socket.socket | None = None
        self._overlay = None

    # -- main thread -------------------------------------------------------

    def start_services(self, *, become_app: bool = True) -> bool:
        """Warm Vision, open the socket, register the hotkey. No loop.

        Split out from `run` so a host that already owns the main run loop -
        the menu bar app - can drive the daemon by calling `tick`, rather than
        the daemon insisting on owning the main thread itself.
        """
        ipc.ensure_runtime_dir()
        if ipc.is_running():
            self._log("slicer: a daemon is already running")
            return False
        ipc.clear_stale_socket()

        if become_app:
            self._become_accessory_app()
        self._warm_up()
        self._start_server()
        self._install_hotkey()
        return True

    def tick(self) -> None:
        """Run any queued main-thread work. Safe to call from a timer."""
        self._drain_main_queue()

    def run(self) -> int:
        """Own the main thread: AppKit run loop plus the work queue."""
        if not self.start_services():
            return 1

        from AppKit import NSApplication          # noqa: PLC0415
        from Foundation import NSDate, NSRunLoop   # noqa: PLC0415
        app = NSApplication.sharedApplication()
        loop = NSRunLoop.currentRunLoop()
        self._log(f"slicer daemon ready  (pid {os.getpid()})")
        self._log(f"  socket {ipc.SOCKET_PATH}")
        self._log("  press the hotkey from any app; ctrl-C here to stop")

        try:
            while self.running:
                self._drain_main_queue()
                # Pump the *application* event queue, not just the run loop.
                # Carbon hotkeys are dispatched by HIToolbox from inside
                # ReceiveNextEvent, which NSApplication reaches through
                # nextEventMatchingMask. CFRunLoop alone never dispatches them,
                # so the handler registers successfully and is never called -
                # which is exactly the symptom this fixes.
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(0.02),
                    "kCFRunLoopDefaultMode", True,
                )
                if event is not None:
                    app.sendEvent_(event)
                loop.runMode_beforeDate_(
                    "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.005)
                )
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def _drain_main_queue(self) -> None:
        while True:
            try:
                work = self.main_queue.get_nowait()
            except queue.Empty:
                return
            work.run()

    def on_main(self, fn, timeout: float = MAIN_THREAD_TIMEOUT):
        """Run `fn` on the main thread and return its result."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        work = _Work(fn)
        self.main_queue.put(work)
        if not work.done.wait(timeout):
            raise TimeoutError("the main thread did not respond")
        if work.error is not None:
            raise work.error
        return work.result

    def _become_accessory_app(self) -> None:
        """Register with the window server, without a Dock icon.

        Carbon hotkey delivery is unreliable from a process the window server
        does not know about, and the picker needs an NSApplication anyway. An
        accessory policy means no Dock icon and no menu bar - the same thing a
        status-bar app does with LSUIElement.
        """
        try:
            from AppKit import (  # noqa: PLC0415
                NSApplication, NSApplicationActivationPolicyAccessory,
            )
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            # Without finishLaunching the application never starts servicing its
            # event queue, so nothing is ever there to pump.
            app.finishLaunching()
        except Exception as exc:          # noqa: BLE001
            self._log(f"  could not register with the window server: {exc}")

    def _log(self, message: str) -> None:
        print(message, flush=True)

    def _warm_up(self) -> None:
        """Load Vision now, so the first real reading is a warm one."""
        started = time.perf_counter()
        try:
            from tests.fixtures import render  # noqa: PLC0415
            path = render([], width=32, height=32)
        except Exception:
            return
        try:
            from .ocr import recognize  # noqa: PLC0415
            recognize(path)
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._log(f"  vision warmed in {(time.perf_counter() - started) * 1000:.0f}ms")

    # -- socket thread -----------------------------------------------------

    def _start_server(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(ipc.SOCKET_PATH)
        os.chmod(ipc.SOCKET_PATH, 0o600)
        server.listen(8)
        self._server = server
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while self.running:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            for request in ipc.read_lines(client):
                self._dispatch(request, client)
                break                     # one request per connection
        except (OSError, BrokenPipeError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, request: dict, client: socket.socket) -> None:
        method = request.get("method", "")
        params = request.get("params") or {}
        try:
            if method == "ping":
                ipc.send_line(client, {
                    "ok": True, "pid": os.getpid(),
                    "uptime": round(time.time() - self.started, 1),
                    "readings": self.readings,
                    "hotkey": self.hotkey,
                    "hotkey_presses": self.hotkey_presses,
                })
            elif method == "shutdown":
                ipc.send_line(client, {"ok": True})
                self.running = False
            elif method in ("read", "plan"):
                self._do_reading(method, params, client)
            else:
                ipc.send_line(client, {"ok": False, "error": f"unknown method {method!r}"})
        except CaptureError as exc:
            ipc.send_line(client, {"ok": False, "error": str(exc),
                                   "remedy": getattr(exc, "remedy", "")})
        except Exception as exc:          # noqa: BLE001
            telemetry.record({"event": "daemon_error", "detail": traceback.format_exc()})
            ipc.send_line(client, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # -- work --------------------------------------------------------------

    def _do_reading(self, method: str, params: dict, client: socket.socket) -> None:
        source = self._acquire(params)
        self.readings += 1

        if method == "plan":
            from .editor import Verbosity  # noqa: PLC0415
            self.conductor.verbosity = Verbosity(params.get("verbosity", "low"))
            reading = self.conductor.prepare(source)
            ipc.send_line(client, {
                "ok": True, "blocks": [
                    {"kind": u.kind.value, "prefix": u.prefix, "text": u.text,
                     "note": u.note}
                    for u in reading.utterances
                ],
                "notes": reading.notes,
                "timings": reading.timings.stages,
            })
            return

        def on_progress(progress):
            block = self._block_for(reading_state, progress.utterance.block_id)
            if self.highlight and block is not None:
                self.on_main(lambda: self._show_highlight(block, reading_state))
            try:
                ipc.send_line(client, {
                    "event": "block", "index": progress.index, "total": progress.total,
                    "text": progress.utterance.spoken,
                })
            except OSError:
                self.narrator.stop()      # the client went away; stop talking

        reading_state = {"slice": None, "capture": source}
        if params.get("follow"):
            reading = self.conductor.read_continuous(
                source, on_progress=on_progress,
                on_advance=lambda n: self._advance(n, reading_state, client),
            )
        else:
            reading_state["slice"] = self.conductor.prepare(source).slice
            reading = self.conductor.read_responsive(source, on_progress=on_progress)
        if self.highlight:
            self.on_main(self._hide_highlight)
        ipc.send_line(client, {
            "ok": True, "blocks_read": len(reading.utterances),
            "words": reading.word_count, "notes": reading.notes,
            "timings": reading.timings.stages,
        })

    def _advance(self, screens: int, state: dict, client: socket.socket) -> None:
        try:
            ipc.send_line(client, {"event": "advance", "screen": screens})
        except OSError:
            self.narrator.stop()

    def _acquire(self, params: dict):
        """Resolve what to read.

        Order matters: an explicit target must win over the picker. Forgetting
        to forward `window` and `screen` from the client made every such
        request fall through to the picker, which then waited forever for a
        drag nobody was there to make.
        """
        if params.get("file"):
            return capture_file(params["file"])
        region = params.get("region")
        if region:
            return capture_region(*region)
        if params.get("window"):
            from .capture import capture_window  # noqa: PLC0415
            return capture_window()
        if params.get("screen"):
            from .capture import capture_display  # noqa: PLC0415
            return capture_display()
        # Picking a region needs a window, so it must happen on the main thread.
        from .capture import capture_interactive  # noqa: PLC0415
        return self.on_main(capture_interactive)

    def _block_for(self, state: dict, block_id: str):
        slice_ = state.get("slice")
        if slice_ is None:
            return None
        for block in slice_.blocks:
            if block.id == block_id:
                return block
        return None

    def _show_highlight(self, block, state) -> None:
        from .overlay import Highlight  # noqa: PLC0415
        if self._overlay is None:
            self._overlay = Highlight()
        capture = state.get("capture")
        if capture is not None:
            self._overlay.show(block.box, capture)

    def _hide_highlight(self) -> None:
        if self._overlay is not None:
            self._overlay.hide()

    def _install_hotkey(self) -> None:
        try:
            from .hotkey import register_read_hotkey  # noqa: PLC0415
            description = register_read_hotkey(self._hotkey_pressed)
            self.hotkey = description
            self._log(f"  hotkey {description} reads a region")
        except Exception as exc:          # noqa: BLE001
            self.hotkey = None
            self._log(f"  hotkey unavailable: {exc}")

    def _hotkey_pressed(self) -> None:
        """Fired on the main thread by the Carbon handler.

        Must return immediately - it runs inside the Carbon event dispatcher.
        """
        self.hotkey_presses += 1
        self._log(f"  hotkey pressed ({self.hotkey_presses}) - drag a region")
        threading.Thread(target=self._hotkey_reading, daemon=True).start()

    def _hotkey_reading(self) -> None:
        try:
            self.begin_reading()
        except CaptureError:
            pass

    def begin_reading(self, *, follow: bool = False):
        """Pick a region and read it. The hotkey and the menu both land here.

        Runs on a worker thread; the picker and the highlight are marshalled to
        the main thread by `_acquire` and `_show_highlight`.
        """
        self._announce(True, "Selecting a region…")
        try:
            source = self._acquire({})
        except CaptureError as exc:
            self._announce(False, str(exc)[:60])
            raise

        state = {"capture": source}
        try:
            prepared = self.conductor.prepare(source)
        except CaptureError as exc:
            self._announce(False, str(exc)[:60])
            raise
        state["slice"] = prepared.slice
        self.readings += 1

        def on_progress(progress):
            block = self._block_for(state, progress.utterance.block_id)
            if self.highlight and block is not None:
                self.on_main(lambda: self._show_highlight(block, state))

        self._announce(True, f"Reading {len(prepared.utterances)} blocks")
        try:
            if follow:
                return self.conductor.read_continuous(source, on_progress=on_progress)
            return self.conductor.read_responsive(source, on_progress=on_progress)
        finally:
            if self.highlight:
                self.on_main(self._hide_highlight)
            self._announce(False, "Ready")

    def _announce(self, reading: bool, detail: str = "") -> None:
        if self.on_reading_state is None:
            return
        # State changes drive UI, so they belong on the main thread.
        try:
            self.on_main(lambda: self.on_reading_state(reading, detail), timeout=5)
        except Exception:                 # noqa: BLE001
            pass

    # -- teardown ----------------------------------------------------------

    def shutdown(self) -> None:
        self.running = False
        self.narrator.stop()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        ipc.clear_stale_socket()
