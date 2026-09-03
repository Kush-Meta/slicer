# Slicer — Engineering Log

A record of how v0 was built and tested, written so that a decision can be
re-examined later without re-deriving it. It is chronological, and it includes
the things that went wrong, because those are the parts worth remembering.

Machine for all measurements: MacBook, Apple Silicon (arm64), macOS 15.3.1
(build 24D70), Swift 6.1.2 command line tools, Python 3.13.12.

---

## 1. Toolchain: two decisions forced by the environment

### 1.1 The Swift OCR bridge was abandoned

The first plan was a small compiled Swift binary wrapping Apple's Vision
framework: zero runtime dependencies, fast startup, one JSON blob on stdout.
It was written (~90 lines) and did not compile:

```
/Library/Developer/CommandLineTools/usr/include/swift/module.modulemap:13:8:
  error: redefinition of module 'SwiftBridging'
/Library/Developer/CommandLineTools/usr/include/swift/bridging.modulemap:13:8:
  note: previously defined here
```

Both modulemaps in the installed Command Line Tools declare the same module,
so every `import Foundation` fails. The known remedies are installing full
Xcode or renaming one of the system modulemaps with `sudo`. Neither is
appropriate: the first is a large unrelated install, the second edits system
files and needs a password.

**Decision:** reach Vision through PyObjC instead. Same frameworks, no compile
step, at the cost of a dependency and interpreter startup. The startup cost is
measured in §4 and turns out to be the strongest argument for the daemon design
rather than an argument against PyObjC.

### 1.2 Dependencies live in a project venv

`pyobjc-framework-Vision`, `pyobjc-framework-Quartz`, and later
`pyobjc-framework-WebKit` are installed into `.venv/` inside the project.
Nothing is installed into the system Python. `bin/slicer` execs the venv
interpreter, so there is no activation step and no global state to clean up.

---

## 2. Build order, and why

Modules were built in dependency order, each one validated before the next was
written, so that a failure could only be in the piece just added.

1. `blocks.py` — the data model, because the grounding invariant is a property
   of the model and everything else reads from it.
2. `ocr.py` — recognition, the riskiest dependency, proven before anything was
   built on top.
3. `capture.py` — screen capture with validity checks.
4. `layout.py` — reading order. The actual product.
5. `editor.py` — speakable text and `assert_grounded`.
6. `narrator.py` — speech, transport, epoch cancellation.
7. `conductor.py` — the state machine and degradation ladder.
8. `cli.py`, `doctor.py` — the surface.

The first smoke test captured a 900×500 region and recognized **zero lines**.
That was not a bug: the region was an empty area of the desktop. It was
diagnosed by rendering a known-good image and recognizing that instead, which
returned three lines at confidence 1.00 with correct boxes — including `$4,318`
and `12%` intact. That diagnostic became `tests/fixtures.py`.

---

## 3. The grounding invariant, and how it is enforced

**Rule: no model may author words that get spoken.** Every content word traces
to a token recognition found on screen.

Implemented as `editor.assert_grounded(spoken, block)`, which tokenizes the
text about to be spoken and decrements a multiset built from the block. Counts
are checked, not just membership, so normalization cannot silently duplicate
content either. It runs on **every block of every reading**, in the live path,
not only in tests.

`Utterance` has two fields:

| field | contents | grounded? |
|---|---|---|
| `text` | screen content | yes, checked |
| `prefix` | Slicer's own narration — counts, hedges, announcements | no, by design |

The split exists because the check kept correctly refusing legitimate
narration (§5.5). Keeping them separate means a listener can tell the tool's
voice from the screen's, and the check stays strict rather than being widened
until it stops catching anything.

It also bounds prompt injection: screen text is attacker-controlled on any web
page, and because planning may only emit block references, a malicious block
can at worst hide itself — which the skip log makes visible.

---

## 4. Measurements

All figures median of repeated runs on the machine above.

| Stage | Cold | Warm |
|---|---|---|
| `screencapture` 400×300 | 96 ms | 96 ms |
| Vision recognition | 484 ms | **33 ms** |
| `say` startup | — | *not reliably measurable — see §11.2* |
| `say` per word | — | ~362 ms |

