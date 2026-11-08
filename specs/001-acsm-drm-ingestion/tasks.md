---
description: "Task list for ACSM / Adobe-DRM PDF ingestion"
---

# Tasks: ACSM / Adobe-DRM PDF ingestion

**Input**: Design documents from `/specs/001-acsm-drm-ingestion/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`

**Tests**: Required by the constitution (§Development Workflow: tests ship with
the code), so every user-story phase includes its test tasks.

**Organization**: grouped by user story so each story is independently
implementable and testable. Paths are relative to the repository root.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelisable (different files, no dependency on an incomplete task)
- **[Story]**: US1–US5 from `spec.md`

---

## Phase 1: Setup (shared infrastructure)

- [x] T001 Initialise the repository: `git init`, `specify init --here --integration claude --script sh`, `.gitignore` (dist, `.venv`, `__pycache__`, `.ruff_cache`, `.pytest_cache`)
- [x] T002 [P] `pyproject.toml`: hatchling build, `acsm` package, entry point `research_engine.plugins` → `acsm = "acsm"`, console script `research-engine-acsm-setup = "acsm.setup:main"`, deps (`pillow`, `httpx`, `structlog`, `marginalia-ai-sdk>=0.6.1,<1`), extras `gpu-ocr`, `dev`, pytest asyncio config, ruff config
- [x] T003 [P] `acsm/plugin.yaml` copied byte-identical from `specs/001-acsm-drm-ingestion/contracts/plugin.yaml`
- [x] T004 [P] `acsm/__init__.py` with no imports (discovery must not execute code); `LICENSE`, `README.md`, `CHANGELOG.md`, `VENDORED_FROM` marker file for the OCR copy

**Checkpoint**: `uv build --wheel` succeeds and `python -c "from research_engine_sdk import parse_manifest; parse_manifest('acsm/plugin.yaml')"` prints five tools.

---

## Phase 2: Foundational (blocking all stories)

- [x] T005 `acsm/_paths.py` per `contracts/module-interfaces.md`: `inbox_dir`, `extracted_dir`, `text_path_for`, `sidecar_path_for`, `read_sidecar` (never raises; `None` on missing/malformed/newer schema), `write_cache` (atomic via temporaries + `os.replace`; raises `ValueError` on whitespace-only text; writes both files or neither)
- [x] T006 [P] `tests/unit/test_paths.py`: path derivation from an injected data dir; atomic write leaves both files; empty text refused; malformed and newer-schema sidecars read as `None`; no `Path.home()` literal in the module (source assertion)
- [x] T007 Vendor the OCR client from `marginalia-plugin-kindle`: `acsm/ocr_config.py`, `acsm/ocr_wire.py`, `acsm/ocr_local.py` (from `kindle/scraper/ocr.py`), `acsm/ocr_remote.py`; change only import paths and add the upstream-revision marker
- [x] T008 [P] Copy the upstream OCR test suite to `tests/unit/test_ocr_offload.py` with adjusted imports (batch order, fallback-on-failure, circuit opening, protocol mismatch, settings validation)
- [x] T009 [P] `tests/unit/test_vendored_protocol.py`: asserts the vendored `PROTOCOL`/`MAX_PAGES`/`MAX_BYTES` equal upstream's, `importorskip("kindle")`

**Checkpoint**: unit suite green with no external tooling; vendored protocol guard passes when the sibling pack is installed.

---

## Phase 3: US1 — fulfil a loan into a usable container (P1) 🎯

