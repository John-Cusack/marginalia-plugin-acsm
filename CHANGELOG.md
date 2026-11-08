# Changelog

## 0.1.0

Initial pack: `acsm.fulfill_document`, `acsm.scrape_document`,
`acsm.ingest_document`, `acsm.check_document`, `acsm.list_documents`, and the
`drm_pdf` document type (prose-window chunking).

- Fulfilment through Adobe Digital Editions under Wine on a private Xvfb
  display; no credential handling and no decryption code path.
- Page capture with frame-digest turn detection, separate staleness and
  emptiness thresholds, and bounded reader restarts.
- OCR through the shared CUDA service with whole-batch local fallback, batched
  within the transport's page/byte limits and order-preserving.
- Atomic cache writes (`extracted/{key}.txt` + provenance sidecar) and
  idempotent ingestion on the cache path.
- `research-engine-acsm-setup` dependency report, activation helper, capture
  probe, and prefix backup/restore. It installs nothing.

### Unverified in this release

The reader-behaviour assumptions A1–A5 in
`specs/001-acsm-drm-ingestion/research.md` are **unmeasured**: no real loan has
been fulfilled through this pack yet on a host with Wine and Adobe Digital
Editions. Consequently:

- the reader window match, display geometry and crop constants in
  `acsm/capture.py` are placeholders pending the Phase 0 probe;
- the end-to-end reader path is unverified; only the logic around it is tested;
- the operator smoke test in the README must be run and recorded before this
  release is relied on for real documents.
