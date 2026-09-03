"""Generate the demo animation on the README from real pipeline output.

Nothing here is a mockup. The page is rendered by WebKit, run through the same
capture, recognition, layout and editor stages a real reading uses, and each
frame highlights the block Slicer actually chose to read next, captioned with
the words it would actually speak. If the ordering changes, this animation
changes with it.

What it cannot show is the sound, which is the other half of the product. A
recording with audio needs a virtual audio device and a person to press the
hotkey.

    ./.venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont

from slicer.blocks import BlockKind
from slicer.capture import capture_file
from slicer.editor import Verbosity, to_speech
from slicer.layout import build_slice
from slicer.ocr import recognize
from tests import webfixtures

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "reading-order.gif")

WIDTH, HEIGHT = 940, 320
CAPTION_H = 96
ACCENT = (69, 191, 200)
DIM_ALPHA = 165
SKIPPED_GREY = (150, 158, 166)
FRAME_MS = 1500

# A layout Slicer verifiably handles: a full-width heading, a navigation
# sidebar, and two columns of running text. This is the shape the golden set
# covers and the ordering is checked by tests, so the animation cannot quietly
# start showing something wrong.
#
# It is deliberately not the hardest possible page. A table or a code block
# sitting *below* two columns is read out of order today, because geometry
# cannot tell that narrow content under a column is not part of it. That limit
# is stated in the README rather than hidden by the choice of demo.
PAGE = """
<html><head><meta charset="utf-8"><style>
 body{font:16px Georgia,serif;margin:0;padding:24px;width:%dpx;background:#fff;color:#111}
 h1{font-size:28px;margin:0 0 20px}
 nav div{font-size:13px;padding:7px 0;color:#555}
 p{margin:0 0 14px;line-height:1.5}
</style></head><body>
 <h1>Quarterly Engineering Review</h1>
 <div style="display:flex;gap:38px">
  <nav style="width:108px"><div>Home</div><div>Reports</div><div>Settings</div>
       <div>Archive</div><div>Billing</div></nav>
  <div style="width:716px;column-count:2;column-gap:40px">
   <p>Latency improved across every service this quarter, with the median
   request now completing in well under forty milliseconds, down from one
   hundred and twelve at the start of the year.</p>
   <p>Throughput rose in step, and no service exceeded its error budget.</p>
   <p>Reliability stayed flat. Two incidents were traced to the same expired
   certificate, which now rotates automatically ahead of expiry.</p>
   <p>The remaining work is the alerting rewrite, which lands next quarter.</p>
  </div>
 </div>
</body></html>
""" % (WIDTH - 48)


def _font(size: int, bold: bool = False):
    for path in ("/System/Library/Fonts/SFNSMono.ttf",
                 "/System/Library/Fonts/Supplemental/Menlo.ttc",
                 "/System/Library/Fonts/Monaco.ttf"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def build() -> str:
    path = webfixtures.render_html(PAGE, width=WIDTH, height=HEIGHT)
    capture = capture_file(path)
    slice_ = build_slice(recognize(capture.source))
    blocks = slice_.readable()
    skipped = slice_.skipped()
    print(f"  {len(blocks)} blocks to read, {len(skipped)} skipped as navigation")

    page = Image.open(path).convert("RGB").resize((WIDTH, HEIGHT), Image.LANCZOS)
    scale_x = WIDTH / capture.width
    scale_y = HEIGHT / capture.height

    caption_font = _font(15)
    label_font = _font(12)
    frames = []

    for index, block in enumerate(blocks, 1):
        utterance = to_speech(block, verbosity=Verbosity.LOW,
                              index=index, total=len(blocks))
        if utterance is None:
            continue

        frame = Image.new("RGB", (WIDTH, HEIGHT + CAPTION_H), (14, 17, 22))
        shade = Image.new("RGBA", (WIDTH, HEIGHT), (10, 14, 18, DIM_ALPHA))
        base = page.copy().convert("RGBA")
        base.alpha_composite(shade)

        box = block.box
        x0, y0 = box.x * scale_x, box.y * scale_y
        x1, y1 = box.x2 * scale_x, box.y2 * scale_y
        pad = 5
        region = (max(0, int(x0 - pad)), max(0, int(y0 - pad)),
                  min(WIDTH, int(x1 + pad)), min(HEIGHT, int(y1 + pad)))
        # Punch the block being read back to full brightness.
        base.paste(page.crop(region), region)

        draw = ImageDraw.Draw(base)
        # Show what was deliberately not read, so the navigation being silent
        # reads as a decision rather than a failure.
        for other in skipped:
            b = other.box
            draw.rectangle((int(b.x * scale_x) - 3, int(b.y * scale_y) - 2,
                            int(b.x2 * scale_x) + 3, int(b.y2 * scale_y) + 2),
                           outline=SKIPPED_GREY + (170,), width=1)
        draw.rectangle(region, outline=ACCENT + (255,), width=3)
        frame.paste(base.convert("RGB"), (0, 0))

        draw = ImageDraw.Draw(frame)
        kind = block.kind.value.replace("_", " ")
        draw.text((18, HEIGHT + 16), f"{index}/{len(blocks)}   {kind}",
                  font=label_font, fill=(120, 200, 205))
        if skipped:
            note = f"{len(skipped)} blocks skipped as navigation"
            draw.text((WIDTH - 18 - draw.textlength(note, font=label_font),
                       HEIGHT + 16), note, font=label_font, fill=(122, 135, 146))
        spoken = utterance.spoken
        if len(spoken) > 96:
            spoken = spoken[:93] + "..."
        draw.text((18, HEIGHT + 42), spoken, font=caption_font, fill=(228, 234, 240))
        draw.line([(0, HEIGHT), (WIDTH, HEIGHT)], fill=(40, 52, 62), width=1)
        frames.append(frame)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=FRAME_MS, loop=0, optimize=True)
    os.unlink(path)
    return OUT


if __name__ == "__main__":
    out = build()
    print(f"  wrote {out}  ({os.path.getsize(out) / 1024:.0f} KB)")
