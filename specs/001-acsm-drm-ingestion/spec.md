# Feature Specification: ACSM / Adobe-DRM PDF ingestion

**Feature Branch**: `001-acsm-drm-ingestion`

**Created**: 2026-11-07

**Status**: Draft

**Input**: User description: "I need a process to ingest encrypted PDFs and open them with Adobe in Wine and then screenshot the pages and use OCR to ingest them like I do with Kindle in this repo."

**Context**: a sibling pack (`marginalia-plugin-kindle`) already ingests books by
driving an authorised web reader page-by-page and handing the captured text to
core. This feature builds the equivalent route for operator-owned Adobe-DRM
loans (`.acsm` fulfillment files), where the authorised reader is Adobe Digital
Editions running under Wine.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Fulfill a loan into a usable container (Priority: P1)

An operator has a `.acsm` file from a library or storefront. They hand it to the
pack and get back a stable document key plus the location of the fulfilled
container that Adobe Digital Editions downloaded into the Wine prefix.

**Why this priority**: nothing downstream can exist without a fulfilled
container and a stable identity. It is also the stage where the human-only
dependency (Adobe activation) surfaces, so it must be explicit from day one.

**Independent Test**: with a real, activated prefix, give the tool one `.acsm`
and assert it returns a key, a container path that exists, and no credential
input of any kind. Also assert the failure path: an un-activated or expired
state returns `operator_action_required` naming the setup command.

**Acceptance Scenarios**:

1. **Given** an activated prefix and a valid `.acsm`, **When** the operator calls
   `acsm.fulfill_document`, **Then** the response contains a document key, the
   container path, whether the key came from embedded rights metadata or from a
   content hash, and a title.
2. **Given** a prefix that was never activated, **When** fulfillment is
   attempted, **Then** the response is `operator_action_required` naming
   `research-engine-acsm-setup` and no credentials were requested or stored.
3. **Given** a loan that has expired, **When** the container is opened, **Then**
   the response is `operator_action_required` and any previously cached text is
   left untouched.

---

### User Story 2 - Capture every page of the document (Priority: P1)

An operator runs a capture for a fulfilled document. The pack opens it in Adobe
Digital Editions on a private virtual display, turns pages, OCRs each page, and
reports honestly what it captured: how many pages, how the run ended, and how
confident the OCR was per page.

**Why this priority**: the page loop is the whole mechanism, and it is the part
most likely to fail silently (a lost keypress looks exactly like a repeated
page). Without the loop and its honesty reporting, nothing else matters.

**Independent Test**: unit-test the loop's state machine with scripted frames —
progress, a lost keypress that recovers, a run of stale turns, a run of empty
pages, a mid-run stall that restarts the reader, and a maximum-page cap — with
no display and no reader present. Then run one real capture and confirm the
cached text matches what was on screen.

**Acceptance Scenarios**:

1. **Given** a fulfilled document, **When** capture runs, **Then** it writes the
   captured text and a sidecar record containing page count, `ended_by`,
   restarts, OCR model identity, and a per-page text-quality summary.
2. **Given** a page turn that does not register, **When** the next frame is
   identical to the previous one, **Then** the pack re-sends the turn rather
   than recording a duplicate page.
3. **Given** the reader freezes mid-document, **When** no frame and no new text
   arrive for the stall timeout, **Then** the pack restarts the reader and
   continues, and the number of restarts is reported.
4. **Given** a run that ends because pages stopped changing, **When** the
   operator inspects the result, **Then** `ended_by` says so, and the pack does
   not claim the document was captured in full.
5. **Given** zero captured text, **When** capture finishes, **Then** nothing is
   written to the cache and the result distinguishes "reader never rendered"
   from "text was empty".

---

### User Story 3 - Ingest the captured text as a corpus document (Priority: P1)

An operator ingests a captured document into the corpus so its quotations become
citable. Re-running ingestion for the same document is a no-op that returns the
existing document.

