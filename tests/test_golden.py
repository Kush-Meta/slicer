"""The golden set, rendered by WebKit rather than by our own drawing code.

Synthetic fixtures place text where I decided to place it, which tests the
ordering algorithm against my assumptions about layout. These pages are laid
out by the same engine a browser uses, so passing here means passing on a real
screen. Two of the five bugs found during testing were only visible on these.

Assertions check the order of key phrases rather than exact strings, because
recognition on real rendered text is imperfect in ways that are not the
ordering algorithm's fault. "Serchive" instead of "Settings Archive" is an OCR
artefact; reading the right column before the left is a Slicer bug.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer.layout import build_slice                      # noqa: E402
from slicer.ocr import recognize                           # noqa: E402
from tests import golden, webfixtures                      # noqa: E402


def _check(name: str) -> None:
    html, order = golden.PAGES[name]
    path = webfixtures.render_html(html)
    text = " ".join(b.text for b in build_slice(recognize(path)).readable())
    positions = [text.find(phrase) for phrase in order]

    missing = [p for p, i in zip(order, positions) if i < 0]
    assert not missing, f"{name}: never read {missing}\\n  got: {text[:200]}"
    assert positions == sorted(positions), (
        f"{name}: read out of order\\n  expected {order}\\n  got: {text[:200]}"
    )


def test_single_column():
    _check("single_column")


def test_two_column():
    _check("two_column")


def test_header_two_column():
    _check("header_two_column")


def test_sidebar():
    _check("sidebar")


def test_table():
    _check("table")


def test_three_column():
    _check("three_column")


def test_code_and_prose():
    _check("code_and_prose")


if __name__ == "__main__":
    if not webfixtures.available():
        print("  skipped: WebKit bindings not installed")
        sys.exit(0)
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
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
