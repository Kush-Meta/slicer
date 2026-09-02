"""IPC protocol, and the daemon end to end.

The daemon test really spawns the process, because the thing worth verifying is
exactly what a fake would paper over: that AppKit lives on the main thread, the
socket server on another, and requests crossing between them still work.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer import ipc                                     # noqa: E402
from tests import fixtures                                 # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- protocol --------------------------------------------------------------

def test_lines_round_trip():
    a, b = socket.socketpair()
    try:
        ipc.send_line(a, {"method": "ping", "params": {"n": 1}})
        ipc.send_line(a, {"method": "stop"})
        a.close()
        assert list(ipc.read_lines(b)) == [
            {"method": "ping", "params": {"n": 1}}, {"method": "stop"}]
    finally:
        b.close()


def test_a_message_split_across_packets_is_reassembled():
    a, b = socket.socketpair()
    try:
        payload = ('{"method": "read", "params": {"text": "'
                   + "x" * 5000 + '"}}\n').encode()
        for i in range(0, len(payload), 512):
            a.sendall(payload[i:i + 512])
        a.close()
        messages = list(ipc.read_lines(b))
        assert len(messages) == 1 and messages[0]["method"] == "read"
    finally:
        b.close()


def test_malformed_lines_are_skipped_not_fatal():
    a, b = socket.socketpair()
    try:
        a.sendall(b'not json\n{"method": "ping"}\n')
        a.close()
        assert list(ipc.read_lines(b)) == [{"method": "ping"}]
    finally:
        b.close()


def test_a_stale_socket_file_is_cleared():
    """A crashed daemon leaves a socket behind; it must not block the next start."""
    ipc.ensure_runtime_dir()
    original = ipc.SOCKET_PATH
    fd, path = tempfile.mkstemp(prefix="slicer-stale-", suffix=".sock")
    os.close(fd)
    ipc.SOCKET_PATH = path
    try:
        assert os.path.exists(path)
        assert ipc.connect(0.2) is None     # nothing is listening
        assert not os.path.exists(path)     # so it was cleared
    finally:
        ipc.SOCKET_PATH = original


def test_the_runtime_directory_is_private():
    path = ipc.ensure_runtime_dir()
    assert oct(os.stat(path).st_mode & 0o777) == "0o700"


# -- the real daemon -------------------------------------------------------

def _wait_until_ready(deadline: float = 25.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if ipc.is_running():
            return True
        time.sleep(0.3)
    return False


def test_daemon_serves_a_reading_plan():
    if ipc.is_running():
        subprocess.run([os.path.join(ROOT, "bin", "slicer"), "stop"], capture_output=True)
        time.sleep(1.0)

    page, _ = fixtures.two_column(
        ["Alpha the left column begins", "and runs on for a line or two"],
        ["Bravo the right column follows", "once the left one has finished"])

    proc = subprocess.Popen(
        [os.path.join(ROOT, "bin", "slicer"), "daemon", "--no-highlight"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    try:
        assert _wait_until_ready(), "daemon never became ready"

        stream = ipc.request({"method": "plan", "params": {"file": page}})
        assert stream is not None
        replies = list(stream)
        final = [r for r in replies if r.get("ok")]
        assert final, f"no successful reply: {replies}"

        text = " ".join(b["text"] for b in final[0]["blocks"])
        # The daemon must produce the same reading order as the in-process path.
        assert text.index("Alpha") < text.index("Bravo")

        pings = list(ipc.request({"method": "ping"}))
        assert pings and pings[0]["readings"] >= 1
    finally:
        subprocess.run([os.path.join(ROOT, "bin", "slicer"), "stop"], capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_socket_is_removed_on_shutdown():
    assert not os.path.exists(ipc.SOCKET_PATH), "a socket file outlived the daemon"


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    # the shutdown check only means something after the daemon test
    tests.sort(key=lambda pair: pair[0] == "test_the_socket_is_removed_on_shutdown")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  \033[32mpass\033[0m  {name}")
        except Exception as exc:
            failed.append(name)
            print(f"  \033[31mFAIL\033[0m  {name}: {exc}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