**Why this priority**: this is the point of the feature — the corpus is what the
rest of the research tooling consumes. It must also be idempotent, because a
capture is expensive.

**Independent Test**: drive the handler with a fake ingestion client: assert the
existing-document short circuit happens before any capture, assert the full text
is handed over with `document_type` equal to the pack's declared document type
and a source path identical to the cache path, and assert an unavailable
ingestion client produces a configuration error instead of a crash.

**Acceptance Scenarios**:

1. **Given** cached text for a document, **When** the operator ingests it,
   **Then** a document is created whose source is the cache path and whose
   metadata carries the document key, OCR identity, page count, review state,
   and (when supplied) an edition key.
2. **Given** a document already ingested from the same cache path, **When**
   ingestion is re-run, **Then** the existing document identifier is returned
   and no second document is created.
3. **Given** no cached text and no reader available, **When** ingestion is
   attempted, **Then** the response explains which prerequisite is missing.
4. **Given** the pack has no ingest permission, **When** ingestion is
   attempted, **Then** the response is a configuration error naming the
   permission.

---

### User Story 4 - Inspect state without doing work (Priority: P2)

An operator checks one document, or lists documents, to see what is fulfilled,
what is cached, whether the cache has been reviewed, whether the OCR looked
weak, and whether it is already in the corpus.

**Why this priority**: the corpus posture is to ingest unverified OCR text and
surface it rather than block on it — surfacing requires an inspection surface.
Nothing else depends on this, hence P2.

**Independent Test**: seed a data directory with a cache and a sidecar with low
quality pages, call the check tool with a fake ingestion client, and assert
review state and the suspect-page list appear at the top level of the response.

**Acceptance Scenarios**:

1. **Given** a cache that has never been reviewed, **When** the operator checks
   it, **Then** the response says unreviewed and lists the suspect pages.
2. **Given** neither cache nor corpus record, **When** the operator checks,
   **Then** the response says so without erroring.
3. **Given** several documents, **When** the operator lists them, **Then** each
   row shows cache presence and whether it is in the corpus.

---

### User Story 5 - Set up and verify the environment (Priority: P2)

An operator runs one command to learn whether Wine, Adobe Digital Editions, the
virtual display, the window manager, the input tool, the capture tool, and the
OCR endpoint are present and usable, and gets exact remediation steps for
anything missing. A second mode measures the crop and the page-turn mechanism
against a real document so a reader upgrade can be re-verified without code
changes.

**Why this priority**: the pack cannot install Adobe software, so a broken
environment must be diagnosable in one step. It is P2 only because P1 stories
can be developed and tested against fakes without it.

**Independent Test**: run the setup command in an environment missing every
external tool and assert it reports each one as missing with a remediation
command, exits non-zero, and installs nothing.

**Acceptance Scenarios**:

1. **Given** any environment, **When** setup runs, **Then** it reports the state
   of every external dependency and never installs or modifies anything.
2. **Given** a missing dependency, **When** setup runs, **Then** that dependency
   is named with the command that would fix it.
3. **Given** an operator who wants to activate Adobe, **When** they run the
   activation mode, **Then** a window is opened for them and no credential is
   requested by the pack.
4. **Given** a fulfilled document, **When** the operator runs the capture probe,
   **Then** the command reports the frame digest for consecutive page-turn
   attempts, which is how crop constants and the key mechanism are re-measured.

---

### Edge Cases

- **Key delivery to an unfocused window.** Synthetic key events may go to
  whatever holds input focus; on a bare virtual display no component holds it.
  Both delivery mechanisms must be measured during Phase 0 and the working one
  recorded, because silently dropping every keypress looks like a blank book.
- **Chrome in the captured region.** If the reader's toolbar or sidebar lies
  inside the crop, every page's OCR text gains the same junk header/footer. A
  stale crop after a reader upgrade must fail loudly rather than quietly
  polluting the corpus.
