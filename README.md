# Slicer

Point at a region of the screen and it reads aloud — in the order a person
would read it, not top to bottom.

This is v0: a working vertical slice with the architecture in place, not a
prototype to be thrown away. Capture, recognition, reading order, speech
normalization, and playback all run end to end on macOS with no cloud calls
and no models.

```bash
./bin/slicer doctor                    # check this machine, measure the budget
./bin/slicer plan --file page.png      # show what would be read, silently
./bin/slicer read                      # drag a region, then listen
./bin/slicer read --region 0,0,900,600 --timings
```

During a reading: `space` pause · `n` next block · `p` previous · `q` quit.

## What makes this different from the other read-aloud tools

Several open-source tools already do drag-a-region, OCR, and speak. All of them
sort recognized text top-to-bottom, which reads a two-column page straight
across. Slicer's ordering is a recursive XY-cut: find a vertical gutter no line
crosses and the region is columns, so read left fully, then right; otherwise
find a horizontal gap and read top then bottom.

On the demo page, naive ordering produces:

> A second column of body **Reading order is the hard** text that should be
> read **problem here, not the text**…

and Slicer produces the two columns in full, in order. That difference is the
product.

The method is deterministic and needs no model. That is a deliberate trade: a
reader that orders the same screen differently twice loses trust faster than
one that is consistently a little wrong.

## The invariant

**No model may author words that get spoken.** Every word of content traces
back to a token recognition actually found on screen, and `assert_grounded`
enforces it on every block of every reading — in the live path, not in tests.

Anything Slicer generates itself — a line count, a hedge like "unclear", an
announcement — lives in `Utterance.prefix`, structurally separate from
`Utterance.text`. The listener can tell narration from content, and the check
applies only to content.

This is load-bearing before any model exists, which is the point. It caught two
real bugs during this build: dehyphenation producing a word in no source token,
and a generated line count being concatenated into content. When a vision model
joins the parse stage, the guarantee is already in place rather than being
retrofitted after the first confidently-spoken hallucination.

It also bounds prompt injection. Screen text is attacker-controlled on any web
page; because planning can only emit block references, the worst a malicious
block achieves is hiding itself — which the skip log makes visible.

## Layout

```
bin/slicer            launcher (uses the project venv)
slicer/
  blocks.py           the grounded data model — TextLine, Block, Slice
  capture.py          screen capture + uniform-frame and stability checks
  ocr.py              Apple Vision recognition, boxes flipped to top-left
  layout.py           XY-cut ordering, paragraph grouping, classification
  editor.py           speakable text + assert_grounded
  narrator.py         speech, transport, epoch cancellation
  conductor.py        the state machine and degradation ladder
  telemetry.py        stage timings to ~/.slicer/telemetry.jsonl
  doctor.py           environment checks and the latency budget
tests/
  fixtures.py         renders pages whose correct reading order is known
  test_slicer.py      20 tests, no external test runner needed
```

## Measured on this machine (M-series, macOS 15.3.1)

| Stage | Cold | Warm |
|---|---|---|
| Capture (400×300) | 96 ms | 96 ms |
| Recognition | 484 ms | **33 ms** |
| Speech startup | — | ~145 ms |

Recognition is 15× faster warm — almost all of the cold cost is loading the
Vision framework. This is the strongest argument for the resident-daemon design:
a long-running Slicer pays that once, and the 900 ms first-word budget is then
comfortable rather than tight.

## Deliberately not built yet

Named honestly rather than left to be discovered:

- **Continuity.** One capture only. Reading past the fold needs scroll,
  re-capture, and shingle-fingerprint alignment to avoid duplicated or skipped
  paragraphs. This is the largest missing piece.
- **The deep lane.** No Docling, no vision model. XY-cut handles columns well
  and will struggle on dense application chrome.
- **Accessibility tree.** Should come before the deep lane — it is the cheapest
  accuracy win available, and screenpipe reaches for it first at scale.
- **Interactive-capture coordinates.** `screencapture -i` does not report the
  chosen rectangle, so blocks cannot yet be mapped back to the display for
  highlighting.
- **Saved slices**, the floating widget, and the metrics dashboard.

## Known limits

- Epoch cancellation is implemented and exercised by hand, but has no automated
  test — a race that only appears under real audio timing.
- Paused readings restart the current block rather than resuming mid-utterance,
  because `say` cannot be resumed. Moving to AVSpeechSynthesizer in-process
  fixes this and removes the ~145 ms spawn cost.
- The doctor's speech figure is a least-squares fit over four utterance lengths;
  expect a spread of tens of milliseconds between runs.
- Screen Recording permission belongs to the **terminal** running Slicer. A
  standalone `.app` will need a Developer ID certificate — macOS 15 refuses
  ad-hoc-signed binaries for screen capture.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install pyobjc-framework-Vision pyobjc-framework-Quartz
./.venv/bin/python tests/test_slicer.py
```

Grant Screen Recording to your terminal in System Settings → Privacy &
Security, then fully quit and reopen it. macOS does not apply the grant to a
running process.
