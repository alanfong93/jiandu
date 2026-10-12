# ADR 0001: Explicit local synthesis review journal

Date: 2026-10-11
Status: Accepted for issue #1 implementation

## Context

A source citation identifies a reference, not how much material was reviewed,
what it contributed across topics, or whether the captured source changed later.
Jiandu needs inspectable progress without replacing source bodies or making
semantic-verification claims. Source reuse across related topics is desirable.

## Decision

Use a stdlib-only explicit CLI, stable captured-file UUIDs, append-only Markdown
records with versioned JSON payloads and sequence ordering, and a rebuildable
coverage report. Store records locally under configurable `tracking/`, outside
`raw/`/`wiki/`, ignored by Git by default and excluded from MemPalace pointers.
The source/topic relationship is many-to-many; review scope is declared, not a
fabricated completion percentage. Import existing citations as unknown historical
review, never invent past snapshots. Structural citation/fingerprint checks do
not establish truth or reading. The exact contract is in
[synthesis_tracking.md](../synthesis_tracking.md).

## Alternatives

- Citations alone: retain provenance but cannot answer review/revision questions.
- A full database/dashboard/watcher: unnecessary new maintenance and implicit
  actions for the explicit workflow. Deferred unless journal/report use proves
  insufficient.
- Shared mutable progress table or timestamp ordering: fragile under interruption,
  replay and clock changes; use immutable records and explicit append sequences.
- Global consumed flag: rejected because a source can legitimately inform more
  than one related topic and be reviewed again.

## Consequences

Records stay human-readable and have no additional dependency/network call.
They need backup despite Git exclusion. Single-writer locks are local only;
conflicts/corruption stop writes and require explicit recovery. Source hashes do
not retain old text, and whole-file revision differences do not identify which
claim changed. No ingestion migration, diary indexing policy change or legacy
personal journal import is included.