- [x] T010 `acsm/capture.py` scaffolding required by fulfill: `Display` context manager (Xvfb + minimal WM, private `DISPLAY`, torn down on exit), `available_tools()`, fixed-argv `_run()` helper with explicit `env`, `find_window`, `grab_png`, `crop_png`, `frame_digest`, `is_blank`, `send_start`, `send_turn` — every reader-specific constant single-sourced and annotated with the Phase 0 verdict that pins it
- [x] T011 `tests/unit/test_capture_crop.py`: `crop_png` box handling and size; `frame_digest` stability and sensitivity to a one-pixel change; `is_blank` on blank vs rendered fixture; `_run` never uses a shell and passes only the intended env (assert on the recorded argv/env)
- [x] T012 `acsm/fulfill.py`: `extract_rights_id` (plaintiff-parse of `META-INF/rights.xml`, no decryption), `document_key` with `sha256:` fallback and validation rule VR-001, `wait_for_container` (poll for a new container + reader idle, injected `sleep`), `fulfill` (fixed-argv `wine` launch, `FulfillError` naming the operator action for unactivated prefix and expired loan), `find_container`
- [x] T013 [P] `tests/unit/test_fulfill_key.py`: rights id from fixture XML; key from rights metadata; hash fallback and `key_source`; rejection of a non-conforming rights id (VR-001); `wait_for_container` picks only the *new* container and times out actionably
- [x] T014 `acsm/tools/fulfill_document.py`: handler + `@tool` decorator matching the manifest schema; stages the `.acsm` copy in `inbox/`; returns the `success` envelope from `contracts/mcp-tools.md`; maps reader failures to `operator_action_required`
- [x] T015 [P] `tests/unit/test_fulfill_tool.py` with a fake fulfill/driver seam: success envelope fields; `operator_action_required` for unactivated prefix; `error/config` for a missing `.acsm`; `force_refill` behaviour; no credential argument accepted anywhere

**Checkpoint**: a key can be produced and validated from fixtures alone; the real reader path is exercised only by the operator runbook.

---

## Phase 4: US2 — capture every page (P1)

- [x] T016 `acsm/driver.py`: `PageCapture`, `DriveResult`, `drive_document` with fully injected I/O per `contracts/module-interfaces.md` — settle-then-capture, digest advancement, stale retries, separate empty counter, stall/restart with bounded restarts, `max_pages`, `is_last_page` → `observed_end`, jittered delays via injected `rng`
- [x] T017 `tests/unit/test_driver_state_machine.py`: six transitions — steady progress; lost keypress recovers within the stale budget; stale budget exhausted → `ended_by="stale"`; empty run → `"empty"` and reset on the next non-empty page; stall → `on_restart` called and the run resumes with `restarts` counted; `max_pages` → `"max_pages"`; `is_last_page` → `"observed_end"`. Also: a blank frame does not produce a page; `accept_page` is not called for a repeated digest; delays stay inside `[min, max]`
- [x] T018 `acsm/acquire.py`: `ScrapeOutcome`, `scrape_to_disk` — display lifecycle, window lookup, `drive_document` wiring, OCR batching within transport limits with order preserved, per-page confidence aggregation and floor, `write_cache` only when text is non-empty, reader-version capture, `container` recording
- [x] T019 [P] `tests/unit/test_acquire_cache.py`: with a fake driver and fake OCR client — batch sizes never exceed limits and order is preserved; nothing written on empty text and `output_path is None`; sidecar fields complete (schema, key provenance, `ended_by`, `restarts`, confidence, `low_confidence_pages` sorted/deduped per VR-004); `remote: false` recorded when the client fell back
- [x] T020 `acsm/tools/scrape_document.py`: handler + decorator; `reused_cache` path when a sidecar exists and `force_rescrape` is false; `complete` computed from `ended_by`; `empty_text` error carries `page_count`/`ended_by`; dependency failures → `operator_action_required`
- [x] T021 [P] `tests/unit/test_scrape_tool.py`: cache reuse short-circuits the driver (assert the driver was not called); `force_rescrape` calls it; `complete: true` only for `observed_end`; missing container → `error/config`

**Checkpoint**: SC-003 and SC-004 hold — every transition covered, and no result claims completeness without `observed_end`.

---

## Phase 5: US3 — ingest the captured text (P1)