**Recognition is ~15× faster warm.** Nearly all the cold cost is loading the
Vision framework, paid once per process. This is the decisive argument for a
resident daemon: a long-running Slicer pays it at launch and the 900 ms
first-word budget becomes comfortable rather than tight.

**The `say` startup figure needed care, and later turned out to be wrong.** A
single short utterance measures ~1030 ms, most of which is the duration of the
word. Fitting a line through four utterance lengths gave an intercept of
~145 ms, which was reported as the real cost of beginning to speak. It was not
reliable — see §11.2, where the same method produced 580 ms from the same
machine with different words.

**Permission model.** Screen Recording is held by the *terminal* running
Slicer, not by Slicer. A CLI therefore works today. macOS 15 refuses screen
capture to ad-hoc-signed binaries, so a standalone `.app` will need a Developer
ID certificate — but that purchase blocks packaging, not development.

---

## 5. Bugs found, in the order they were found

Each entry: symptom, root cause, fix, and the test that now prevents it.

### 5.1 Both columns of a two-column page classified as navigation

*Symptom.* The first fixture test produced an empty reading; both columns were
marked as chrome and skipped.

*Root cause.* The chrome heuristic was absolute — narrow column, short lines —
with no reference to context. A page made entirely of narrow columns satisfied
it everywhere.

*Fix.* Made the judgement relative: a column is navigation only if a
substantially wider sibling exists for it to be navigation *for*. Added a guard
that never lets chrome detection silence the entire slice.

*Test.* `test_equal_columns_are_not_mistaken_for_navigation`.

### 5.2 Dehyphenation produced a word that was not on screen

*Symptom.* `UngroundedSpeech: the word 'international' is not on the screen`,
raised on `["This word is inter-", "national in scope."]`.

*Root cause.* Joining `inter-` and `national` is correct — the word genuinely
was on screen, split by a line break — but the token multiset was built from
the raw lines, which contain `inter` and `national` and never `international`.

*Fix.* Moved line-break healing into `Block.healed_text` and built the multiset
from it. Healing is a declared, deterministic transformation with no judgement
in it, so its output is grounded by definition. The invariant was right; the
token model was incomplete.

*Test.* `test_line_break_healing_is_grounded`, plus
`test_capitalised_join_is_not_healed` for the `well-` + `Known` case that must
*not* be joined.

### 5.3 The duplication check was silently disabled

*Symptom.* Immediately after fixing 5.2, `"The quick quick brown fox."` was
accepted against a source containing one `quick`.

*Root cause.* The multiset added counts from both the healed text and the raw
lines, **summing** them — doubling every token's allowance.

*Fix.* Take the element-wise maximum, not the sum. A token's allowance is the
most times it legitimately appears under either reading.

*Test.* `test_duplicated_word_is_rejected`.

*Note.* This is the failure mode to watch for in this design: a fix that widens
the grounding model can disable the check without any test failing, because the
check's job is to refuse things. Both directions need a test.

### 5.4 A code block was read as prose

*Symptom.* `def rotate(cert): if cert.expires_in() < 30: renew(cert)` was
classified `body` and would have been read as punctuation.

*Root cause.* Detection relied on a single signal, the ratio of non-alphanumeric
characters, at a threshold of 0.28. The line scored 0.20.

*Fix.* Score five weak signals — symbol ratio (threshold lowered to 0.18), code
keywords, call syntax, statement terminators, staggered left edges from
indentation — and declare code only when three agree. Any single signal
misfires on prose: "use the function() to return a value" trips two.

*Tests.* `test_real_code_is_detected`, `test_prose_is_not_mistaken_for_code`.

### 5.5 A generated line count was mixed into grounded content

*Symptom.* `UngroundedSpeech: the word '3' is not on the screen`, from the code
summary "code block, 3 lines, beginning...".

*Root cause.* The count is generated by Slicer, not read from screen, and it
was being concatenated into the content string.

*Fix.* Split `Utterance` into `prefix` (narration) and `text` (content), with
the check applying only to `text`. The tempting alternative — adding digits to
the scaffolding allowlist — would have punched a hole exactly where numeric
hallucination is most dangerous.

*Tests.* `test_code_is_summarised_not_read_character_by_character`,
`test_narration_is_separate_from_content`.

### 5.6 Unicode normalization silently dropped whole blocks

