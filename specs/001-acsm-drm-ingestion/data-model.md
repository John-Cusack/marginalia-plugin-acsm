# Data Model: ACSM / Adobe-DRM PDF ingestion

**Feature**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07

## Entities

### LoanArtifact

The operator's `.acsm` file. Input only.

| Field | Type | Notes |
|---|---|---|
| `acsm_path` | path | Operator-supplied; never modified |
| `sha256` | str | Hash of the file, used to name the staged copy in `inbox/` |
| `staged_path` | path | `inbox/{sha256[:12]}.acsm` — a copy, so the operator's original can move |

**Invariants**: the file is passed to the reader by path; the pack never parses
or rewrites it.

### FulfilledContainer

The encrypted document the reader downloaded into the Wine prefix.

| Field | Type | Notes |
|---|---|---|
| `container` | path | Inside the prefix's library directory; existence is asserted |
| `key` | str | `DocumentKey` |
| `title` | str | Read from the reader's library entry or the container's metadata |
| `fulfilled_at` | datetime (UTC) | |

**Invariants**: the pack never opens this file for reading; it is handed to the
reader as a path.

### DocumentKey

Stable identity for a fulfilled work. It is the primary key for the cache path
and for idempotency lookups.

| Field | Type | Notes |
|---|---|---|
| `value` | str | Rights-metadata identifier, or `sha256:<hex>` |
| `source` | enum | `"rights_metadata"` \| `"content_hash"` |

**Invariants**: `source == "content_hash"` implies the key changes if the
operator re-downloads the same work — reported in every result that carries the
key (FR-004). The key is a filesystem-safe token (hex or identifier characters
only); anything else is hash-derived instead.

### CaptureRecord (sidecar: `extracted/{key}.json`)

Everything known about one capture. Written atomically with the text.

```jsonc
{
  "schema": 1,
  "key": "…",
  "key_source": "rights_metadata",        // | "content_hash"
  "title": "…",
  "container": "/path/inside/prefix/library/book.pdf",
  "page_count": 312,
  "ended_by": "stale",                    // see EndOfRun
  "restarts": 1,
  "suspect_pages": [17, 88],
  "ocr": {
    "protocol": 1,
    "model": "english_g2",
    "model_version": "easyocr-1.7.2:craft:english_g2",
    "remote": true,                        // false ⇒ local fallback was used
    "quality_metric": "printable_char_ratio",
    "quality_min": 0.41,
    "quality_mean": 0.93,
    "quality_floor": 0.9
  },
  "reviewed": false,
  "scraped_at": "2026-11-07T12:00:00+00:00",
  "reader_version": null,                  // Phase 0 (A1) records the pin
  "wine_version": null,
  "tools": {"display": ":9", "screen": "1280x900x24", "crop": [0, 44, 1280, 860]}
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema` | int | yes | Sidecar schema version; readers must reject unknown values |
| `key`, `key_source` | str, enum | yes | Provenance of the identity (FR-004) |
| `title` | str | yes | Fallback `"Untitled"` is permitted, never empty |
| `container` | path | no | Absent when the container moved and could not be re-resolved |
| `page_count` | int | yes | Pages captured, not pages believed to exist |
| `ended_by` | enum | yes | Why the capture stopped (below) |
| `restarts` | int | yes | Reader restarts during the capture |
| `suspect_pages` | int[] | yes | Pages below the configured quality floor; may be empty |
| `ocr.*` | mixed | yes | Model identity is required for reproducibility; the quality values may be `null` when no page produced text. There is no confidence field on purpose: the wire protocol does not carry one (research D11) |
| `reviewed` | bool | yes | Set by ingest; never silently flipped to true |
| `scraped_at` | datetime | yes | UTC |
| `reader_version`, `wine_version` | str\|null | yes (nullable) | Filled by Phase 0 / setup probe |

