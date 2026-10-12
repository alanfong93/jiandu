# System flow

After any write under `raw/` or `wiki/`, the user runs `index-vault`. The script reads the full source set before touching MemPalace. A scan failure stops the run without changing pointers. A successful scan refreshes changed pointers and prunes missing pointers only within the same vault identity.

```mermaid
flowchart TD
  Write[Write or change a source in raw/ or wiki/]
  Run[Run index-vault with the exact MemPalace path]
  Validate[Validate vault root and expected source directories]
  Scan[Read Markdown, VTT, and SRT sources; compute hashes and excerpts]
  Complete{Was the scan complete?}
  Error[Report errors; leave MemPalace unchanged]
  Load[Load indexer pointers for this vault identity]
  Compare[Compare content hash and index version]
  Upsert[Upsert new or changed pointers]
  Prune{Pruning enabled and under safety limit?}
  Delete[Delete missing pointers for this vault only]
  Done[Report indexing totals]

  Write --> Run --> Validate --> Scan --> Complete
  Complete -->|No| Error
  Complete -->|Yes| Load --> Compare --> Upsert --> Prune
  Prune -->|Yes| Delete --> Done
  Prune -->|No| Done
```

## Compilation with declared review history

```mermaid
flowchart TD
  Capture[Capture source verbatim in raw/]
  Eligible{Eligible text and appropriate destination?}
  Hold[Inventory as attachment/manifest/private-review-needed<br>Resolve separately; no general review completion]
  Register[Register stable captured-source ID]
  Start[Begin scope/extent/purpose; fingerprint source]
  Read[Read material; record actual bounded scope]
  Compile[Update related wiki topics with source citations<br>or assess no useful addition]
  Check[Compare prose/source; check destinations and final bytes]
  Stable{Observable checks pass?}
  Pending[Preserve pending attempt or record observed failure]
  End[Append outcome and many-topic contributions]
  Report[Explicitly regenerate local coverage]
  Index[Run existing index-vault after final source/wiki writes]

  Capture --> Eligible
  Eligible -->|No| Hold
  Eligible -->|Yes| Register --> Start --> Read --> Compile --> Check --> Stable
  Stable -->|No| Pending --> Report
  Stable -->|Yes| End --> Report --> Index
```

No file watcher starts these steps. Reports distinguish referenced/unknown
history from partial/all-captured reviews, no-addition, rechecked contributions
and pending/failed attempts. Tracking-only files are local and outside pointer
indexing. Compilation privacy classification does not change whether the
existing indexer reads diary excerpts under `raw/`.