- **A blank frame.** An unrendered page and a legitimately blank page (photo
  insert, blank verson) are indistinguishable by emptiness alone; emptiness must
  be tested differently from staleness.
- **Renewal and re-fulfillment.** A renewed loan produces a new container for
  the same work. Cached text must remain usable, and re-capture must be an
  explicit choice.
- **The same work already in the corpus from another source.** A book ingested
  through the Kindle route and through this route is two documents; the pack
  must not attempt to merge or deduplicate them.
- **Identity from a content hash.** When the container carries no usable rights
  metadata, the key is content-derived, so a re-download yields a new key. This
  must be reported in the result, not hidden.
- **An OCR endpoint that is not warm.** The service must be verified reachable
  and ready before a long capture starts, or a mid-run failure will surface as
  a partial book.
- **Region restrictions.** OCR of captured pages may be refused by the
  configured endpoint; the pack must fall back locally and report that the
  remote path was unavailable rather than failing the capture.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The pack MUST contribute exactly five MCP tools — fulfill,
  capture, ingest, check, list — plus one document type whose default chunking
  is prose-window chunking.
- **FR-002**: The pack MUST NOT store, request, or transmit Adobe credentials,
  and MUST NOT decrypt or otherwise bypass an access control.
- **FR-003**: Fulfillment MUST open the `.acsm` only through Adobe Digital
  Editions running under Wine, using a prefix owned by the operator.
- **FR-004**: The fulfilled container MUST be identified by a stable key derived
  from embedded rights metadata when available, and MUST report when the key is
  instead content-derived.
- **FR-005**: Capture MUST run the reader on a private virtual display that the
  pack starts and stops, and MUST NOT reuse or disturb the operator's desktop.
- **FR-006**: Capture MUST NOT write any rendered page image to disk, nor log
  page images, nor send them anywhere except the configured OCR endpoint.
- **FR-007**: Capture MUST detect an unregistered page turn by comparing
  consecutive frames, and MUST retry the turn a bounded number of times before
  concluding the document has ended.
- **FR-008**: Capture MUST distinguish an empty page run from a stale page run,
  and MUST end the capture on either after its configured threshold.
- **FR-009**: Capture MUST restart the reader and resume after a stall, MUST
  bound the number of restarts, and MUST report how many occurred.
- **FR-010**: Capture MUST record, per document: page count, how the run ended,
  OCR model identity, a per-page text-quality summary (printable-character
  ratio), and the list of pages below the configured quality floor. The wire
  protocol carries no per-page confidence, so the pack MUST NOT claim one.
- **FR-011**: Capture MUST write its cache atomically, so an interrupted capture
  cannot leave a partially written text file that a later ingest would trust.
- **FR-012**: Capture MUST produce no cache file at all when it captured no
  text, and MUST report enough detail to distinguish reader failure from an
  empty document.
- **FR-013**: OCR MUST preserve page order and MUST NOT accept partial results,
  and every request MUST stay within the transport's page-count and byte limits.
  The pack requests one page at a time so the capture loop's empty-page counter
  measures real recognition output rather than a placeholder (research D10);
  multi-page batching is available in the transport and is deliberately unused.
- **FR-014**: OCR MUST fall back to local recognition when the configured
  endpoint is unavailable, and MUST disable further attempts to that endpoint
  for the remainder of the capture.
- **FR-015**: Ingestion MUST hand the complete captured text to core and MUST
  NOT pre-chunk, trim, or normalise it.
- **FR-016**: Ingestion MUST be idempotent on the cache path, returning the
  existing document when one exists, and MUST offer an explicit override.
- **FR-017**: Ingestion MUST write the document key, OCR identity, page count,
  review state, and, when supplied, an edition key into the document metadata.
- **FR-018**: Every tool MUST return `operator_action_required` when the
  blocking cause is a human action, naming the action.
