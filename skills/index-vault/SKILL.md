---
name: index-vault
description: >
  Re-index this vault into MemPalace after any vault write (new raw file,
  wiki compile, edit, rename, or delete). Not only after compile-wiki.
  MemPalace does not watch files. mempalace sync prunes; it is not ingest.
---

# index-vault

Search is MemPalace. Files are the vault. After **any** write to this vault, update the pointers.

This is **not** stock MemPalace auto-indexing. There is no file watch. You run this skill.

`mempalace sync` **prunes** drawers whose source files are gone. Do not use it as ingest.

## When

Run after creating, editing, renaming, or deleting a file under `raw/` or `wiki/`. Including after `compile-wiki`.

## How

From the vault root:

```bash
python scripts/index_vault.py --vault . --palace "<PALACE_PATH>" --sync
```

- `--vault` — this repository (the knowledge vault).
- `--palace` — the MemPalace directory on the machine running the agent. Pass the path **exactly** as the MemPalace server uses it (on Windows, backslashes are load-bearing for some backends).
- `--sync` — update changed files only.

If the MemPalace MCP is connected in this session, call its reconnect after a CLI index so the in-memory search index matches disk.

If `scripts/index_vault.py` cannot import MemPalace, stop and tell the user to install MemPalace. Do not pretend the files are searchable.

## What gets indexed

Lightweight pointers: title, path, tags, and the first 300 characters of Markdown or extracted caption text. The agent then reads the source file from the vault. Do not dump whole notes into the palace.

Default folders: `raw/` and `wiki/`. Markdown, `.vtt`, and `.srt` are indexed. Captions become a short text excerpt with cue numbers and timestamps omitted; the original files are not modified. Diaries may be present but gitignored; do not scrape ignored diary files back into git.

`tracking/` review records and generated coverage are not indexed. A tracking-only
write does not create new pointers; do not broaden the source folders to include
private review metadata. Private classification in `compile-wiki` does not change
this indexer: it can still store diary excerpts from `raw/diaries/`. Review the
source/destination privacy before indexing private material.

The indexer scans all expected source directories before writing pointers. If the vault is incomplete or any source cannot be read, fix the reported problem and retry; do not force pruning after an incomplete scan. Pointers are scoped to the normalized vault path, so keep that path stable across runs. Moving the vault creates a new pointer identity.

## Do not

- Say MemPalace auto-indexes
- Run `mempalace sync` when you meant ingest
- Skip this after a raw save because "we will compile later"
- Index a different person's vault into this palace
