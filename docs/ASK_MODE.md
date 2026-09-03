# Ask Mode — product direction

Branch: `ask-mode`. Nothing here is built yet. This records the landscape scan
and the design decisions that came out of it, so they can be argued with later.

Full readable version, with the positioning map and the citation diagram:
https://claude.ai/code/artifact/2849a2a2-2b1b-4ccb-bc2f-4cd078324382

---

## 1. The idea, and what the scan found

**Idea:** ask questions about what is on screen, or inside a selection, answered
by a local model, with nothing leaving the machine.

**Finding:** as stated, that is not an opening. It is a funded, contested
category, it has a free open-source clone, and Apple ships a version of it in
the macOS context menu.

| Who | What | Position |
|---|---|---|
| Highlight AI | Desktop assistant across every app; attach screen/voice/doc and ask | $40M Series A (Khosla, Mar 2026), $73M+ total, 500k+ signups |
| Clicky | Viral screen-assistant demo, now a company | YC, ~$10M |
| Cluely | Screen-context assistant | Funded |
| Jarvis | Cmd-/ bar, asks about the screen, 35 app connectors | Commercial |
| Screenpipe | Continuous local capture + local search, works with Ollama | YC S26, open source |
| `Bitshank-2338/clicky-windows` | "Reads your screen, hears your question, speaks the answer. No API key, runs locally with Ollama." | Open source, free |
| Apple Siri | Onscreen awareness in the macOS context menu, on-device | Free, in the OS |

## 2. Why it is not dead: what Apple's version cannot see

Apple's onscreen awareness is a **cooperation API**, not pixel understanding.
Developers attach `NSUserActivity` to the view holding their primary content and
annotate items with View Annotations so Siri can resolve "this" and "that".
Apple's documentation is explicit that Personal Intelligence features require
the app to expose its content through App Intents.

So it works for apps whose developers did the work, and is blind to everything
else: PDFs in third-party viewers, remote desktop, canvas and Electron apps,
video frames, screen-shared windows, scanned documents, legacy internal tools.

**This is Slicer's founding argument restated.** Screen readers walk the
accessibility tree and fail where the tree is absent or lying; Slicer reads
pixels and works everywhere. Apple's onscreen awareness is the same cooperation
model in newer clothes and fails in the same places.

Ask Mode is therefore not a pivot. It is the original thesis applied to a
second verb.

## 3. The position that is actually open

Every tool above hands a screenshot or an OCR dump to a model and gets prose
back. Spatial structure is discarded at the first step, so the answer is a
floating claim you either trust or verify by hand.

Slicer does not discard it. Blocks already carry ids, bounding boxes,
confidence and reading order, and the highlight overlay can already draw on any
screen region. That makes a different product possible:

> Ask "what's the total?". It answers "$4,318" — and the box lights up around
> that exact cell on your screen.

The pattern is proven in document AI: V7 Go records a bounding box for every
extracted value and links back to a highlight on the source page; LandingAI
returns a bounding-box citation with every field. The research calls it visual
grounding and finds people can verify an answer at a glance.

Nobody has applied it to a **live screen** rather than an uploaded file.

|  | Any pixels | On device | Answer shows its source |
|---|---|---|---|
| Apple Siri | no | yes | no |
| Highlight / Clicky / Cluely | yes | no | no |
| Screenpipe | yes | yes | no |
| V7 Go / LandingAI | files only | no | yes |
| **Slicer Ask Mode** | **yes** | **yes** | **yes** |

## 4. Design: two modes, one wall between them

Slicer's founding rule is that **no model may author words that get spoken**.
Ask Mode is definitionally a model authoring words. That is faced directly, not
eroded quietly, because the rule is why the reader can be trusted.

|  | Read mode | Ask mode |
|---|---|---|
| Whose words | The screen's, verbatim | The model's |
| Invariant | Every content word traces to a recognized token | Every claim carries the block id it came from |
| Unciteable output | Impossible by construction | Flagged aloud, never highlighted |
| Voice | The reading voice | A different voice, so the ear can tell |
| Highlight | Follows the block being read | Jumps to the cited blocks |

The seam already exists: `Utterance` has separated `prefix` (Slicer's own
narration) from `text` (grounded screen content) since day two, and only `text`
is checked. An answer is narration with citations attached.

**The citation contract.** The model never returns prose alone. It returns
claims paired with block ids; a verifier checks every id exists in the slice and
drops claims that cite nothing. That bounds hallucination and produces the
highlight for free — the citation and the visual proof are the same object.

## 5. Feasibility

| Piece | Choice | Cost |
|---|---|---|
| Screen → blocks | Already built | done |
| Speech in | Apple `SpeechAnalyzer`, on-device, push-to-talk on the existing hotkey (no always-on mic) | ~1 day |
| The model | Ollama 0.19+ runs MLX on Apple Silicon; a 9B does 25–35 tok/s, prefill >1,100 tok/s | ~1 day |
| Citations | Structured output, verified against the slice | ~1 day |
| Speech out + highlight | Already built | done |

**No local vision model needed** — that is where local tools normally lose to
cloud. Clean structured text into a competent local text model beats a
screenshot into a weak local VLM. The reading work already paid that cost.

## 6. Demo recommendation

Reader is the hero. Ask Mode as a 30-second coda: select a table, **type** (do
not speak) "which region grew fastest?", answer plays while the box lights up
around that row. Typed input keeps speech recognition off the critical path,
which is the part most likely to fail on someone else's machine. The citation
and the highlight are the idea, and they are demonstrable in a day.

## 7. Risks

- **Verifiability may be a feature, not a product.** People say they want to
  check AI answers and often do not. Counter: the buyers where it is not
  optional are the same ones who cannot use cloud tools at all.
- **A local 9B loses head-to-head.** Against a frontier model on open-ended
  reasoning, visibly. Positioning must claim only what local wins: extraction,
  lookup, summarisation and comparison *within visible text*.
- **Apple could extend onscreen awareness to arbitrary pixels.** They have the
  OCR, the silicon and the models. Do not build anything whose only defence is
  that Apple has not got there yet.
- **Ask Mode could contaminate Read Mode.** The real internal risk. Separate
  code paths, and a test asserting the reader can never emit a model token.

## 8. Who buys it

1. **Regulated professionals** — HIPAA, 42 CFR Part 2, CMMC 2.0, GDPR/EU AI
   Act, ITAR. Cannot send PHI/PII/controlled data to an external API as a rule,
   not a preference. Audit obligations make citations a requirement.
2. **Blind and low-vision users** — people already use Be My AI to understand a
   screen when JAWS stops talking. That failure is exactly where Slicer works,
   and the population already pays for assistive software. Here reading and
   asking are one product, not two features.
3. **Anyone in a non-cooperating app** — remote desktop, VMs, legacy internal
   tools, PDFs in the wrong viewer. Unglamorous, and a lot of professional
   software.

## 9. The experiment that decides it

One afternoon, before committing a week. Feed the existing golden-set blocks
(ids, text, boxes) to a local model via Ollama and ask ten real questions.

- Are answers good enough on extraction and lookup? Probably yes.
- Fast enough at 2–3s? Almost certainly, for asking as distinct from reading.
- **Will a 9B reliably cite real block ids?** This decides the product. Test it
  adversarially — ask questions the screen does not answer and see whether it
  invents an id. If a local model will not hold a citation contract, the
  differentiator evaporates and this is just another local assistant.
