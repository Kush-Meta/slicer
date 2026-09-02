"""Render real HTML with WebKit, so the corpus is not just my own drawing code.

Synthetic fixtures place text at coordinates I chose, which means they test the
ordering algorithm against my assumptions about layout. WebKit lays pages out
the way a browser actually does - CSS columns, flexbox, real font metrics,
antialiasing - so a page that reads correctly here reads correctly on screen.

The renderer runs in a subprocess because WKWebView needs its own run loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_RENDERER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_render_html.py")


class RenderUnavailable(RuntimeError):
    pass


def render_html(html: str, width: int = 1000, height: int = 620) -> str:
    """Return the path to a PNG of `html` as WebKit draws it."""
    fd, html_path = tempfile.mkstemp(prefix="slicer-page-", suffix=".html")
    os.close(fd)
    with open(html_path, "w") as fh:
        fh.write(html)
    fd, png_path = tempfile.mkstemp(prefix="slicer-page-", suffix=".png")
    os.close(fd)

    proc = subprocess.run(
        [sys.executable, _RENDERER, html_path, png_path, str(width), str(height)],
        capture_output=True, text=True, timeout=60,
    )
    os.unlink(html_path)
    if proc.returncode != 0 or not os.path.getsize(png_path):
        raise RenderUnavailable(proc.stdout.strip() or proc.stderr.strip() or "render failed")
    return png_path


def available() -> bool:
    try:
        import WebKit  # noqa: F401
        return True
    except ImportError:
        return False
