# Synthesis tracking contract (version 1)

This is the portable contract for issue #1. It describes declared review history
and structural citation checks, not a semantic verifier or proof of reading.

## Files and boundaries

- The vault has `raw/` and `wiki/`. Text inventory supports `.md`, `.vtt`, `.srt`,
  like the existing indexer. Other formats are counted as attachment-only.
- `tracking/` is the default local record directory. `--tracking-dir` may select
  another vault-relative directory outside `raw/` and `wiki/`. Traversal,
  absolute/drive paths and symlink/junction components are rejected. Tracking
  cannot be the vault root, overlap `raw/`/`wiki/`, or occupy reserved kit folders
  (`scripts`, `skills`, `docs`, `tests`, `.git`, `.obsidian`, `.agents`, `.claude`).
  Portable paths reject Windows reserved device names, alternate data streams,
  control characters, trailing spaces/dots and case-colliding identities.
- Source bodies are never edited. A source UUID identifies a captured file, not
  an entire work. Identical copies/editions/translations remain independent.
- `tracking/sources/<source_id>.md` records immutable source registrations;
  `tracking/events/<event_id>.md` records starts, completions, failures and explicit
  path bindings. Each file has a readable summary and one authoritative JSON block.
- `tracking/Coverage.md` is a rebuildable, explicit report, not authoritative state.
  Records/report are local and excluded from Git by default. The existing indexer
  continues to read only `raw/` and `wiki/`; it does not index tracking metadata.
- Hidden paths are outside inventory, consistent with the indexer. Raw diaries,
  `privacy: private` sources and designated personal/medical records are visible
  as `private-review-needed`, not eligible for this general compilation workflow.
  This is not permission to send source bodies to external tools. Resolve private
  destination needs separately before compiling/indexing private material.
- `type: index`, `manifest` and `backlog` are inventoried as manifests, not material
  awaiting general synthesis. Empty bodies are flagged, not silently excluded.

## Common record fields

Every published record includes `schema: 1`, a positive integer `sequence`,
`record_id`, UTC `recorded_at`, actual `runtime`, and a `kind`. Sequences are
contiguous and globally ordered across both record directories. Timestamps are
descriptive, not ordering authority. Malformed records, unknown schemas/kinds,
sequence gaps/duplicates, conflicting filenames and invalid transitions fail closed.

The kit does not ingest the personal pilot's unnumbered journal records. Only
existing wiki citations are imported as historical references. Unsupported record
formats require an explicit future migration, not guessed timestamp ordering.

### Source registration

Fields: `source_id` (`S-` + UUID hex), initial `path` (full vault-relative raw path),
`fingerprint`, `legacy_references` (wiki path and original citation target), and
`historical_review: unknown`. Registration observes present references; it does
not invent a past review date, reviewed scope or past source revision.

Fingerprint fields: `byte_sha256`, `body_sha256`, `normalization`.
`utf8-bom-lf-body-v1` removes a UTF-8 BOM, normalizes CRLF/CR to LF, and removes
leading Markdown YAML frontmatter from the body hash. The byte hash preserves
all differences. VTT/SRT bodies are normalized text including their timestamps.
Hashes identify versions; no historical source text is archived by this feature.
Body equality with byte drift means a metadata/normalization-only difference,
not necessarily a harmless change: frontmatter may contain substantive content.

Frontmatter parsing supports top-level quoted/unquoted scalars and inline/block
string lists for classification/aliases. It is not a full YAML parser. Malformed
leading frontmatter and invalid UTF-8 are scan errors. An unrecognized/unsupported
`privacy` value requires private review rather than being assumed public. `public`
is the only explicit public privacy value. Private types are `personal-record`,
`medical-record`, `statement`, `receipt` (case/underscore normalization allowed).
Duplicate recognized keys and block lists appended to nonempty scalar/inline-list
values are malformed; they cannot discard private classification. Source-supplied
`generated` metadata does not exclude captures from inventory; reports are
excluded by their location outside `raw/`/`wiki/`.

