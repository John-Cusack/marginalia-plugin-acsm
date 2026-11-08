# Implementation Plan: ACSM / Adobe-DRM PDF ingestion

**Branch**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-acsm-drm-ingestion/spec.md`

**Upstream design**: `MarginaliaAI/docs/design/drm-pdf-ocr-ingestion-architecture.md`
and `MarginaliaAI/docs/implementation/drm-pdf-ocr/build-guide.md` (this plan is
the executable form of the build guide).

## Summary

A schema-v2 plugin pack (`marginalia-ai-plugin-acsm`) that fulfils an operator's
`.acsm` loan through Adobe Digital Editions under Wine, drives the reader on a
private Xvfb display page by page, screenshots each page into memory, OCRs it
through the existing CUDA OCR service (with local fallback), caches the text
atomically with a provenance sidecar, and hands the complete text to core's SDK
ingestion client as `document_type="drm_pdf"`.

The technical approach follows the sibling Kindle pack stage for stage, with one
structural change that drives everything else: **the page loop has no DOM to read
from**. Kindle detects a stuck page via repeated text; here the only signal is
the pixels, so the loop compares consecutive frame digests and keeps two
separate stopping counters (staleness vs emptiness). Every I/O in the loop is
injected, so all six transitions are testable without X, Wine, or the OCR
service.

## Technical Context

**Language/Version**: Python 3.11+ (target 3.11–3.13; developed on 3.14)

**Primary Dependencies**: `marginalia-ai-sdk>=0.6.1,<1`; `pillow`; `httpx`;
`structlog`; optional `easyocr==1.7.2` (`[gpu-ocr]` extra). External binaries
(not Python dependencies): `wine`, Adobe Digital Editions, `Xvfb`, a minimal
window manager, `xdotool`, ImageMagick `import` (or `xwd`+`convert`).

**Storage**: files under the injected `PluginContext.data_dir`
(`~/.research-engine/plugin-data/acsm`): `inbox/` for staged `.acsm` copies,
`extracted/{key}.txt` (canonical captured text) and `extracted/{key}.json`
(provenance sidecar). The Wine prefix is operator state, outside the data dir.

**Testing**: `pytest` + `pytest-asyncio`; fake injection for the loop, fakes for
the ingestion client, fixture PNGs and fixture `rights.xml`, copied upstream OCR
suite. No test in `tests/unit` requires a display, Wine, or the OCR service.

**Target Platform**: Linux host with a GPU OCR service (remote) or a local GPU;
the reader is Windows software under Wine.

**Project Type**: single-project Python distribution (plugin pack), sibling to
the other five packs; no service, no database.

**Performance Goals**: capture throughput dominated by page turn settle time
(1.5–4.0 s per page, jittered) and OCR batching (≤16 pages / ≤32 MiB per batch).
A 300-page document is a ~10–20 minute operation; no latency target applies.

**Constraints**:

- Rendered page images never touch disk (FR-006, SC-007).
- Cache writes are atomic (FR-011, SC-008).
- Runtime imports: `research_engine_sdk` only, never `research_engine.*` (FR-020).
- The pack installs nothing; setup reports and instructs (FR-021).

**Scale/Scope**: one operator, tens of documents, hundreds of pages each. Five
tools, one document type, ~9 modules, ~1.5k lines of implementation, ~1.2k lines
of tests.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Status |
|---|---|---|
| I. Canonical text is the product | Full text to `ingest_document`, no pre-chunking; sidecar carries confidence and `ended_by`; cache is byte-identical to what was ingested | PASS — FR-015, FR-010, FR-012, FR-017 |
| II. No circumvention, no credentials | No decryption code path exists; credentials never read/stored; page images memory-only; licensed-content warning on every operator surface | PASS — FR-002, FR-006; enforced by SC-007 test and setup/README copy |
| III. External tooling verified, never installed | Setup probes and instructs only | PASS — FR-021, FR-019 |
| IV. Page loop deterministic and injectable | `driver.py` takes capture/key-send/restart/clock; six transitions tested with no external tooling | PASS — FR-024, SC-004 |
| V. Plugin boundary discipline | Static manifest; SDK-only imports; inert until approved | PASS — FR-001, FR-019, FR-020; enforced by wheel contract test |
| VI. Honesty over completeness | `operator_action_required` for human-blocked paths; unverified reader claims banned | PASS — FR-018, FR-025; Phase 0 verdicts gate all reader claims |

No violations. Complexity tracking is therefore empty.

**Post-Phase 1 re-check**: unchanged. The design introduces no second
convention: the pack reuses the sibling's document-type/chunker pattern, the
sibling's OCR wire protocol, and core's existing ingestion client.

## Project Structure

### Documentation (this feature)

```text
specs/001-acsm-drm-ingestion/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 research and decisions
├── data-model.md        # Entities, sidecar schema, state transitions
├── quickstart.md        # Operator runbook
├── contracts/           # Tool + manifest + module contracts
│   ├── plugin.yaml      # The manifest as shipped (approval contract)
│   ├── mcp-tools.md     # Tool inputs, outputs, error taxonomy
│   └── module-interfaces.md
└── tasks.md             # Task list (Phase 2 output)
```

### Source Code (repository root)

```text
acsm/
├── __init__.py               # no import side effects
├── plugin.yaml               # schema-v2 manifest (copied from contracts/)
├── _paths.py                 # data-dir derivation, sidecar read/write
├── capture.py                # Xvfb + WM lifecycle, window lookup, grab, crop, digests
├── driver.py                 # page loop state machine (injected I/O)
├── fulfill.py                # wine/ADE launch, container wait, key derivation
├── acquire.py                # orchestration: drive -> OCR batches -> atomic cache
├── ocr_config.py             # vendored from kindle (env-driven settings)
├── ocr_wire.py               # vendored protocol v1
├── ocr_local.py              # vendored local EasyOCR
├── ocr_remote.py             # vendored remote batch client + circuit breaker
├── setup.py                  # research-engine-acsm-setup
└── tools/
    ├── fulfill_document.py   scrape_document.py  ingest_document.py
    ├── check_document.py     list_documents.py

tests/
├── unit/                     # no display, no Wine, no OCR service
│   ├── test_driver_state_machine.py
│   ├── test_capture_crop.py  test_paths.py   test_acquire_cache.py
│   ├── test_fulfill_key.py   test_ingest_document.py
│   ├── test_check_document.py  test_ocr_offload.py
│   └── test_vendored_protocol.py  test_setup_probe.py
└── integration/
    ├── test_stub_reader.py   # X11 plumbing against a slideshow, no ADE
    └── test_built_wheel.py   # discovery, permissions, tool catalogue, schemas
```

**Structure Decision**: single project at the repository root, matching the
sibling packs exactly (`acsm/` package + `tests/`), because core's discovery
reads `<import_package>/plugin.yaml` and the entry point must name the top-level
`acsm` package. No `src/` layout: the sibling packs and the repo-wide
convention both use a flat package directory, and a wheel that adds `src/`
would be the only one of six that differs.

## Complexity Tracking

No constitution violations; no entries.
