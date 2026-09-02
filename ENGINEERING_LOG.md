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
| `say` fixed startup | — | ~145 ms |
| `say` per word | — | ~362 ms |

**Recognition is ~15× faster warm.** Nearly all the cold cost is loading the
Vision framework, paid once per process. This is the decisive argument for a
resident daemon: a long-running Slicer pays it at launch and the 900 ms
first-word budget becomes comfortable rather than tight.

**The `say` startup figure needed care.** A single short utterance measured
~1030 ms, which appeared to blow the budget on its own. It does not: most of
that is the duration of the word itself. Timing four utterance lengths (1, 5,
10, 20 words) and fitting a line gives a slope of ~362 ms per word and an
intercept of ~145 ms, which is the real cost of beginning to speak. `doctor`
now uses the same four-point fit rather than a single sample, because a
headline number that swings 3× between runs is not usable.

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
tests/test_golden.py      7   WebKit-rendered pages
                         --
                         47   all passing
```

Run everything with `./.venv/bin/python tests/run_all.py`.

Five of the eleven code bugs above caused **silent data loss or silent
substitution** — the categories the pre-mortem ranks as trust-destroying. Three
were found only because the invariant refuses rather than tolerates, and two
only because the corpus included pages laid out by a real browser.

---

## 8. Open items

Ordered by what blocks what.

1. **Verify window content protection on macOS 15.** Mark a window protected,
   capture the screen, inspect the result. Fifteen minutes, and it decides
   whether OS-level exclusion is a defence or a decoration.
2. **Continuity.** One capture only. Reading past the fold needs scroll,
   re-capture, and shingle-fingerprint alignment. The largest missing piece.
3. **Accessibility tree ahead of the deep lane.** Cheapest accuracy win
   available; the one system doing this at scale reaches for it first.
4. **Move speech in-process** to `AVSpeechSynthesizer`, removing the ~145 ms
   spawn cost and enabling mid-utterance resume and word-timing callbacks.
5. **Interactive capture coordinates.** `screencapture -i` does not report the
   chosen rectangle, so blocks cannot be mapped back to the display.
6. **Non-Latin scripts** in the golden set before claiming multilingual support.
