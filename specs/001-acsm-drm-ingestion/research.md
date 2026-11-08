# Phase 0 Research: ACSM / Adobe-DRM PDF ingestion

**Feature**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07

## Measured verdicts (A1–A5) — REQUIRED BEFORE READER-BEHAVIOUR CLAIMS

Per constitution §VI and FR-025, no reader-behaviour claim may enter code,
docs, or the runbook until its row below records a verdict. Measured on:—

| # | Assumption | Verdict | Evidence / notes |
|---|---|---|---|
| A1 | Reader renders correctly under a pinned Wine on a virtual display | **UNMEASURED** | Requires an activated prefix and a real loan |
| A2 | Synthesised keys turn pages (and which mechanism works) | **UNMEASURED** | Decides `driver.send_turn`'s implementation and whether a WM is required |
| A3 | Window-grab at the measured crop yields OCR-acceptable legibility | **UNMEASURED** | Record word error rate + chosen DPI/render settings |
| A4 | Fulfilled container carries usable rights metadata (the key) | **UNMEASURED** | Fallback is content hash and is already implemented |
| A5 | Activation survives a headless relaunch | **UNMEASURED** | Failure ⇒ automation loop is unusable as designed |

**Consequence for this feature's implementation**: the reader-facing constants
(display geometry, window title match, crop box, key mechanism) ship as
named, single-source constants in `capture.py`, and the module docstring states
which verdict pins each one. Everything else is implemented and tested without
them.

## Decisions

### D1 — Identity comes from embedded rights metadata, with a content-hash fallback

**Decision**: derive the document key from the container's rights metadata
(`META-INF/rights.xml`) when a stable identifier is present; otherwise use the
SHA-256 of the container bytes, and report `key_source`.

**Rationale**: the key is the primary key for the cache path and the idempotency
lookup (`ingestion.find_existing(source=...)`), so it must be stable across
process runs and readable without opening the encrypted payload. A content hash
is always available and always correct, but it changes when the operator
re-downloads the same loan; making the provenance explicit lets the check tool
and the operator see the difference. Rights metadata is a plaintext part of the
container, so reading it is not decryption (constitution §II).

**Rejected**: title/author-derived slugs (unstable, collide); key from the
`.acsm` (changes on every renewal, and the file is a token not a document).

### D2 — OCR client is vendored from the sibling pack, not extracted yet

**Decision**: copy `ocr_config.py`, `ocr_wire.py`, local and remote clients from
`marginalia-plugin-kindle` into `acsm/`, keep the upstream environment-variable
names and protocol version, and add a test that asserts the vendored protocol
version equals upstream's when upstream is importable.

**Rationale**: the sibling is the only proven consumer; extracting a shared
distribution before this pack works would couple a working plugin to a
hypothesis. The protocol has a version handshake, so a drifted copy fails loudly
rather than silently. The sync-guard test plus a `VENDORED_FROM` marker makes the
duplication safe to hold for one release (constitution §VI).

**Rejected**: a new shared distribution now (premature, touches a working pack);
putting the client in the SDK (pulls HTTP and imaging dependencies into every
plugin); a second protocol implementation (two wire formats for one service).

### D3 — Page-turn detection uses frame digests, and staleness is not emptiness

**Decision**: hash the cropped frame after every turn. An unchanged digest means
the turn did not register (retry, bounded). Empty OCR text is counted separately
and only ends the capture after its own, larger threshold.

**Rationale**: without a DOM there is no direct signal that a page changed. The
two failure modes are genuinely different: a lost keypress repeats a page (text
would be duplicated), while a photo insert or blank verson legitimately produces
no text. Conflating them would end captures early on image-heavy books or loop
forever on a stuck page.

**Rejected**: page-count cross-check as the primary signal (the reader's counter
is not reliably machine-readable); fixed page counts (unknown before capture).

### D4 — Capture is one process-local flow: one display, one reader, one document

**Decision**: `capture.py` starts and tears down a private display per capture;
`acquire.py` opens the document, drives it, OCRs each page as it is captured,
and writes the cache.

**Rationale**: one display per capture removes cross-document interference (two
readers fighting for focus on one display is exactly the race that loses
keypresses) and keeps the failure domain small. Startup cost is a few seconds
against a capture that takes minutes.

**Rejected**: a long-lived display shared across documents (focus races);
driving the operator's desktop (constitution §III, and it would seize their
input).

### D5 — Cache writes are atomic; empty captures write nothing

**Decision**: write text and sidecar to temporary files and `os.replace` them
into place; write neither when no text was captured.

**Rationale**: a partially written cache file would be indistinguishable from a
complete one to `find_existing` and would be ingested as canonical text,
violating constitution §I. The sibling writes directly because its cache is
re-scrapable; here ingestion may happen much later, and a truncated file is a
silent corpus defect.

**Rejected**: write-then-ingest in one transaction (ingestion is a separate,
possibly-forced-later step); a marker file for completeness (two files to keep
consistent instead of one atomic rename).

### D6 — The sidecar records provenance, not just text

**Decision**: `extracted/{key}.json` holds key provenance, title, container
path, page count, `ended_by`, restarts, OCR identity, quality summary, the
suspect-page list, review state, timestamps, and reader/tool versions.

