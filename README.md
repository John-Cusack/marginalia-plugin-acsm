# marginalia-plugin-acsm

Adobe-DRM (`.acsm`) acquisition pack for
[MarginaliaAI](https://github.com/John-Cusack/MarginaliaAI).

It fulfils a loan through **Adobe Digital Editions running under Wine**, drives
that reader page by page on a private virtual display, screenshots each page into
memory, OCRs it through the existing CUDA OCR service (with local fallback), and
ingests the captured text into the corpus as `drm_pdf`.

It is the sibling of [`marginalia-plugin-kindle`](https://github.com/John-Cusack/marginalia-plugin-kindle),
which does the same for Kindle Cloud Reader. Same posture, same OCR transport,
different reader.

## What this pack does _not_ do

- **It does not remove DRM.** There is no decryption code path. The encrypted
  container is opened only by Adobe Digital Editions, on your machine, with your
  activation — exactly as it would be if you opened it by hand. The pack turns
  the pages and reads the screen.
- **It never touches Adobe credentials.** Activation is a one-time interactive
  step you perform yourself in the Wine prefix.
- **It writes no page images to disk.** Frames live in memory from capture to
  OCR.
- **It makes no completeness claim.** OCR text is a reading copy; every capture
  result reports how the run ended and which pages recognised poorly.

Use it only for material you are authorised to read, and for personal
annotation. Captured text is not for redistribution.

## Requirements

| Component | Provided by |
|---|---|
| Python 3.11+ and a core install (`marginalia-ai` `>=0.6,<0.7`) | you |
| Wine, Adobe Digital Editions 4.5.x, `Xvfb`, a minimal window manager, `xdotool`, ImageMagick `import` (or `xwd`+`convert`) | you — the pack installs nothing |
| An OCR service (`KINDLE_OCR_BASE_URL`) or local `easyocr` (`[gpu-ocr]`) | you |

`research-engine-acsm-setup` reports the state of all of these and prints the
command that fixes each gap. It never installs or modifies anything.

## Install

```bash
uv build --wheel --out-dir dist
uv pip install --python /path/to/core/.venv/bin/python dist/marginalia_ai_plugin_acsm-*-py3-none-any.whl
# or, for development:
uv pip install -e ".[dev]"
```

Install into the **same environment that runs `research-engine serve`**. Core
never installs plugin code and cannot see a plugin in another virtual
environment. The wheel advertises one `research_engine.plugins` entry point
(`acsm = "acsm"`) and carries its schema-v2 manifest at `acsm/plugin.yaml`.

```bash
research-engine plugin audit acsm      # permissions, tools, manifest hash
research-engine plugin enable acsm --yes
```

The manifest declares `subprocess: true`: approving it lets the pack launch
Adobe Digital Editions under Wine with your privileges. Approve only if that is
acceptable on this host. After reinstalling a changed version or manifest, use
`research-engine plugin approve-upgrade acsm`.

## Tools

| Tool | What it does |
|---|---|
| `acsm.fulfill_document` | Opens an `.acsm` with Adobe Digital Editions and returns the fulfilled container's stable key |
| `acsm.scrape_document` | Captures and OCRs every page into `extracted/{key}.txt` plus a provenance sidecar. Does **not** ingest |
| `acsm.ingest_document` | Captures if needed and ingests as `drm_pdf`. Idempotent on the cache path |
| `acsm.check_document` | One document: fulfillment, cache, capture quality, review state, corpus |
| `acsm.list_documents` | Everything the pack knows, joined with the corpus |

## Runbook

```bash
# 1. Prefix, once
export WINEPREFIX=~/.local/share/acsm/wineprefix
research-engine-acsm-setup              # dependency report, installs nothing
research-engine-acsm-setup --activate   # opens the reader so you can activate Adobe
research-engine-acsm-setup --backup-prefix   # before any reader upgrade

# 2. OCR endpoint (if remote)
export KINDLE_OCR_BASE_URL=http://gpu-host:9883
curl -s "$KINDLE_OCR_BASE_URL/health"          # warm + cuda
```

```text
acsm.fulfill_document(acsm_path="/home/me/loans/book.acsm")
acsm.scrape_document(document_key="…")      # read ended_by and suspect_pages
acsm.ingest_document(document_key="…", edition_key="978-…")
acsm.check_document(document_key="…")
```

Then verify a quotation from the book against the corpus with `verify_quote`:
if the captured text is sound, it resolves as exact.

## Reading a capture result

| Field | Meaning |
|---|---|
| `ended_by: "observed_end"` | The reader signalled the end. `complete: true` |
| `ended_by: "stale"` / `"empty"` | Stopped at a threshold: likely the end, possibly a stuck reader or failed OCR. **Completeness is unverified** |
| `ended_by: "max_pages"` | Truncated on purpose by your page cap |
| `restarts` | How many times a frozen reader was restarted mid-capture |
| `suspect_pages` | Pages whose recognised text scored below the quality floor (printable-character ratio) — spot-check these first. It catches symbol-soup recognition, not chrome contamination; see `--capture-probe` for that |
| `ocr.remote: false` | The endpoint was unavailable; local recognition was used |

## Verified state

Claims about reader behaviour in this pack are gated on the Phase 0 measurements
recorded in `specs/001-acsm-drm-ingestion/research.md` (A1–A5). Until those rows
carry a verdict, the reader-facing constants in `acsm/capture.py` are marked
placeholders and the pack's reader path is **unverified end to end** — the unit
suite proves the logic around it, not the reader's cooperation.

Operator smoke test per release (cannot run in CI): one real `.acsm` →
fulfil → capture → ingest on a real prefix, then `verify_quote` against a
quotation you can see in the reader.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest tests/unit -q      # no display, no Wine, no OCR service
uv run ruff check acsm tests
uv run pytest tests/integration -q -rs   # needs Xvfb/xdotool/import
```

Design and planning artefacts live in `specs/001-acsm-drm-ingestion/`
(constitution: `.specify/memory/constitution.md`).

## Licence

Apache-2.0. Adobe, Kindle and Amazon are trademarks of their respective owners;
this project is not affiliated with them.
