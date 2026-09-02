"""Deterministic page images for testing.

Rendering fixtures rather than checking in screenshots means the reading-order
tests state their own ground truth: the generator knows the correct order
because it placed the text.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from AppKit import (
    NSBitmapImageRep, NSColor, NSDeviceRGBColorSpace, NSFont,
    NSFontAttributeName, NSForegroundColorAttributeName,
    NSGraphicsContext, NSMakePoint, NSString,
)


@dataclass
class Placed:
    text: str
    x: int
    y: int
    size: int


def render(placed: list[Placed], width: int = 1000, height: int = 700,
           path: str | None = None, dark: bool = False) -> str:
    """Draw text at given positions (origin top-left) and return the PNG path."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0,
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    bg = NSColor.blackColor() if dark else NSColor.whiteColor()
    fg = NSColor.whiteColor() if dark else NSColor.blackColor()
    bg.setFill()
    from AppKit import NSRectFill, NSMakeRect
    NSRectFill(NSMakeRect(0, 0, width, height))

    for item in placed:
        font = NSFont.fontWithName_size_("Helvetica", item.size) or NSFont.systemFontOfSize_(item.size)
        attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: fg}
        # Callers think top-down; AppKit draws bottom-up.
        flipped_y = height - item.y - item.size
        NSString.stringWithString_(item.text).drawAtPoint_withAttributes_(
            NSMakePoint(item.x, flipped_y), attrs
        )

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(4, {})  # 4 = PNG
    if path is None:
        fd, path = tempfile.mkstemp(prefix="slicer-fixture-", suffix=".png")
        os.close(fd)
    data.writeToFile_atomically_(path, True)
    return path


def single_column(lines: list[str], *, x: int = 80, top: int = 60,
                  leading: int = 34, size: int = 20, **kw) -> tuple[str, list[str]]:
    placed = [Placed(t, x, top + i * leading, size) for i, t in enumerate(lines)]
    return render(placed, **kw), list(lines)


def two_column(left: list[str], right: list[str], *, width: int = 1000,
               gutter_x: int = 540, top: int = 60, leading: int = 34,
               size: int = 20, **kw) -> tuple[str, list[str]]:
    """Two columns. Correct reading order is all of left, then all of right."""
    placed = [Placed(t, 70, top + i * leading, size) for i, t in enumerate(left)]
    placed += [Placed(t, gutter_x, top + i * leading, size) for i, t in enumerate(right)]
    return render(placed, width=width, **kw), list(left) + list(right)


def header_two_column(header: str, left: list[str], right: list[str], *,
                      width: int = 1000, gutter_x: int = 540,
                      size: int = 20, **kw) -> tuple[str, list[str]]:
    """A full-width header above two columns - the layout naive sorting breaks on."""
    placed = [Placed(header, 70, 50, 30)]
    placed += [Placed(t, 70, 130 + i * 34, size) for i, t in enumerate(left)]
    placed += [Placed(t, gutter_x, 130 + i * 34, size) for i, t in enumerate(right)]
    return render(placed, width=width, **kw), [header] + list(left) + list(right)


def sidebar_and_body(nav: list[str], body: list[str], *, width: int = 1000,
                     size: int = 20, **kw) -> tuple[str, list[str]]:
    """A narrow nav column beside content. Correct behaviour is to skip the nav."""
    placed = [Placed(t, 30, 70 + i * 40, 17) for i, t in enumerate(nav)]
    placed += [Placed(t, 300, 70 + i * 34, size) for i, t in enumerate(body)]
    return render(placed, width=width, **kw), list(body)


def crop(path: str, x: int, y: int, w: int, h: int) -> str:
    """Crop a PNG. Used to simulate scrolling over a tall page."""
    import Quartz
    from Foundation import NSURL

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    image = Quartz.CGImageCreateWithImageInRect(
        Quartz.CGImageSourceCreateImageAtIndex(source, 0, None),
        Quartz.CGRectMake(x, y, w, h),
    )
    fd, out = tempfile.mkstemp(prefix="slicer-crop-", suffix=".png")
    os.close(fd)
    destination = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(out), "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(destination, image, None)
    Quartz.CGImageDestinationFinalize(destination)
    return out


def tall_page(paragraphs: list[str], *, width: int = 900, leading: int = 56,
              top: int = 40, size: int = 20) -> tuple[str, int]:
    """A page taller than any one viewport, for continuity tests.

    Leading is deliberately wider than the line height so paragraphs come out
    as separate blocks, the way real body text does.
    """
    placed = [Placed(t, 60, top + i * leading, size) for i, t in enumerate(paragraphs)]
    height = top + len(paragraphs) * leading + 40
    return render(placed, width=width, height=height), height