**Rationale**: the operator must be able to answer "is this capture complete and
trustworthy?" without re-running it — the pack's only defence against silently
contributing a partial book (constitution §I, §VI). Recorded reader versions
also make a future behaviour change attributable.

**Rejected**: stuffing everything into document metadata (core's metadata is for
the corpus, and the pre-ingest state has no document); logging only (not
durable, not queryable by the check tool).

### D7 — Verification splits into three tiers, and CI owns only two

**Decision**:

1. **Unit** (CI, no external tooling): loop transitions, crop/blank/digest
   behaviour against fixture images, key derivation against fixture metadata,
   ingest idempotency against a fake client, vendored protocol guard, setup
   probing with every dependency absent.
2. **Integration** (CI where a display is available, else skipped with a reason):
   X11 plumbing end-to-end against a slideshow window that is *not* the reader.
3. **Operator smoke** (never CI): a real `.acsm` through a real reader on a real
   prefix.

**Rationale**: the reader cannot run in CI, and pretending otherwise produces
green builds that prove nothing (constitution §VI). Tier 2 is the highest-value
automation available: it separates "our X11 orchestration is wrong" from "the
reader is misbehaving", which are otherwise indistinguishable.

**Rejected**: mocking the X layer entirely (then nothing tests the real
automation); a mandatory real-reader CI job (impossible).

### D8 — Subprocess execution is fixed-argv and environment-scoped

**Decision**: every external call (`wine`, `Xvfb`, the window manager,
`xdotool`, `import`/`convert`) is invoked with a fixed argument list, no shell,
and an explicit environment carrying only what that process needs (display,
prefix, temporary home where applicable).

**Rationale**: the pack spawns unsandboxed Windows software; shell
interpolation of paths from operator input is the obvious injection surface, and
`$DISPLAY`/`$WINEPREFIX` leakage across captures is the obvious correctness
surface. A fixed `env` also makes a capture reproducible.

**Rejected**: `shell=True` one-liners (injection); inheriting the ambient
environment (non-reproducible, and the operator's `DISPLAY` would be used if the
private one failed to start — which would then seize their screen).

### D9 — The pack declares egress and subprocess permissions, and says why

**Decision**: `network: egress` with an empty allowlist (the OCR endpoint is
operator-configured), `subprocess: true`, `filesystem: read_write_plugin_data`,
`ingest: true`; comments in the manifest state that subprocesses run Windows
software with the operator's privileges.

**Rationale**: the operator approves the manifest, so the manifest is the place
to disclose the riskiest grant; an empty allowlist matches the sibling pack's
posture (no fixed host baked into a distributed artefact).

**Rejected**: `network: none` (breaks the remote OCR path, the only fast one);
a hard-coded endpoint in the manifest (leaks one operator's infrastructure into
every install).

### D10 — OCR is requested one page at a time, not in transport batches

**Decision**: `acquire.py` calls the vendored client once per captured page and
never accumulates a multi-page batch.

**Rationale**: the loop's empty-page counter must be measured on the text of the
page it just captured — that counter is what stops a capture when recognition
fails or the reader sits on a text-free page. Deferring recognition to a later
batch would make the driver decide on a placeholder, which is exactly the class
of silent wrongness the constitution forbids. The cost is small: a page turn
already costs 1.5–4.0 s of settle time, against 50–200 ms of recognition, so
per-page requests add a few percent to a capture dominated by page turns. The
transport's batching (≤16 pages, ≤32 MiB, order-preserving, no partial results)
is still enforced and still used by the sibling pack; this pack simply does not
need it. If a future profile shows recognition dominating, batching can be
reintroduced *together with* a per-page ink signal for the loop.

**Rejected**: batched recognition with a placeholder returned to the driver
(makes the empty-page counter meaningless); capturing everything first and
recognising afterwards (loses the ability to stop on a failing reader).

### D11 — Page quality is a text-quality proxy, not OCR confidence

**Decision**: the sidecar records `quality_metric: "printable_char_ratio"` with
`quality_min`/`quality_mean` and a `suspect_pages` list, and there is no
`confidence` field anywhere.

**Rationale**: the OCR wire protocol returns text only — it has no confidence
channel — and the vendored client is deliberately not modified (FR-023). Writing
a field named "confidence" that is not a confidence would be exactly the kind of
plausible-looking lie the constitution forbids. What *is* available and useful
is the shape of the recognised text: a page that recognised as symbol soup has a
low printable-character ratio, which is a strong signal that the capture or the
recognition — not the book — went wrong. It does not detect chrome contamination
(that is a crop problem, caught by the capture probe), and the spec says so.

**Rejected**: adding a confidence channel to the wire protocol (touches a working
pack and its server for a signal this pack can approximate); reporting a
constant confidence (meaningless); dropping quality reporting altogether (leaves
the operator no way to find a bad page in a 300-page capture).

## Open questions carried into Phase 0

- Which key delivery mechanism the reader honours (A2) — decides whether a
  window manager is a hard requirement or a nicety.
- Whether the reader exposes a usable last-page affordance, or end-of-document
  is inferable only from the staleness threshold.
- Whether a small DPI increase is needed for OCR legibility, and what it costs
  in capture size (the OCR transport caps batch bytes).