*Symptom.* Text containing the `fi` ligature, fullwidth characters, roman
numerals, or superscripts raised `UngroundedSpeech`, and the Conductor's
handler dropped those blocks. The user would simply never hear them.

*Root cause.* `_normalize` applies NFKC before speaking, which rewrites
characters — `ﬁ` becomes two letters, `²` becomes `2`. The multiset was built
from un-normalized text, so those tokens did not exist in it. Ligatures are
common in PDFs, so this was not an edge case.

*Fix.* Same principle as 5.2: NFKC is a declared deterministic transformation,
so the multiset includes NFKC-normalized tokens.

*Tests.* `test_unicode_normalisation_stays_grounded`,
`test_fullwidth_text_stays_grounded`.

### 5.7 The third column of a three-column page was deleted

*Symptom.* A three-column page read only columns one and two. Column three was
classified as navigation and skipped. The same bug removed the label column of
every table — `Region / North / South / West` simply vanished.

*Root cause.* The XY-cut produces a **binary** tree, so three columns come out
as `left(left(A, B), C)`. The chrome check compared a column against its widest
sibling, and C's only sibling was the subtree containing both A and B. C was
therefore measured against roughly twice a column's width and looked like a
sidebar.

This is the most serious class of bug in the system: content on screen, never
spoken, never reported.

*Fix.* Three changes, all necessary:
1. **Flatten** chains of same-axis splits into one n-ary group, so all columns
   are siblings and "is this one unusually narrow?" has a meaningful answer.
2. Compare against the **median** sibling width, not the maximum.
3. Only the **first or last** column may be navigation — a sidebar sits at a
   margin, not between content columns.
Plus a content signal: navigation labels do not end in sentence punctuation.

*Tests.* `test_three_columns_are_all_read`,
`test_no_content_is_ever_silently_lost`.

### 5.8 Tables were read down each column

*Symptom.* `Region North South West, Revenue 4,318 2,901 5,144, Growth 12% 8%
21%` — every word present, all meaning gone.

*Root cause.* Not a bug so much as an unimplemented case: XY-cut sees a grid as
columns, which is geometrically true and useless to a listener.

*Fix.* Detect grids on a flattened vertical group and emit one block per row,
cells left to right, typed `TABLE_ROW`.

*Test.* `test_table_is_read_row_wise`, and the `table` golden page.

### 5.9 Table detection was too brittle for real HTML

*Symptom.* The first implementation of 5.8 worked on synthetic fixtures and
failed on the first real browser-rendered table.

*Root cause.* It required every column to contain the same number of leaves.
In the real table the recursion split on the widest gap first, and within one
column the remaining gap fell just under threshold, leaving `Region` and
`North` merged in a single leaf — 3 leaves where the other columns had 4.

*Fix.* Cluster rows directly from line y-centres, independent of how the
recursion happened to split. Structure should be read from the pixels, not
inferred from the shape of our own recursion.

*Test.* The `table` golden page, which is rendered by WebKit.

### 5.10 Table detection then swallowed a sidebar

*Symptom.* After 5.9, the sidebar layout produced rows pairing navigation with
body text: `"Home The body text begins here and continues."`

*Root cause.* The discriminator against prose was *median* words per line. A
column of one-word links beside a paragraph has a median of one, so it passed.
A 70 % fill threshold then tolerated the rows where only the nav column had
content.

*Fix.* Two tighter conditions: the **longest** line in the group must be short
(≤ 4 words), and every row must be complete except possibly one.

*Test.* `test_a_sidebar_beside_a_paragraph_is_not_a_table`.

### 5.11 The over-skip guard counted the wrong thing

*Symptom.* The guard from 5.1 began firing on the legitimate sidebar case,
un-skipping navigation.

*Root cause.* It measured skipped **blocks** as a fraction of all blocks. Five
one-word links beside one paragraph block is 5/6 — 83 % — when it is barely a
quarter of the words.

*Fix.* Measure words. Content volume is what is worth protecting.

*Test.* `test_over_skip_guard_counts_words_not_blocks`.

### 5.12 A zero-width space became a spoken block

*Symptom.* A block containing only `U+200B` produced an utterance.

*Root cause.* Zero-width characters are not whitespace to `str.strip`.

*Fix.* Drop Unicode category `Cf` (format) characters during normalization.

