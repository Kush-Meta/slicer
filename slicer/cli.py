"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import time
import termios
import threading
import tty

from . import telemetry
from .blocks import BlockKind
from .capture import CaptureError
from .conductor import Conductor, capture_for
from .layout import LayoutConfig
from .narrator import Narrator

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, RED = "\033[36m", "\033[33m", "\033[31m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slicer", description="Read a region of the screen aloud, in a sensible order."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in [("read", "capture a region and read it aloud"),
                            ("plan", "show what would be read, without speaking")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--region", help="x,y,w,h in screen points (default: drag to select)")
        p.add_argument("--file", help="read an image file instead of the screen")
        p.add_argument("--voice", help="a macOS voice name, see: say -v '?'")
        p.add_argument("--rate", type=int, help="words per minute")
        p.add_argument("--fast", action="store_true", help="faster, less accurate recognition")
        p.add_argument("--show-skipped", action="store_true", help="list what was not read")
        p.add_argument("--timings", action="store_true", help="print the stage breakdown")
        p.add_argument("--follow", action="store_true",
                       help="keep reading as the content scrolls")
        p.add_argument("--window", action="store_true",
                       help="read the frontmost window, no selection needed")
        p.add_argument("--screen", action="store_true",
                       help="read the whole display")
        p.add_argument("--verbosity", choices=["off", "low", "high"], default="low",
                       help="how much structure to announce (default: low)")

    daemon_parser = sub.add_parser("daemon", help="run the resident Slicer process")
    daemon_parser.add_argument("--voice")
    daemon_parser.add_argument("--rate", type=int)
    daemon_parser.add_argument("--no-highlight", action="store_true",
                               help="do not draw the on-screen highlight")
    daemon_parser.add_argument("--background", action="store_true",
                               help="detach and log to ~/.slicer/daemon.log")

    menubar_parser = sub.add_parser("menubar", help="run Slicer in the menu bar")
    menubar_parser.add_argument("--voice")
    menubar_parser.add_argument("--rate", type=int)

    sub.add_parser("status", help="is a daemon running?")
    sub.add_parser("stop", help="shut down the running daemon")
    sub.add_parser("doctor", help="check this machine and measure the latency budget")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        from .doctor import run as run_doctor
        return run_doctor()
    if args.command == "daemon":
        if args.background:
            return _start_background(args)
        from .daemon import Daemon
        return Daemon(voice=args.voice, rate=args.rate,
                      highlight=not args.no_highlight).run()
    if args.command == "menubar":
        from .menubar import main as run_menubar
        return run_menubar(voice=args.voice, rate=args.rate)
    if args.command == "status":
        return _status()
    if args.command == "stop":
        return _stop()

    # Prefer the resident daemon: recognition is 15x faster in a warm process.
    # Falling back in-process means the CLI always works, daemon or not.
    forwarded = _via_daemon(args)
    if forwarded is not None:
        return forwarded

    from .editor import Verbosity  # noqa: PLC0415
    conductor = Conductor(
        narrator=Narrator(voice=getattr(args, "voice", None), rate=getattr(args, "rate", None)),
        layout_config=LayoutConfig(),
        fast_ocr=args.fast,
        verbosity=Verbosity(args.verbosity),
    )

    # Two screen readers talking at once is unusable, and the user cannot see a
    # dialog to find out why. Say it plainly before anything else happens.
    from .windows import voiceover_running  # noqa: PLC0415
    if voiceover_running():
        print(f"  {YELLOW}note{RESET} VoiceOver is running. Both will speak at once.\n"
              f"       {DIM}Silence VoiceOver with control, or use --verbosity off.{RESET}")

    try:
        source = capture_for(args.region, args.file,
                             window=args.window, screen=args.screen)
    except CaptureError as exc:
        return _fail(exc)

    try:
        reading = conductor.prepare(source)
    except (CaptureError, Exception) as exc:
        if isinstance(exc, CaptureError):
            return _fail(exc)
        raise

    _print_header(reading, args)

    if args.command == "plan":
        for index, utterance in enumerate(reading.utterances, 1):
            label = utterance.kind.value
            note = f"  {YELLOW}{utterance.note}{RESET}" if utterance.note else ""
            narration = f"{DIM}{utterance.prefix}{RESET} " if utterance.prefix else ""
            print(f"  {DIM}{index:2d}{RESET} {CYAN}{label:<8}{RESET} {narration}{utterance.text}{note}")
        if args.show_skipped:
            _print_skipped(reading)
        if args.timings:
            print(f"\n{DIM}{reading.timings.render()}{RESET}")
        return 0

    print(f"{DIM}space pause   n next   p previous   q quit{RESET}\n")
    stop = threading.Event()
    listener = threading.Thread(
        target=_transport, args=(conductor.narrator, stop), daemon=True
    )
    listener.start()

    def on_progress(progress):
        prefix = f"{DIM}{progress.index + 1:2d}/{progress.total}{RESET}"
        print(f"  {prefix} {progress.utterance.spoken}")

    try:
        if args.follow:
            from .continuity import accessibility_granted  # noqa: PLC0415
            if not accessibility_granted():
                print(f"  {YELLOW}note{RESET} automatic scrolling needs Accessibility "
                      f"permission.\n       {DIM}System Settings > Privacy & Security > "
                      f"Accessibility. Until then, scroll\n       yourself and Slicer will "
                      f"pick up where it left off.{RESET}\n")
            reading = conductor.read_continuous(
                source, on_progress=on_progress,
                on_advance=lambda n: print(f"  {DIM}--- screen {n} ---{RESET}"),
            )
            for note in reading.notes:
                print(f"  {YELLOW}note{RESET} {note}")
        else:
            conductor.read(source, on_progress=on_progress)
    except KeyboardInterrupt:
        conductor.narrator.stop()
        print(f"\n{DIM}stopped{RESET}")
    finally:
        stop.set()

    if args.show_skipped:
        _print_skipped(reading)
    if args.timings:
        print(f"\n{DIM}{reading.timings.render()}{RESET}")
    return 0


def _via_daemon(args) -> int | None:
    """Send this command to the daemon, or None if there isn't one."""
    from . import ipc  # noqa: PLC0415

    region = None
    if args.region:
        try:
            region = [int(part) for part in args.region.split(",")]
        except ValueError:
            return None
    payload = {"method": args.command,
               "params": {"region": region, "file": args.file,
                          "follow": getattr(args, "follow", False)}}
    stream = ipc.request(payload)
    if stream is None:
        return None

    print(f"\n{DIM}via daemon{RESET}")
    failed = False
    for message in stream:
        if message.get("event") == "block":
            print(f"  {DIM}{message['index'] + 1:2d}/{message['total']}{RESET} "
                  f"{message['text']}")
        elif message.get("event") == "advance":
            print(f"  {DIM}--- screen {message['screen']} ---{RESET}")
        elif message.get("ok") is False:
            failed = True
            print(f"{RED}slicer:{RESET} {message.get('error')}", file=sys.stderr)
            if message.get("remedy"):
                print(f"\n{message['remedy']}", file=sys.stderr)
        elif message.get("ok"):
            for index, block in enumerate(message.get("blocks", []), 1):
                narration = f"{DIM}{block['prefix']}{RESET} " if block["prefix"] else ""
                print(f"  {DIM}{index:2d}{RESET} {CYAN}{block['kind']:<10}{RESET} "
                      f"{narration}{block['text']}")
            for note in message.get("notes", []):
                print(f"  {YELLOW}note{RESET} {note}")
            if args.timings and message.get("timings"):
                spent = "  ".join(f"{k} {v:.0f}ms" for k, v in message["timings"].items())
                print(f"\n{DIM}{spent}{RESET}")
    return 1 if failed else 0


def _start_background(args) -> int:
    """Re-exec the daemon detached, logging to a file.

    Re-exec rather than fork: forking a process that has initialised AppKit is
    not safe, and the daemon initialises it immediately.
    """
    import subprocess  # noqa: PLC0415
    from . import ipc  # noqa: PLC0415

    if ipc.is_running():
        print(f"{DIM}a daemon is already running{RESET}")
        return 0

    ipc.ensure_runtime_dir()
    log_path = os.path.join(ipc.RUNTIME_DIR, "daemon.log")
    command = [sys.executable, "-m", "slicer.cli", "daemon"]
    if args.voice:
        command += ["--voice", args.voice]
    if args.rate:
        command += ["--rate", str(args.rate)]
    if args.no_highlight:
        command.append("--no-highlight")

    with open(log_path, "a") as log:
        subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    for _ in range(60):
        if ipc.is_running():
            print(f"{BOLD}daemon started{RESET}  {DIM}log: {log_path}{RESET}")
            return 0
        time.sleep(0.5)
    print(f"{RED}slicer:{RESET} the daemon did not come up. See {log_path}",
          file=sys.stderr)
    return 1


def _status() -> int:
    from . import ipc  # noqa: PLC0415
    stream = ipc.request({"method": "ping"})
    if stream is None:
        print(f"{DIM}no daemon running{RESET}  "
              f"start one with: ./bin/slicer daemon")
        return 1
    for message in stream:
        hotkey = message.get("hotkey")
        print(f"{BOLD}daemon running{RESET}  pid {message.get('pid')}  "
              f"up {message.get('uptime')}s  {message.get('readings')} readings")
        if hotkey:
            print(f"  hotkey {CYAN}{hotkey}{RESET}  "
                  f"{DIM}pressed {message.get('hotkey_presses', 0)} time(s) "
                  f"since start{RESET}")
        else:
            print(f"  {YELLOW}no hotkey registered{RESET}")
        return 0
    return 1


def _stop() -> int:
    from . import ipc  # noqa: PLC0415
    stream = ipc.request({"method": "shutdown"})
    if stream is None:
        print(f"{DIM}no daemon running{RESET}")
        return 1
    for _ in stream:
        break
    print("daemon stopped")
    return 0


def _print_header(reading, args) -> None:
    blocks = len(reading.utterances)
    label = getattr(reading, "label", "") or ""
    target = f"{label} \u00b7 " if label else ""
    print(f"\n{BOLD}{target}{blocks} blocks, {reading.word_count} words{RESET}"
          f"  {DIM}to first word {reading.timings.to_first_word():.0f}ms{RESET}")
    for note in reading.notes:
        print(f"  {YELLOW}note{RESET} {note}")
    print()


def _print_skipped(reading) -> None:
    skipped = reading.slice.skipped()
    if not skipped:
        print(f"\n{DIM}nothing was skipped{RESET}")
        return
    print(f"\n{DIM}skipped:{RESET}")
    for block in skipped:
        print(f"  {DIM}-{RESET} {block.text[:60]!r}  {DIM}{block.reason}{RESET}")


def _transport(narrator: Narrator, stop: threading.Event) -> None:
    """Read single keypresses without waiting for a newline."""
    if not sys.stdin.isatty():
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            key = sys.stdin.read(1)
            if key == " ":
                narrator.toggle_pause()
            elif key == "n":
                narrator.skip(1)
            elif key == "p":
                narrator.skip(-1)
            elif key in ("q", "\x03"):
                narrator.stop()
                return
    except Exception:
        return
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass


def _fail(exc: CaptureError) -> int:
    print(f"{RED}slicer:{RESET} {exc}", file=sys.stderr)
    remedy = getattr(exc, "remedy", "")
    if remedy:
        print(f"\n{remedy}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
