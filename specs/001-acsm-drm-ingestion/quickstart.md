# Quickstart: ACSM / Adobe-DRM PDF ingestion

**Feature**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07

Two runbooks: **developer** (build and test the pack with no reader present) and
**operator** (the real thing, on a host with Wine and a loan).

## Developer

```bash
git clone <pack repo> && cd marginalia-plugin-acsm
uv venv && uv pip install -e ".[dev]"

# Unit suite: no display, no Wine, no OCR service. This is the CI gate.
uv run pytest tests/unit -q
uv run ruff check acsm tests

# Build the artefact and inspect what the operator would approve.
uv build --wheel --out-dir dist
uv run python -c "
from research_engine_sdk import parse_manifest
m = parse_manifest('acsm/plugin.yaml')
print(m.plugin_id, [t.id for t in m.provides.mcp_tools])"
```

Integration tests need a display and `xdotool`; they skip with a printed reason
when either is missing:

```bash
uv run pytest tests/integration -q     # add -rs to see skip reasons
```

Phase 0 (A1–A5) is **not** part of this loop: it needs an activated prefix and a
real loan. Its verdicts live in `specs/001-acsm-drm-ingestion/research.md` and
gate every reader-behaviour claim in code and docs.

## Operator

### 1. Prerequisites (you own these; the pack installs nothing)

| Component | Why |
|---|---|
| Python 3.11+ and a core install (`marginalia-ai` `>=0.6,<0.7`) | The corpus this ingests into |
| Wine | Runs Adobe Digital Editions |
| Adobe Digital Editions 4.5.x | Opens the DRM container — the only thing that can |
| `Xvfb` | The private display the pack drives |
| A minimal window manager (e.g. `matchbox-window-manager`) | Input focus for synthetic keys |
| `xdotool` | Page turns |
| ImageMagick `import` (or `xwd` + `convert`) | Frame capture |
| The OCR service (or local `easyocr`) | Recognising pages |

### 2. Install and enable

```bash
pip install dist/marginalia_ai_plugin_acsm-<version>-py3-none-any.whl
research-engine plugin audit acsm      # permissions, tools, manifest hash
research-engine plugin enable acsm --yes
```

Install into the **same environment that runs `research-engine serve`**. Core
never installs plugin code and cannot see a plugin in another virtual
environment.

Read the audit output before approving. `subprocess: true` means this pack can
launch Adobe Digital Editions under Wine with your privileges.

### 3. Prepare the prefix, once

```bash
export WINEPREFIX=~/.local/share/acsm/wineprefix
# install Wine + ADE yourself, then:
research-engine-acsm-setup              # reports every dependency, installs nothing
research-engine-acsm-setup --activate   # opens the reader so YOU can activate Adobe
```

The pack never asks for, stores, or transmits Adobe credentials. Activation is
yours, once, in the prefix. Back the prefix up before any reader upgrade
(`--backup-prefix`); Adobe activations are limited, so deactivate before
deleting a prefix.

### 4. Fulfil a loan

```bash
export KINDLE_OCR_BASE_URL=http://gpu-host:9883   # if using the remote OCR service
curl -s "$KINDLE_OCR_BASE_URL/health"              # must report warm + cuda
```

Then, from your MCP client:

```text
acsm.fulfill_document(acsm_path="/home/me/loans/book.acsm")
→ {document_key: "…", key_source: "rights_metadata", container: "…", title: "…"}
```

If this returns `operator_action_required`, do what `action` says — usually the
activation step. If `key_source` is `content_hash`, remember the key changes if
you re-download the same loan.

### 5. Capture

```text
acsm.scrape_document(document_key="…")
→ {page_count: 312, ended_by: "observed_end", complete: true, suspect_pages: []}
```

**Read the result before moving on.** `complete: true` means the reader itself
signalled the end. `ended_by: "stale"` or `"empty"` means the capture stopped at
a threshold: check `page_count` against the last page number you see in the
reader, and check `suspect_pages` for pages that may have captured
badly. Nothing is written to the cache when no text was captured.

### 6. Ingest

```text
acsm.ingest_document(document_key="…", edition_key="978-…")
→ {status: "success", document_id: "…", passage_count: 2140}
```

Set `edition_key` (the print ISBN is ideal). Captured OCR text is a reading
copy; the edition key is what citations resolve against. Re-running is a no-op
that returns the existing `document_id`.

### 7. Inspect

```text
acsm.check_document(document_key="…")
→ {state: "captured_unreviewed", partial: false, quality: {...}, ingested: true}
acsm.list_documents()
```

`suspect_pages` come from a text-quality proxy, not an OCR confidence score —
the recognition protocol does not report confidence, so the pack measures the
printable-character ratio of each page instead. It finds pages that recognised
as garbage, not pages contaminated by reader chrome (that is a crop problem,
caught by `--capture-probe`).

`reviewed: false` and any `suspect_pages` are surfaced here on purpose:
the corpus ingests OCR text unverified, so this is where you decide whether to
spot-check.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `operator_action_required: setup` | Prefix/reader/tool missing | `research-engine-acsm-setup` and follow its output |
| `operator_action_required: activate` | Prefix not activated | `research-engine-acsm-setup --activate` |
| Capture returns `ended_by: "stale"` at page 1 | Keys are not reaching the reader | Run `--capture-probe`; it reports whether frames change per turn |
| `page_count` far below the reader's page count | Staleness threshold too low, or the reader froze | Raise `max_consecutive_stale`; the sidecar's `restarts` shows freeze-recovery |
| Text full of toolbar words | The crop no longer matches this reader version | `--capture-probe`, then update the crop constant |
| `error: empty_text` with `page_count > 0` | Pages rendered but recognition produced nothing | Check OCR endpoint health; `ocr.remote: false` means it fell back |
| Ingest says `error: config` | Pack not enabled or core not restarted after enabling | `research-engine plugin list`, then restart the server |
| Documents duplicated in the corpus | `force_reingest` was used | Expected: the pack never merges by design |

## Limits (by design)

- No DRM circumvention, no credential handling: the reader opens the file, not
  the pack.
- No completeness guarantee: OCR text is a reading copy and the capture result
  says how it ended.
- No deduplication against the same work from another route (e.g. a Kindle
  copy); `edition_key` is the join key for citations.
- No EPUB-specific pagination work; scope is page-based documents.
- Region-restricted OCR endpoints fall back locally rather than failing the
  capture.
