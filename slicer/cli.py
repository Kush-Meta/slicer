"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
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

    sub.add_parser("doctor", help="check this machine and measure the latency budget")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        from .doctor import run as run_doctor
        return run_doctor()

    conductor = Conductor(
        narrator=Narrator(voice=getattr(args, "voice", None), rate=getattr(args, "rate", None)),
        layout_config=LayoutConfig(),
        fast_ocr=args.fast,
    )

    try:
        source = capture_for(args.region, args.file)
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


def _print_header(reading, args) -> None:
    blocks = len(reading.utterances)
    print(f"\n{BOLD}{blocks} blocks, {reading.word_count} words{RESET}"
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
