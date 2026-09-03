"""Where the time actually goes, stage by stage, on realistic content."""
from __future__ import annotations
import os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from slicer import capture as cap, layout, ocr
from slicer.editor import Verbosity, to_speech
from slicer.speech import AVSpeechBackend
from slicer.windows import frontmost_window

def bench(fn, n=7):
    times = []
    for _ in range(n):
        t = time.perf_counter(); fn(); times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times), min(times)

win = frontmost_window()
print(f"target: {win.label}  {win.w}x{win.h} points\n")

# 1. capture, the way we do it now
med, best = bench(lambda: os.unlink(cap.capture_region(*win.region, stability_check=False).path))
print(f"  capture  screencapture subprocess      {med:6.0f} ms   (best {best:.0f})")

# 2. capture, in-process via CoreGraphics
import Quartz
def cg_capture():
    img = Quartz.CGWindowListCreateImage(
        Quartz.CGRectMake(*win.region), Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
    return img
try:
    med_cg, best_cg = bench(cg_capture)
    img = cg_capture()
    print(f"  capture  CGWindowListCreateImage     {med_cg:6.0f} ms   (best {best_cg:.0f})"
          f"  -> {Quartz.CGImageGetWidth(img)}x{Quartz.CGImageGetHeight(img)}")
except Exception as e:
    print(f"  capture  CGWindowListCreateImage     failed: {e}")

# 3. recognition, on a real capture, accurate vs fast
c = cap.capture_region(*win.region, stability_check=False)
ocr.recognize(c.path)                     # warm
med_a, _ = bench(lambda: ocr.recognize(c.path), 5)
med_f, _ = bench(lambda: ocr.recognize(c.path, fast=True), 5)
res = ocr.recognize(c.path)
print(f"  ocr      accurate, full window       {med_a:6.0f} ms   ({len(res.lines)} lines)")
print(f"  ocr      fast, full window           {med_f:6.0f} ms")

# 4. recognition on just the top strip - the speculative first block
strip_h = min(240, win.h)
strip = cap.capture_region(win.x, win.y, win.w, strip_h, stability_check=False)
ocr.recognize(strip.path)
med_s, _ = bench(lambda: ocr.recognize(strip.path), 5)
print(f"  ocr      accurate, top {strip_h}pt strip    {med_s:6.0f} ms"
      f"   ({len(ocr.recognize(strip.path).lines)} lines)")

# 5. layout + editor
med_l, _ = bench(lambda: layout.build_slice(res))
sl = layout.build_slice(res)
blocks = sl.readable()
med_e, _ = bench(lambda: [to_speech(b, verbosity=Verbosity.LOW) for b in blocks])
print(f"  layout   xy-cut + classify            {med_l:6.0f} ms   ({len(blocks)} blocks)")
print(f"  editor   normalize + ground           {med_e:6.0f} ms")

# 6. speech start
av = AVSpeechBackend()
def speak_start():
    u = av._av.AVSpeechUtterance.speechUtteranceWithString_("one")
    t = time.perf_counter(); av._synth.speakUtterance_(u)
    while not av._synth.isSpeaking() and time.perf_counter() - t < 1.0: time.sleep(0.001)
    av.stop()
med_sp, _ = bench(speak_start, 5)
print(f"  speech   to first audio               {med_sp:6.0f} ms")

print(f"\n  TOTAL to first word, as built:      {med + med_a + med_l + med_e + med_sp:6.0f} ms")
print(f"  TOTAL if we capture+ocr a strip only:{med_cg + med_s + med_l + med_e + med_sp:6.0f} ms")
for p in (c.path, strip.path):
    try: os.unlink(p)
    except OSError: pass