Registering the same current path is a no-op. Registration does not bind a
matching copy to a missing source identity. A whole-vault copy carrying its
tracking directory carries the existing history too; this is not an automatic
edition/work merge or independent capture registration.
A registration retry returns history, not current privacy permission; review
starts/completions still assess the current capture. Case collisions are rejected
rather than projecting one identity's review onto two differently cased files.

### Review start

Fields: `run_id` (`R-` + UUID hex), `source_id`, current `path`, `fingerprint`,
nonempty `scope`, `extent` (`partial` or `all-captured`) and nonempty `purpose`.
Only one pending review per captured source is allowed. Retrying an identical
pending start with the same source fingerprint returns that start; a changed
fingerprint or different purpose/scope requires explicit recovery. Other sources/topics can
be reviewed later; no source ever becomes globally consumed.

### Completion

Fields: `run_id`, `outcome`, `reviewed_scope`, `reviewed_extent`, `contributions`,
`evidence`, and a nonempty assessment `note`.

- Outcomes: `incorporated`, `rechecked-existing`, `no-addition`.
- Each contribution has `target` (full `wiki/...` path, optional heading) and a
  nonempty `description`. Multiple topics are explicitly allowed.
- Incorporated/rechecked outcomes require contributions. No-addition has none.
- Evidence entries are optional full vault-relative Markdown links with an
  optional heading/block locator. They refer to existing evidence, not replacements
  for that evidence or its exact passages.
- Source bytes/body must match the start fingerprint at the completion check.
- Every contribution destination/heading must exist and have an unambiguous
  source citation somewhere in the note. Evidence note/locator must resolve.
  These are structural checks; agents must still compare prose with source passages.
- Actual scope may be narrower than planned. `all-captured` means this captured
  file, never the full book behind a highlight export or material omitted by a tool.
- Identical completion retry is a no-op. A different result cannot overwrite a
  closed review. A retry returns historical evidence even if source/destination
  changed later; it does not attest to current validity. Fresh rechecks get a new
  start; prior scopes/topics remain history.

### Failure and binding

Failure: `run_id`, `outcome: failed`, nonempty `note`. An identical failure retry
returns the existing result. An unclosed start remains
pending/interrupted; no success or failure is fabricated from process absence.

Binding: `source_id`, `old_path`, new `path`, `byte_sha256`. Explicit `bind` requires a complete
scan, missing old path, eligible/unassigned new path and an explicitly supplied
byte hash already recorded for that source. It appends history; it does not move
files. Retrying the most recent identical binding returns its historical result;
it does not reassert current bytes. Combined rename-and-edit reconciliation is not automated. Unobserved
replacement at the same path cannot reliably be distinguished from editing.
An explicit case-only rename is allowed when enumeration identifies one capture
at the requested new spelling and its byte hash matches; it keeps the same ID.

## CLI

Run from the kit root with Python's standard library. Global arguments precede
the subcommand; `--vault` defaults to the working directory.

```bash
python scripts/synthesis_tracker.py --vault . scan
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" scan --write
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" register "raw/books/highlights.md"
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" begin S-ID --scope "Locations 100-200" --extent partial --purpose "Assess new highlights"
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" complete R-ID --outcome incorporated --contribution "wiki/topic#Heading|Specific contribution" --note "Checked source against prose; remaining scope unreviewed"
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" complete R-ID --outcome no-addition --note "Nothing to add from this scope"
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" fail R-ID --note "Observed failure"
python scripts/synthesis_tracker.py --vault . --runtime "<runtime/model>" bind S-ID "raw/books/renamed.md" --expected-sha256 HASH
```

IDs above are placeholders; use IDs returned by the CLI. Repeat `--contribution`
for related topics; add `--evidence` for existing evidence. Add `--reviewed-scope`
and `--reviewed-extent partial` when an actual read was narrower than planned.
`scan --details` includes link issues in CLI output. Writes require explicit
runtime attribution. Exit codes: 0 success, 1 incomplete read-only/report scan,
2 rejected operation or invalid/corrupt state.

## Reporting and recovery