- [x] T022 `acsm/tools/ingest_document.py`: idempotency via `find_existing(source=resolved_cache_path)` before any capture; capture-if-needed; metadata per data-model; full text handed to `ingest_document(document_type="drm_pdf", …)` unmodified; `reviewed` passed through (never forced true); `already_ingested` envelope; `error/config` when the ingestion client is absent
- [x] T023 [P] `tests/unit/test_ingest_document.py` with a `_FakeIngestion` (sibling-pack pattern): existing-document short circuit calls neither the driver nor `ingest_document`; success path asserts the exact kwargs (`document_type`, `source` == resolved cache path, full text, metadata keys); `force_reingest` skips the lookup; empty text → `empty_text` error and no ingestion call; absent client → `config` error; `edition_key` and `reviewed` propagate

**Checkpoint**: SC-002 and SC-005 are demonstrable — idempotent re-ingest, and a quote from the ingested text verifies against the stored canonical text.

---

## Phase 6: US4 — inspect state without doing work (P2)

- [x] T024 `acsm/tools/check_document.py`: `state` from the data-model table, `partial` flag, disk/capture/quality sections, corpus lookup with a `warning` on failure (never `error`); unreadable sidecar reported as such
- [x] T025 [P] `acsm/tools/list_documents.py`: cache ∪ corpus rows, most recent first, `include_ingested` honoured, unreadable sidecars listed with `"unreadable": true`
- [x] T026 [P] `tests/unit/test_check_document.py`: pre-fulfillment (no cache) → `unfulfilled` without error; unreviewed cache → `captured_unreviewed` with `low_confidence_pages` at the top level; `ended_by="stale"` → `partial: true`; corpus failure → `warning` and `ingested: false`; list ordering, `include_ingested=false`, unreadable sidecar visibility

**Checkpoint**: an operator can tell completeness and trust state before ingesting.

---

## Phase 7: US5 — set up and verify the environment (P2)

- [x] T027 `acsm/setup.py`: `Probe`, `probe_environment()` (wine, prefix, reader executable, `Xvfb`, WM, `xdotool`, capture tool, OCR endpoint health, optional local OCR), `main(argv)` with modes `--check` (default), `--activate`, `--capture-probe`, `--backup-prefix`, `--restore-prefix`; non-zero exit when a required probe is missing; prints remediations; installs nothing
- [x] T028 [P] `tests/unit/test_setup_probe.py`: with every binary absent, each probe reports missing with a remediation and `main` exits non-zero; probe mode performs no writes (assert on a read-only data dir); `--capture-probe` reports digests per turn using an injected capture
- [x] T029 [P] `README.md`: install, enable, prefix preparation, the operator runbook from `quickstart.md`, licensed-content warning, the reader-behaviour claims limited to measured Phase 0 verdicts
- [x] T030 [P] `CHANGELOG.md`: `0.1.0` entry — initial pack; note explicitly which claims remain unverified until Phase 0 is measured

**Checkpoint**: SC-001 and SC-006 hold — one command diagnoses the environment, and the runbook is reproducible from cold.

---

## Phase 8: Polish and cross-cutting verification

- [x] T031 [P] `tests/integration/test_stub_reader.py`: real `capture.py` + real `xdotool` against an ImageMagick slideshow on Xvfb (no reader); asserts frame digests advance per turn and the loop stops cleanly; skips with a printed reason when `Xvfb`/`xdotool`/`import` are absent
- [x] T032 [P] `tests/integration/test_built_wheel.py`: entry point exposes the package without importing it; manifest permissions match the tools' needs; exactly five tools and one document type; decorator schema equals manifest schema for every tool; handler importable with core absent
- [x] T033 `ruff check acsm tests` clean; `pytest tests/unit -q` green; `uv build` succeeds; `twine check --strict` (if available) passes
- [x] T034 Assert SC-007 on the unit suite: a fake-driven capture writes only the two cache paths (instrument `Path.write_text`/`os.replace` and assert the observed set)
- [ ] T035 Update `specs/001-acsm-drm-ingestion/research.md` with the Phase 0 verdict table state (still `UNMEASURED` unless measured) and list the operator-run release gate — **PENDING: needs a host with the reader**
- [ ] T036 Record the operator smoke procedure in `README.md` — documented; **PENDING: not executed (needs a real .acsm)**