*Test.* `test_degenerate_blocks_do_not_crash`.

### 5.13 Three narrator tests encoded the wrong contract

Not a code bug, recorded because the investigation is the useful part. Three
tests failed; tracing the actual behaviour showed the narrator was correct:

- `read()` yields progress **before** speaking, so a `stop()` between the two
  announces a block that is then correctly never spoken.
- `skip(+1)` after a block finishes normally is indistinguishable from no skip.
  Its real contract is that an interruption which would otherwise **end** the
  reading instead **advances** it.

The tests were rewritten to assert the real contract. The temptation here is to
"fix" working code until the test passes; verifying behaviour first is what
avoided it.

### 5.14 A patch script corrupted `layout.py`

While rewriting table detection, a scripted string replacement inserted the new
function at the wrong offset and left a stray quote, producing a syntax error.
The file was rewritten in full rather than repaired. Recorded as a process
note: for changes spanning multiple functions, rewrite the file.

---

## 6. Testing method

### 6.1 Two corpora, deliberately

**Synthetic fixtures** (`tests/fixtures.py`) draw text at coordinates chosen by
the test, using AppKit. The generator knows the correct reading order because
it placed the words, so ground truth is exact. Fast, deterministic, no browser.

Their weakness is that they test the algorithm against *my assumptions* about
layout. Bugs 5.9 and 5.10 were invisible to them.

**Golden pages** (`tests/golden.py`) are HTML rendered by WKWebView through
PyObjC — real CSS columns, flexbox, font metrics and antialiasing, laid out by
the same engine a browser uses. Quick Look no longer renders HTML and
`cupsfilter` does not accept it, so WebKit snapshotting was the route that
worked. The renderer runs in a subprocess (`tests/_render_html.py`) because
`WKWebView` needs its own run loop.

Golden assertions check the **order of key phrases**, not exact strings.
Recognition on real rendered text is imperfect in ways that are not the
ordering algorithm's fault — the sidebar page yields `Serchive` for `Settings
Archive`. Reading the right column before the left is a Slicer bug; a merged
OCR line is not, and only the first should fail a test.

### 6.2 Making the narrator testable

Epoch cancellation was the one untested area, because exercising it for real
means making sound and waiting on it. The seam is `Narrator._say`. A
`FakeNarrator` subclass records what would have been spoken, returns
immediately, and can be told to cut a given utterance short — enough to drive
supersession, stop, skip and stale-epoch discard deterministically.

### 6.3 What is covered, and what is not

Covered: reading order across seven real layouts; the grounding invariant in
both directions; table and code classification and their false positives;
navigation detection and its guards; unicode normalization; capture validity;
epoch cancellation and transport; pipeline error paths and their remedies.

**Not covered:**
- Real audio timing. The narrator is tested through a fake; a race that only
  appears with live `say` processes would not be caught.
- Continuity past the fold, which is not implemented.
- Multi-display and mixed-DPI capture, which cannot be tested on one screen.
- Permission revocation. The uniform-frame detector is tested against a
  synthetic blank image, not an actual revoked grant.
- Any non-Latin script. Vision supports many; none are in the corpus.

---

## 7. Current state

```
tests/test_slicer.py     28   model, layout, editor, capture
tests/test_narrator.py    8   epochs, transport, cancellation
tests/test_conductor.py   4   pipeline error paths
tests/test_picker.py      8   coordinate conversion, selection
tests/test_fingerprint.py 12  OCR-tolerant matching
tests/test_continuity.py  6   reading past the fold
tests/test_overlay.py     4   highlight placement
tests/test_daemon.py      7   IPC protocol, real daemon spawn
tests/test_golden.py      7   WebKit-rendered pages
                         --
                         84   all passing