Citation resolution supports Obsidian wikilinks/embeds and inline Markdown links
(`path` or `<path>`, without an optional title). Root-qualified `raw/`/`wiki/`
targets resolve from the vault root; relative targets resolve from the referring
wiki file. Unqualified wikilinks may use a unique basename/frontmatter alias.
Extension omission includes supported text suffixes; case collisions are ambiguous.
URL-escaped paths/fragments are decoded before containment checks. Code fences,
inline code, HTML comments and Obsidian comments do not contribute citations.
Reference-style Markdown and unsupported inline syntax are flagged, not credited.
Headings must resolve uniquely (literal heading or simple lowercase-hyphen slug);
explicit `#^block-id` locators must have exactly one definition. Duplicate headings
or blocks remain unresolved. Internal relative links may use `..` only when their
normalized destination stays inside the vault; CLI/storage paths never allow it.
Supported block definitions append `^block-id` at line end (or on its own line);
an inline mention followed by more text is not a definition. Inline code spans
use matching backtick-run lengths; a shorter fence does not close a longer one.
Opaque code/comments are masked without deleting boundaries or newlines. A link
target cannot be assembled across them, and trailing code/comment text cannot
manufacture a line-end block definition.

The report separates eligibility, current wiki references, recorded review,
revision drift, declared scope, contributions and pending/failed history. Earlier
different reviewed revisions stay surfaced even if a newer partial review matches;
the whole-file hash does not establish which particular claim changed. There are
no fabricated coverage percentages or inference that an unrecorded source was
never used historically.

Both expected source roots must exist. Read/enumeration errors produce an
incomplete scan. Reporting may expose that uncertainty; registration/completion/
binding require a complete scan. Missing identities are retained, never pruned.

One writer machine and one compilation at a time. An exclusive local `.lock`
coordinates journal CLI writes only, not distributed Drive replicas or all wiki
edits. Do not claim multi-file transactions or rollback. A crash can leave a
pending start, a lock, or a malformed partial record. These stop writes rather
than guessing a completion. Inspect owner/process and preserve the files before
explicit recovery; the CLI never auto-clears locks or conflict copies. Report
replacement is atomic; a leftover report temp file blocks regeneration.

After compilation, refresh coverage and run the existing `index-vault` procedure.
Tracking-only changes do not introduce new MemPalace pointers; the indexer's
existing source/privacy scope is preserved. Custom tracking directories need
their own Git ignore rule before storing sensitive metadata. The unchanged
indexer can still index diary excerpts under `raw/diaries/`: compilation exclusion
and Git ignore are not indexing privacy boundaries.

### Recovery and race limitations

Stop other writers and back up the entire tracking directory. Read `.lock`
owner/PID and confirm its process has ended before explicitly removing a stale
lock. Preserve malformed/conflict copies outside active record directories;
restore the affected record from a known valid backup rather than inventing a
completion or renumbering events. Interior sequence gaps/duplicates stop loading;
deletion of the last records is not detectable without a backup. Append-only is
an application convention, not tamper-proof storage. Back up tracking with the
vault despite its Git exclusion. A valid pending start can be resumed or closed
using `fail` with the observed reason.
If the source changed while a review was pending, close the stale attempt with
`fail`, then begin a new review against the changed bytes and re-read. A stale
start cannot become a fresh completion simply by rereading the file.

Before publishing a completion, recheck source and contribution/evidence bytes
after structural validation and reject observable changes. Editors/sync can still
change files after that check; local locks/path checks are not external write
isolation or protection against an adversarial filesystem race.

## Requirement-to-test map

| Requirement | Contract evidence |
|---|---|
| Categories and legacy uncertainty | Raw/books/articles/podcasts/videos/courses/notes fixtures and ambiguous/missing citations |
| Stable IDs and many-topic use | Copy/rename/re-review fixtures plus generated uniqueness/history invariants |
| Honest scope and revisions | Highlight/partial/narrowed-scope, body/metadata drift and older-history fixtures |
| Lifecycle/retry/order | Begin/complete/fail/bind replay, rollback clocks, invalid transitions and corruption/lock fixtures |
| Portability/privacy | Custom directory, reserved names, traversal, link/junction, privacy/frontmatter and report-escaping fixtures |
| Skills/index integration | Disposable CLI pilot, indexer regression suite and skills/docs inspection |

Generated tests use reproducible seeds and independently asserted invariants.
Failure injection exercises unavailable files and edits at completion boundaries.