---

## Dependencies and ordering

```text
T001–T004 (setup)
   └─► T005–T009 (foundational: paths, vendored OCR)
          ├─► T010–T015 (US1 fulfil)      ─┐
          ├─► T016–T021 (US2 capture)     ─┼─► T022–T023 (US3 ingest)
          │                                │        └─► T024–T026 (US4 inspect)
          │                                │
          └─► T027–T030 (US5 setup)  ──────┘
                                            └─► T031–T036 (polish)
```

- US1 and US2 are parallelisable after Phase 2 (different modules), but US2's
  `scrape_document` reuses US1's container lookup, so implement US1 first unless
  two agents are active.
- US3 depends on US2's cache writer; US4 depends on US1 (fulfilment state) and
  US3 (ingested state).
- US5 touches only `setup.py`, `README.md`, `CHANGELOG.md` and is parallelisable
  with US1–US4 after Phase 2.

## MVP

Phase 1 + Phase 2 + Phase 3 + Phase 4 + Phase 5 = a working
fulfil → capture → ingest path (spec's P1 stories). US4 and US5 make it
operable; Phase 8 makes it releasable.

---

## Completion log (2026-11-07)

All tasks above are implemented except where noted. Verification on this host:

```
pytest tests -q        → 156 passed, 4 skipped
ruff check acsm tests  → All checks passed
uv build --wheel       → marginalia_ai_plugin_acsm-0.1.0-py3-none-any.whl
twine check --strict   → PASSED
research-engine-acsm-setup --prefix …  → reports, installs nothing, exit 1
```

Skipped tests are honest, not hidden: the vendored-protocol guard (sibling pack
not installed here) and three X11 integration tests (this host has `wine` and
`tesseract` but no `Xvfb`, window manager, `xdotool`, or ImageMagick `import`).

### Deviations from the plan

| Task | What changed | Why |
|---|---|---|
| T008 | The upstream OCR suite could not be copied: it drives the sibling's ASGI server and its scraper, neither of which this pack ships. Replaced by a client-side suite covering the same behaviours (order, whole-batch fallback, circuit breaking, transient retry, malformed protocol, settings validation) plus the local path this pack does ship | Copying tests for code we do not ship would prove nothing about this package |
| T017 | `accept_page` may be a coroutine, awaited before the page's counters move; the driver also takes `digest_of`, `is_blank`, `clock` and `stall_timeout` | OCR is I/O, and the loop's empty-page counter must see real recognition output; blank-frame and stall handling need injected time to be testable |
| T019/T034 | SC-007 asserts that every file written during a capture lies inside the cache directory (temporaries included) rather than equalling an exact path set | The invariant worth protecting is "no page image and nothing outside the cache", not the spelling of a temp file |
| T024 | `confidence_*` fields became a text-quality proxy: `quality_metric`, `quality_min`, `quality_mean`, `quality_floor`, `suspect_pages`; tool argument `confidence_floor` → `quality_floor` (default 0.9) | The OCR wire protocol carries no per-page confidence (research D11); a field named "confidence" would be a plausible-looking lie |
| FR-013 | Amended: recognition is requested one page at a time, within the transport's limits, order-preserving, no partial results | Batching would force the loop to decide on a placeholder (research D10) |
| T035 | Phase 0 verdicts remain **UNMEASURED** | This host has no Xvfb/WM/xdotool/ImageMagick, no Adobe Digital Editions, and no loan |
| T036 | The operator smoke procedure is documented in the README; it has **not** been executed, and no result claims otherwise | It needs a real `.acsm` and an activated prefix |

### Not done (out of scope for this run)

- No commit, remote, or release tag: the repository was created here and has no
  configured remote.
- Phase 0 (A1–A5), and therefore the reader-facing constants in
  `acsm/capture.py`, remain placeholders pending a host with the reader.