**Invariants**: `page_count == 0` ⇒ no sidecar and no text file exist.
`ended_by != "observed_end"` ⇒ every consumer must treat completeness as
unverified.

### CachedText (`extracted/{key}.txt`)

The canonical captured text: exactly the string handed to
`ingestion.ingest_document`, so a later quotation can be re-verified against it
without the pack running (constitution §I).

**Invariants**: written atomically; never written when empty; never edited after
ingest.

### CorpusDocument

Core's document, of this pack's document type. The pack contributes metadata
only; storage and chunking belong to core.

| Metadata key | Source | Notes |
|---|---|---|
| `acsm_key` | `DocumentKey.value` | |
| `key_source` | `DocumentKey.source` | |
| `container` | `FulfilledContainer.container` | |
| `page_count` | `CaptureRecord.page_count` | |
| `ocr` | `CaptureRecord.ocr` | Model identity + text-quality summary |
| `reviewed` | tool argument | Surfaces OCR trust state on the corpus document |
| `source_format` | constant `"adobe_drm"` | Distinguishes this route from other packs |
| `edition_key` | tool argument | Present only when supplied; enables citation edition checks |
| `adobe_drm_loan` | added by the fulfillment tool when known | `expires_at` when the operator supplies it |

## Enumerations

### EndOfRun (`ended_by`)

| Value | Meaning | Completeness |
|---|---|---|
| `observed_end` | The reader itself signalled the end (last-page affordance) | Verified |
| `max_pages` | The caller's page cap was reached | Truncated on purpose |
| `stale` | Frames stopped changing for the staleness threshold | Unverified — likely end, possibly a stuck reader |
| `empty` | Pages produced no text for the emptiness threshold | Unverified — likely end, possibly OCR/capture failure |
| `cancelled` | An external cancellation propagated (KeyboardInterrupt/Timeout) | Partial |

`observed_end` is only ever produced from a genuine reader affordance; if Phase
0 finds none, it is simply never emitted and `stale` carries the end.

## State transitions

```text
              fulfill                capture                 ingest
  .acsm  ───────────────►  container ──────────►  text+sidecar ──────────►  corpus doc
   │                                      │                │                    │
   │ operator_action_required:            │ empty capture: │ already ingested:  │
   │  not activated / expired loan        │  write nothing │  return existing   │
   ▼                                      ▼                ▼                    ▼
  (no state change)                  (retry possible)  (idempotent no-op)  (metadata updated
                                                                            only on force_reingest)
```

Per-document state is therefore derivable from three facts, all of which the
check tool reports:

| State | Container fulfilled | Text cached | Sidecar says reviewed | In corpus |
|---|---|---|---|---|
| `unfulfilled` | ✗ | — | — | — |
| `captured_unreviewed` | ✓ | ✓ | ✗ | ✓ or ✗ |
| `captured_reviewed` | ✓ | ✓ | ✓ | ✓ or ✗ |
| `ingested` | ✓ | ✓ | either | ✓ |
| `partial` | ✓ | ✓ | either | ✓ or ✗ — `ended_by` is `stale`/`empty`/`cancelled` |

`partial` is a flag, not a lifecycle stage: it never clears by itself.

## Validation rules

- **VR-001**: A key MUST match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` or be
  `sha256:` + 64 hex digits; otherwise the key derives from content instead.
- **VR-002**: The sidecar MUST NOT be written without its text file, or vice
  versa; both are renamed into place from temporaries in the same operation.
- **VR-003**: `page_count` MUST equal the number of pages the driver reported,
  and MUST be `> 0` whenever a sidecar exists.
- **VR-004**: `suspect_pages` MUST contain only page indices in
  `[0, page_count)`, sorted and de-duplicated.
- **VR-005**: Ingest MUST NOT write a document whose text is empty or
  whitespace-only.
- **VR-006**: A sidecar whose `schema` is newer than the pack's known version
  MUST be reported as unreadable rather than partially interpreted.
