# Marginalia ACSM Pack Constitution

The non-negotiable principles for `marginalia-plugin-acsm`. Every spec, plan,
and task in `specs/` is checked against these before implementation.

## Core Principles

### I. Canonical text is the product

The value this pack delivers is text that core can chunk, offset-validate, and
quote. A citation that resolves to `exact` against text this pack produced is
the only proof that counts.

- The **entire** captured text MUST reach core through
  `ingestion.ingest_document`; the pack MUST NOT pre-chunk, trim, normalise
  case, or "clean" text before handing it over.
- Every cached artefact MUST be the exact text that was ingested, so a citation
  can be re-verified later without the pack running.
- OCR is lossy; the pack MUST report what it does not know (page count
  confidence, per-page OCR confidence, `ended_by`) rather than presenting a
  plausible-looking complete book. Missing pages MUST NOT be silently claimed
  as present.

### II. No circumvention, no credentials

- The pack MUST NOT decrypt, strip, or bypass any access control. The encrypted
  container is opened only by Adobe Digital Editions, under Wine, with the
  operator's own activation.
- The pack MUST NOT read, store, prompt for, or transmit Adobe credentials.
  Activation is a one-time interactive act performed by the operator.
- Rendered page images are licensed content: they MUST stay in memory. No frame,
  crop, or page image may be written to disk, logged, or sent anywhere but the
  configured OCR endpoint.
- Every operator-facing surface MUST carry the licensed-content warning.

### III. External tooling is verified, never installed

Wine, Adobe Digital Editions, Xvfb, the window manager, `xdotool`,
ImageMagick, and the OCR service are operator-owned infrastructure.

- `research-engine-acsm-setup` MUST report state and print the exact remediation
  command. It MUST NOT install packages, run `winetricks`, or mutate the Wine
  prefix except through ADE itself.
- A missing dependency MUST produce an actionable error naming the dependency,
  never a bare traceback and never a silent degraded path.
- Windows software (ADE) runs with the operator's privileges; the manifest MUST
  say so where the operator approves permissions.

### IV. The page loop is deterministic and injectable

Driving a GUI is the least reliable part of this system; it MUST therefore be
the most testable.

- `driver.py` MUST take its capture function, key sender, restart hook, and
  clock as parameters. It MUST NOT open a display, spawn a process, or import
  `capture.py`.
- Unit tests MUST exercise every loop transition (progress, lost keypress,
  stale run, empty run, stall/restart, max pages, end of document) with no X
  server, no Wine, and no OCR service present.
- Time-dependent behaviour MUST be injected rather than slept through in tests.

### V. Plugin boundary discipline

- Runtime code MUST import only `research_engine_sdk` and the standard library's
  third-party dependencies; it MUST NOT import `research_engine.*`.
- `acsm/plugin.yaml` is static, schema-v2, and names exactly the tools,
  document type, and permissions the code uses. Discovery MUST NOT import the
  package.
- The pack MUST be inert until an operator approves and enables the exact
  installed artefact.

### VI. Honesty over completeness

- A tool that cannot proceed because a human must act (activation missing, loan
  expired, dependency absent) MUST say so as `operator_action_required`, not
  bury it in a generic error.
- A scrape that cannot be verified against the real reader MUST be reported as
  unverified. Shipping "probably fine" text into a corpus whose purpose is
  verification is a defect, not a shortcut.
- Vendored code MUST name its upstream revision, and the vendored protocol
  version MUST be asserted equal to upstream by a test.

## Development Workflow and Quality Gates

1. **Spec before code.** Every behaviour traces to a user story and an FR in
   `spec.md`. Tasks in `tasks.md` name their story.
2. **Tests with the code.** Each task's acceptance criterion is exercised by a
   test in the same change; the unit suite MUST run with no external tooling.
3. **Gates.** `ruff check acsm tests` clean, `pytest tests/unit -q` green,
   wheel builds and `research-engine plugin audit acsm` lists exactly the
   approved tools. A red gate blocks the task, not the release notes.
4. **Phase 0 before plumbing.** The Wine/ADE assumptions (A1–A5) are recorded
   verdicts, not beliefs. Until they are recorded, no claim about ADE behaviour
   may enter code comments, README, or the operator runbook.
5. **No fabricated verification.** CI cannot run ADE. Anything the CI cannot
   prove MUST be listed as an operator-run gate in the release notes, with the
   command to run it.