```

Run everything with `./.venv/bin/python tests/run_all.py`.

Five of the eleven code bugs above caused **silent data loss or silent
substitution** — the categories the pre-mortem ranks as trust-destroying. Three
were found only because the invariant refuses rather than tolerates, and two
only because the corpus included pages laid out by a real browser.

---

## 8. Phase two: picker, continuity, and content protection

### 8.1 macOS 15 does honour window content protection

The question deferred three times is answered. `scripts/verify_content_protection.py`
shows an ordinary window in a capture (the control), then shows a window with
`sharingType = NSWindowSharingNone` and captures again. On macOS 15.3.1 the
protected window is **excluded** - the capture contains what was behind it.

*Consequence.* The OS flag is real on this version, so Slicer's own application
denylist is defence in depth rather than the only line. The script exists so
this is re-checked on every major release instead of assumed.

### 8.2 Slicer owns the region picker

`screencapture -i` gives a good selection experience and then refuses to say
which rectangle was chosen. That one missing fact blocks re-capture after a
scroll, on-screen highlighting, and saved regions - so the picker is now ours:
a dimmed overlay per display, drag to select, Escape to cancel, and the
overlay sets `sharingType` none so it never appears in our own captures.

The difficulty is coordinates. AppKit places windows in a global space whose
origin is the bottom-left of the main display with y increasing upward;
`screencapture -R` wants top-left with y increasing downward. The conversion
lives in `picker.to_capture_space` so no caller has to know, and it is tested
at both extremes.

Dragging is driven directly in tests rather than through synthesized events, so
the suite needs no Accessibility permission. What that cannot cover is whether
the overlay *looks* right, which needs eyes.

### 8.3 Word trigrams could not survive OCR errors

*Symptom.* A paragraph with two recognition errors failed to match its clean
self, so a scroll would have re-read it.

*Root cause.* Fingerprints were word trigrams. One misread word destroys three
of them, and recognition errors are guaranteed in real use.

*Fix.* Character 5-grams over normalized tokens. Measured on the corpus:

| comparison | word-3 | char-5 |
|---|---|---|
| same text, 2 OCR errors | 0.50 | 0.89 |
| same text, 3 OCR errors | 0.50 | 0.84 |
| clipped half-paragraph | 1.00 | 1.00 |
| different paragraph | 0.00 | 0.04 |
| sticky header vs body | 0.00 | 0.13 |

True matches land at 0.84 and above, false ones at 0.13 and below. The 0.55
threshold sits in a very wide gap, which is what makes it safe to leave alone.

Similarity is Jaccard **or** containment, whichever is higher: a paragraph
clipped by the top of the viewport shares all its grams with the full version
but only half the union, and plain Jaccard would call it a different block.

### 8.4 Block-level fingerprints broke on regrouping

*Symptom.* Reading a tall page stopped three paragraphs early, reporting
"the content changed on screen" - a false positive from the watchdog.

*Root cause.* Fingerprints were per block, and **block boundaries are not
stable across captures**. Paragraph grouping depends on what else is visible,
so scrolling merges two blocks into one or splits one into two. The new blocks
then matched nothing, overlap fell to zero, and the watchdog concluded the
screen had changed.

*Fix.* Fingerprint **lines**, not blocks. A line is the same line regardless of
what surrounds it. A block counts as read once 60% of its lines have been.

*Note.* This is the second time a stable-looking intermediate representation
turned out to depend on viewport state. Anything derived from grouping is
suspect across captures; only line-level facts survive.

### 8.5 A unit test depended on what was on screen

`test_a_capture_remembers_where_it_came_from` captured the top-left of the live
screen. It passed alone and failed intermittently under the runner: if that
area happens to be uniform, the capture validity check correctly rejects it.
Rewritten to construct the object directly. The live capture path belongs in
`doctor`, where nondeterminism is acceptable, not in a suite that must be
trusted.

### 8.6 Continuity, and how it is tested

`read_continuous` reads a screenful, scrolls, re-captures, and resumes after the
last familiar block - last rather than first, so a sticky header repeated at
the top of every screen does not rewind the reading. It stops, always with a
stated reason, when the content stops advancing, when the watchdog fires, or
when the screen budget runs out.

Scrolling posts scroll wheel events, which macOS gates behind Accessibility
permission. Without it Slicer does not fail: it says so and follows the reader's
own scrolling instead. That is also the better interaction in one respect - the
pre-mortem lists "user scrolls while it reads" as a common failure, and a reader
that follows the viewport cannot hit it.

Tests simulate scrolling by cropping successive overlapping windows out of one
tall rendered page, which is exactly what a scroll looks like to the pipeline.
That makes the two failures that matter deterministic: a paragraph read twice,
and a paragraph never read at all.

---

## 9. Phase three: the daemon, the hotkey, and the highlight

### 9.1 The daemon exists for one number and is shaped by another

*The number it exists for.* Recognition costs 440 ms in a cold process and
116 ms in a warm one on a full page. End to end, a reading plan goes from
532 ms to 161 ms - 3.3x. Almost all of the difference is loading the Vision
framework, which is paid once per process.

A note on honesty in measurement: an earlier figure of "33 ms warm" was real
but came from a single-line image. On a realistic page the warm cost is 116 ms.
Both are true; only the second is representative, and quoting the first would
have flattered the design.

*The number it is shaped by.* **AppKit is main-thread only.** The region picker
and the highlight overlay both need the main run loop, so the main thread
cannot be the one blocking on a socket. The architecture follows directly:

    main thread    - AppKit run loop, all UI, drains a work queue every 20 ms
    socket threads - accept, parse, and run recognition / layout / speech

Work that touches a window is put on the queue and waited on. Everything else
runs wherever it lands. This is why `test_daemon.py` spawns a real process
rather than using a fake: the thing worth verifying is precisely the part a
fake would replace.

### 9.2 Transport: newline JSON over a Unix domain socket

Chosen over a localhost TCP port and over a JSON-RPC library, for reasons in
that order of importance:

* A Unix socket **cannot be reached from the network at all**. A localhost port
  can be, by anything else on the machine. This process moves screen contents
  around; the transport should not be addressable.
* Filesystem permissions are the access control - a `0700` directory and a
  `0600` socket. No tokens, no handshake, nothing to get wrong.
* Replies are **streamed**: a reading emits a line per block as it is spoken,
  and the client prints them live. Request-response framing would have to be
  worked around to do that.
* A socket file with nothing behind it is a crashed daemon, not a running one.
  `ipc.connect` detects and clears it, so a crash never blocks the next start.

This is the shape Core Lightning and signal-cli use for the same job, and it
needs nothing outside the standard library.

### 9.3 The hotkey is a privacy decision, not a technical one

Two APIs can register a system-wide hotkey on macOS.

`NSEvent.addGlobalMonitorForEventsMatchingMask` is the modern one. It requires
Accessibility permission and it delivers **every keystroke on the system** to
this process - the shape of a keylogger, whatever the intent.

Carbon's `RegisterEventHotKey` is formally deprecated and entirely stable. It
requires **no permission at all** and delivers exactly the one combination
registered. Electron, VS Code and Slack all still use it, because Apple never
shipped a replacement for this specific job.

Slicer already asks for Screen Recording. Asking for keystroke access as well,
when a narrower API does the job, would be the wrong trade. We use the Carbon
route through `quickmachotkey`, which wraps it properly. Verified in practice:
the daemon registers `cmd-ctrl-R` with no permission prompt.

The one cost: modifier-only shortcuts cannot be expressed this way. They need
an event tap, and we would rather have no such shortcut than that permission.

### 9.4 The highlight: three lines that are each easy to omit

The overlay is visually simple and has three properties that are not:

* `setIgnoresMouseEvents_(True)` - it sits above everything, so without this it
  swallows every click on the screen.
* `setSharingType_(NSWindowSharingNone)` - otherwise the highlight appears in
  Slicer's **own** next capture and is recognized as content during a following
  read. This is correctness, not cosmetics.
* `canJoinAllSpaces` - a reading should survive the user switching desktops.

A test asserts the first two are present in the source, because their absence
produces symptoms nowhere near their cause.

Placement crosses two conversions - Retina pixels to points, then
screencapture's top-left origin to AppKit's bottom-left one - and both are
tested, including that a 2x capture resolves to the same rectangle as a 1x one.
A capture whose origin is unknown returns `None`: better to draw nothing than
to draw a box in the wrong place.

### 9.5 Prior art consulted before building

Per instruction, each of these was checked before writing code:

| Source | What it decided |
|---|---|
| Carbon vs NSEvent discussions in electrobun and KeePassXC | Use Carbon; avoid the Accessibility requirement entirely |
| `quickmachotkey` | Wraps `RegisterEventHotKey` correctly; no hand-rolled bindings |
| Core Lightning, signal-cli | Newline-JSON over a Unix socket is the established local-IPC shape |
| `NSWindowStyles`, Electron click-through workarounds | `setIgnoresMouseEvents_` is the mechanism for a click-through overlay |
| `rumps` | The intended menu bar shell when Slicer needs visible idle state; not yet used |

### 9.6 One bug, minor

Stage timings never printed on the daemon path. The client's reply handler
tested `"blocks" in message` before `message.get("ok")`, so a plan response -
which carries both - took the earlier branch and returned before reaching the
timings line. Merged into a single branch.

---

## 10. Phase four: speech moves in-process

### 10.1 Why

`say` was chosen because it needed no setup. Two costs made that the wrong
trade for a screen reader:

- A subprocess per utterance is spawn cost paid on **every keypress** while
  skimming, which is the interaction navigation is built around.
- A killed process cannot be resumed. Pausing restarted the block from the
  beginning, which for a listener is not a pause at all.

`AVSpeechSynthesizer` runs in this process. Measured directly: **35–37 ms to
first audio**, pause and resume mid-sentence, instant stop. It also works with
no AppKit run loop being pumped, which was the open risk — `slicer navigate`
blocks its main thread reading keys and has no run loop to spare. Verified
before building anything on it.

Speech is now behind a `SpeechBackend` protocol with two implementations, so
`say` remains as a fallback for a machine where the AVFoundation bindings are
missing, and `--speech say` forces it for comparison.

### 10.2 A measurement that was wrong, and how it was caught

The doctor originally reported `say` startup as a fitted intercept over four
utterance lengths. Building the new backend produced a contradiction: the same
method now fitted **580 ms** where it had previously fitted **145 ms**.

The first hypothesis was contention — the in-process synthesizer holding the
audio device and making `say` wait. That was testable, and it was wrong:
measuring `say` before the synthesizer existed gave 580 ms too, a 3 ms
difference.

The actual cause is that a four-point fit with correlated errors returns
whatever the phrasing implies. The earlier run used distinct words ("one two
three four five"), the later one repeated the same word; slope came out 362 vs
240 ms/word, and the intercept moved with it. Neither number was trustworthy.

**Resolution: stop reporting inferred components.** The in-process backend
exposes `isSpeaking`, so the moment audio starts is directly observable and is
reported as measured. `say` offers no such signal, so it is reported as what
can actually be measured about it — the whole round trip for one short word,
startup and pronunciation together, labelled as not separable.

The lesson is narrower than "measure twice". A derived number that cannot be
observed directly should be labelled as derived, and checked against a second
method before it is repeated in three documents — which this one was.

### 10.3 Rate and voices

`--rate` is words per minute for `say` and an opaque 0-to-1 scale for
AVSpeechUtterance. The two are anchored so the flag means the same thing
either way: `say`'s default of ~175 wpm maps to AV's default of 0.5, linearly
and clamped. The scale is not documented as linear, so this is approximate by
construction and says so.

A voice that was asked for and silently substituted is worse than no voice
setting at all, particularly for someone who cannot see which voice is in use.
Unknown voice names are now reported, and `slicer voices` lists the 190
available on this machine.

### 10.4 A patch that broke initialisation invisibly

A scripted edit inserted a new property in the middle of `__init__`, orphaning
the two lines that follow it. The class still imported, and twenty tests failed
with `'FakeNarrator' object has no attribute '_pause'`. Recorded because the
symptom pointed at the test doubles rather than at the constructor, and because
it is the second time a scripted multi-function edit has damaged a file. The
standing rule stands: for changes spanning more than one function, rewrite the
file.

---

## 11. Open items

Ordered by what blocks what.

1. **Resident daemon.** Recognition is 484 ms cold and 33 ms warm; a
   long-running process pays that once. The single largest perceived-speed win.
2. **On-screen highlight** following the reading, now that the picker returns
   real coordinates. The most convincing thing to show someone.
3. **Word-timing callbacks.** The in-process backend can report which word is
   being spoken, but the delegate needs a run loop, so it is available under
   the menu bar app and not inside `navigate`. Needed for word-level cursor
   tracking.
4. **Verify continuity against a real scrolling application**, not only the
   cropped-page simulation. Momentum scrolling may overshoot.
5. **Accessibility tree ahead of the deep lane.** Cheapest accuracy win
   available; the one system doing this at scale reaches for it first.
6. **Non-Latin scripts** in the golden set before claiming multilingual support.
