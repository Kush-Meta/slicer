"""Local IPC between the Slicer client and the resident daemon.

Newline-delimited JSON over a Unix domain socket. This is the shape Core
Lightning and signal-cli use for the same job, and it is the right one here:

  * A Unix socket cannot be reached from the network, at all. A TCP port on
    localhost can be, by anything else on the machine. Slicer moves screen
    contents around, so the transport should not be addressable.
  * Filesystem permissions are the access control. The socket lives in a
    0700 directory owned by the user; no tokens, no handshake.
  * No port to collide with, and a stale socket file is trivially detectable.

Newline-delimited JSON rather than a framed JSON-RPC library because responses
are *streamed*: a reading emits a progress line per block as it is spoken, and
the client prints them live. Request/response framing would have to be worked
around to do that.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Iterator

RUNTIME_DIR = os.path.expanduser("~/.slicer")
SOCKET_PATH = os.path.join(RUNTIME_DIR, "daemon.sock")
# Long enough that a slow first recognition does not look like a dead daemon,
# short enough that a hung one does not hang the client.
CONNECT_TIMEOUT = 2.0


def ensure_runtime_dir() -> str:
    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    return RUNTIME_DIR


def send_line(sock: socket.socket, payload: dict) -> None:
    sock.sendall((json.dumps(payload) + "\n").encode())


def read_lines(sock: socket.socket) -> Iterator[dict]:
    """Yield each JSON object the peer sends, until it closes."""
    buffer = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def connect(timeout: float = CONNECT_TIMEOUT) -> socket.socket | None:
    """Connect to a running daemon, or None if there isn't one."""
    if not os.path.exists(SOCKET_PATH):
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(SOCKET_PATH)
        return sock
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        sock.close()
        # A socket file with nothing behind it is a crashed daemon, not a
        # running one. Clear it so the next start is not blocked.
        clear_stale_socket()
        return None


def clear_stale_socket() -> None:
    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass


def request(payload: dict, timeout: float = CONNECT_TIMEOUT) -> Iterator[dict] | None:
    """Send one request and stream the replies. None if no daemon is running."""
    sock = connect(timeout)
    if sock is None:
        return None
    return _exchange(sock, payload)


def _exchange(sock: socket.socket, payload: dict) -> Iterator[dict]:
    try:
        sock.settimeout(None)      # a reading takes as long as it takes
        send_line(sock, payload)
        yield from read_lines(sock)
    finally:
        sock.close()


def is_running() -> bool:
    sock = connect(0.5)
    if sock is None:
        return False
    try:
        send_line(sock, {"method": "ping"})
        for reply in read_lines(sock):
            return bool(reply.get("ok"))
        return False
    except OSError:
        return False
    finally:
        sock.close()
