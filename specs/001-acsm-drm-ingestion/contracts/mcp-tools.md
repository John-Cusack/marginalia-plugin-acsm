# MCP Tool Contracts: ACSM / Adobe-DRM PDF ingestion

**Feature**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07

Every response is a JSON object with a `status` field. Input schemas are shipped
twice — in `contracts/plugin.yaml` (approved by the operator) and in the
`@tool(...)` decorator (served by core). A wheel contract test asserts they are
equal.

## Common response envelope

| `status` | Meaning | Required companion fields |
|---|---|---|
| `success` | The tool did its job | tool-specific result fields |
| `already_ingested` | Ingestion was a no-op because the document exists | `document_id`, `document` |
| `operator_action_required` | A human must act before this can proceed | `action` (what to do), `detail` |
| `error` | The pack could not do its job | `error_type`, `message` |

`error_type` values: `config` (permission or client missing), `empty_text`,
`timeout`, `dependency` (external tool missing), `reader` (reader misbehaved),
`ocr`, plus the exception class name as a last resort.

**Never** return `error` for a human-blocked path (FR-018); `dependency` and
`operator_action_required` exist precisely so those are distinguishable by a
caller that is not a human reading the message.

---

## `acsm.fulfill_document`

**Input**: `acsm_path` (required), `loan_expires_at`, `force_refill`.

**Output** (`success`):

```json
{
  "status": "success",
  "document_key": "urn:uuid:…",
  "key_source": "rights_metadata",
  "container": "/prefix/library/book.pdf",
  "title": "…",
  "staged_acsm": "/data/acsm/inbox/3f9a1c2b8d4e.acsm",
  "already_fulfilled": false,
  "wine_version": "…",
  "reader_version": null
}
```

**Failures**

| Condition | Status | `action` / `message` |
|---|---|---|
| `acsm_path` missing / not a file | `error` (`config`) | message names the path |
| Prefix missing or reader not installed | `operator_action_required` | run `research-engine-acsm-setup` |
| Prefix not activated | `operator_action_required` | run `research-engine-acsm-setup --activate` |
| Loan expired at the storefront | `operator_action_required` | re-borrow or re-download the `.acsm` |
| Download did not produce a container in time | `error` (`timeout`) | message names the prefix library path searched |
| Rights metadata absent | `success` | `key_source` is `content_hash` |
| Container already known (no `force_refill`) | `success` | `already_fulfilled: true` |

**Side effects**: stages a copy of the `.acsm` in the pack data dir's `inbox/`;
launches the reader; the reader writes into the operator's prefix. Nothing else.

---

## `acsm.scrape_document`

**Input**: `document_key` (required), `page_delay_min`, `page_delay_max`,
`max_pages`, `max_consecutive_stale`, `max_consecutive_empty`,
`force_rescrape`.

**Output** (`success`):

```json
{
  "status": "success",
  "document_key": "…",
  "title": "…",
  "page_count": 312,
  "ended_by": "stale",
  "complete": false,
  "restarts": 1,
  "chars": 984123,
  "text_preview": "…",
  "suspect_pages": [17, 88],
  "ocr": {"model": "english_g2", "remote": true, "quality_mean": 0.93},
  "output_path": "/data/acsm/extracted/<key>.txt",
  "reused_cache": false
}
```

`complete` is `true` only when `ended_by == "observed_end"`. Consumers must not
treat a successful capture as a complete document otherwise (SC-003).

**Failures**

| Condition | Status | Notes |
|---|---|---|
| No sidecar and no container found for the key | `error` (`config`) | message says to fulfill first |
| External tool missing | `operator_action_required` | names the tool and the setup command |
| Reader produced no text at all | `error` (`empty_text`) | `page_count` and `ended_by` included; nothing written to cache |
| OCR endpoint unreachable | `success` | local fallback used; `ocr.remote == false` in the result and sidecar |
| Cache exists and `force_rescrape` is false | `success` | `reused_cache: true`, plus the recorded `ended_by`/`page_count` |

**Side effects**: writes `extracted/{key}.txt` and `.json` atomically, or
nothing. Never writes a page image.

---

## `acsm.ingest_document`

**Input**: `document_key` (required), `title`, `edition_key`, `reviewed`,
`force_rescrape`, `force_reingest`, `quality_floor`.

**Output** (`success` / `already_ingested`):

```json
{
  "status": "success",
  "document_key": "…",
  "document_id": "…",
  "source": "/data/acsm/extracted/<key>.txt",
  "chars": 984123,
  "page_count": 312,
  "ended_by": "observed_end",
  "reviewed": false,
  "suspect_pages": [17, 88],
  "passage_count": 2140
}
```

**Failures**

| Condition | Status | Notes |
|---|---|---|
| `ingestion` client absent | `error` (`config`) | names `permissions.ingest=true` |
| No cache and no container to capture | `error` (`config`) | says to fulfill first |
| Capture produced no text | `error` (`empty_text`) | same detail as `scrape_document` |
| Same source already ingested, no `force_reingest` | `already_ingested` | returns the existing document |

**Contract notes**

- The full captured text is handed over unmodified; core chunks it (FR-015).
- Metadata always carries `acsm_key`, `key_source`, `page_count`, `ocr`,
  `reviewed`, `source_format`; `edition_key` and `container` only when known
  (FR-017).
- `reviewed` defaults to `false`; the pack never flips it (constitution §I).

---

## `acsm.check_document`

**Input**: `document_key` (required).

**Output**:

```json
{
  "status": "success",
  "document_key": "…",
  "state": "captured_unreviewed",
  "partial": false,
  "on_disk": true,
  "disk": {"path": "…", "size_bytes": 984123, "modified_at": 1762521600.0},
  "capture": {"page_count": 312, "ended_by": "stale", "restarts": 1,
              "scraped_at": "…", "title": "…", "container": "…"},
  "quality": {"quality_min": 0.41, "quality_mean": 0.93,
              "suspect_pages": [17, 88], "reviewed": false},
  "ingested": true,
  "documents": [{"document_id": "…", "title": "…"}]
}
```

`state` is one of `unfulfilled`, `captured_unreviewed`, `captured_reviewed`,
`ingested` (data-model.md). `partial` mirrors
`ended_by in {"stale", "empty", "cancelled"}`.

**Failures**: a corpus lookup failure is reported as a `warning` field with
`ingested: false`, not as an `error` — the disk-side answer is still useful.

---

## `acsm.list_documents`

**Input**: `include_ingested` (default `true`).

**Output**:

```json
{
  "status": "success",
  "count": 3,
  "documents": [
    {"document_key": "…", "title": "…", "page_count": 312, "ended_by": "observed_end",
     "reviewed": false, "partial": false, "characters": 984123,
     "output_path": "…", "ingested": true, "document_id": "…"}
  ]
}
```

Ordering: most recently scraped first. Corrupt or unreadable sidecars are listed
with `"unreadable": true` rather than omitted — a cache the pack cannot read is
exactly what the operator needs to know about.