- **FR-019**: The pack MUST declare, in its static manifest, exactly the
  permissions its code uses, including permission to run subprocesses, and MUST
  document that subprocesses run unsandboxed Windows software.
- **FR-020**: The pack MUST NOT import core internals at runtime; it may import
  only the standalone SDK.
- **FR-021**: A setup command MUST report the state of every external dependency
  with remediation steps and MUST NOT install or modify anything.
- **FR-022**: A capture-probe mode MUST report consecutive frame digests for
  page-turn attempts, so crop constants and the key mechanism can be re-measured
  after a reader upgrade.
- **FR-023**: Vendored OCR client code MUST name its upstream revision, and a
  test MUST assert its protocol version equals upstream's when upstream is
  installed.
- **FR-024**: The page-loop state machine MUST be exercisable in tests with
  injected capture, key-send, restart, and clock, with no display, reader, or
  OCR service present.
- **FR-025**: Any claim about reader behaviour that has not been measured MUST
  NOT appear in code comments, documentation, or operator-facing text.

### Key Entities

- **Loan artifact**: the operator's `.acsm` file; input only, never modified.
- **Fulfilled container**: the encrypted document Adobe Digital Editions
  downloaded into the prefix; never parsed, only opened by the reader.
- **Document key**: stable identity for a fulfilled work — rights metadata when
  available, otherwise a content hash; carries its provenance.
- **Capture record (sidecar)**: everything known about a capture — key and its
  provenance, title, container path, page count, how the run ended, restart
  count, OCR identity, quality summary, suspect pages, review state,
  and the tool versions in play.
- **Cached text**: the captured text that was (or will be) handed to core; the
  canonical artefact for citation re-verification.
- **Corpus document**: the ingested work in core, of the pack's document type,
  carrying the capture record's key facts as metadata.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator with a working prefix can go from a `.acsm` file to a
  citable corpus document using the documented runbook, with no manual file
  copying and no step that requires reading the source.
- **SC-002**: Re-running ingestion for an already-ingested document performs no
  capture and creates no duplicate document (observable: existing identifier
  returned, document count unchanged).
- **SC-003**: Every capture result states how the run ended and how many pages
  were captured; no result claims a complete document when the run ended on a
  staleness or emptiness threshold.
- **SC-004**: The loop's six transitions (progress, lost turn, stale run, empty
  run, stall-restart, page cap) are each covered by a test that runs with no
  external tooling installed.
- **SC-005**: A quotation drawn from an ingested document verifies as exact
  against the corpus, proving the captured text is what core stored.
- **SC-006**: Running the setup command in an environment with all external
  dependencies missing produces a remediation line for each one, installs
  nothing, and exits non-zero.
- **SC-007**: No code path writes a page image to disk (verifiable by test: the
  only file writes observed during a fake-driven capture are the text and
  sidecar paths).
- **SC-008**: An interrupted capture leaves either no cache file or a complete
  one; a truncated text file is never observable.

## Assumptions

- **Phase 0 assumptions are unevaluated.** The reader runs under Wine on a
  virtual display; synthesised keys turn its pages; window capture at the
  measured crop yields OCR-acceptable legibility; the fulfilled container
  carries usable rights metadata; activation survives a headless relaunch.
  These are the design document's A1–A5 and MUST be measured before any of them
  is asserted as fact (FR-025).
- Windows-level tooling (Wine, the reader, a window manager, the input and
  capture utilities) is installed and owned by the operator, not by this pack.
- The OCR service is the existing CUDA service used by the sibling reader pack;
  the pack may also use local recognition.
- Core `>=0.6,<0.7` with the standalone SDK's ingestion client is available and
  is the only sanctioned way to add text to the corpus.
- Loans are personal; the operator is authorised on the device, and captured
  text is for personal annotation, never redistribution.
- Scope is page-based documents; reflowable formats and OCR-accuracy guarantees
  are out of scope.
