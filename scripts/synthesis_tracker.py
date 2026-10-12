#!/usr/bin/env python3
"""Explicit local inventory and declared synthesis review history for Jiandu.

Never edits raw/wiki bodies or makes network requests. See the versioned contract
in docs/synthesis_tracking.md for privacy, retry and filesystem limitations.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys
from urllib.parse import unquote
import uuid

TEXT_SUFFIXES = {".md", ".vtt", ".srt"}
NORMALIZATION = "utf8-bom-lf-body-v1"
PRIVATE_TYPES = {"personal-record", "medical-record", "statement", "receipt"}
RESERVED = {"raw", "wiki", "docs", "scripts", "skills", "tests", ".git", ".obsidian", ".agents", ".claude"}
RECORD_JSON = re.compile(r"```json\n(.*?)\n```", re.S)
WIKILINK = re.compile(r"!?\[\[([^\]\n]+)\]\]")
MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^\s()]+)\)")


class TrackingError(ValueError):
    """An operation cannot safely establish the declared tracking state."""


def now():
    return datetime.now(timezone.utc).isoformat()


def is_link_or_junction(path):
    if Path(path).is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path):
        return True
    try:
        tag = getattr(Path(path).lstat(), "st_reparse_tag", None)
    except FileNotFoundError:
        return False
    # Python 3.10/3.11 have reparse tags but no os.path.isjunction.
    return tag is not None and tag in {
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1),
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -2),
    }


def portable_relative(value):
    text = str(value).replace("\\", "/")
    parts = text.split("/")
    if not text or text.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        raise TrackingError("Use a nonempty vault-relative path without traversal")
    for part in parts:
        device = part.split(".", 1)[0].upper()
        if (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((" ", "."))
                or device in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}):
            raise TrackingError(f"Non-portable path component: {part!r}")
    return "/".join(parts)


def secure_path(root, relative):
    relative = portable_relative(relative)
    root = Path(root).resolve()
    path = root
    for component in relative.split("/"):
        path = path / component
        if is_link_or_junction(path):
            raise TrackingError(f"Refusing linked/junction path: {relative}")
    if not path.resolve().is_relative_to(root):
        raise TrackingError("Path escapes the configured root")
    return path


def read_source(path):
    return Path(path).read_bytes()


def scalar(value):
    value = value.strip()
    if value.startswith("#"):
        return ""
    if value.startswith('"'):
        try:
            parsed, end = json.JSONDecoder().raw_decode(value)
        except ValueError as error:
            raise TrackingError("Malformed double-quoted metadata") from error
        trailing = value[end:].strip()
        if not isinstance(parsed, str) or (trailing and not trailing.startswith("#")):
            raise TrackingError("Unsupported quoted metadata")
        return parsed
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise TrackingError("Malformed single-quoted metadata")
        return match[1].replace("''", "'")
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_value(value):
    if not value.startswith("["):
        return scalar(value)
    if not re.fullmatch(r"\[.*\]\s*(?:#.*)?", value):
        raise TrackingError("Malformed metadata list")
    inside = value[:value.rfind("]")][1:]
    parts, start, quote = [], 0, None
    index = 0
    while index < len(inside):
        char = inside[index]
        if quote is not None and char == quote:
            if quote == "'" and index + 1 < len(inside) and inside[index + 1] == "'":
                index += 2
                continue
            if index == 0 or inside[index - 1] != "\\":
                quote = None
        elif quote is None and char in {'"', "'"} and not inside[start:index].strip():
            quote = char
        if char == "," and quote is None:
            parts.append(inside[start:index])
            start = index + 1
        index += 1
    if quote:
        raise TrackingError("Unclosed metadata list quote")
    parts.append(inside[start:])
    return [scalar(p) for p in parts if p.strip()]


def frontmatter(text):
    if not re.match(r"\A---\s*\n", text):
        return {}, text
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    if not match:
        raise TrackingError("Unclosed leading frontmatter")
    fields, key, block_list = {}, None, False
    for line in match[1].splitlines():
        item = re.match(r"^([\w-]+):\s*(.*?)\s*$", line)
        if item:
            key, value = item.groups()
            block_list = False
            # Only this subset influences classification and link aliases.
            if key in {"privacy", "type", "aliases", "alias", "generated"}:
                if key in fields:
                    raise TrackingError("Duplicate classification/alias metadata key")
                fields[key] = parse_value(value)
                block_list = not value or value.startswith("#")
        elif key in fields and re.match(r"^\s*-\s+", line):
            if not block_list:
                raise TrackingError("Block list cannot replace a scalar/inline-list value")
            if fields[key] == "":
                fields[key] = []
            fields[key].append(scalar(re.sub(r"^\s*-\s+", "", line)))
    return fields, text[match.end():]


def fingerprint(path):
    data = read_source(path)
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    fields, body = frontmatter(text) if Path(path).suffix.lower() == ".md" else ({}, text)
    return ({"byte_sha256": hashlib.sha256(data).hexdigest(),
             "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
             "normalization": NORMALIZATION}, text, fields, body)


def classify(path, fields):
    raw_kind = fields.get("type", "")
    kinds = raw_kind if isinstance(raw_kind, list) else [raw_kind]
    kinds = {str(k).casefold().replace("_", "-") for k in kinds}
    privacy = fields.get("privacy", "public")
    values = privacy if isinstance(privacy, list) else [privacy]
    if (path.casefold().startswith("raw/diaries/") or kinds & PRIVATE_TYPES
            or not values or any(str(x).casefold() != "public" for x in values)):
        return "private-review-needed"
    return "manifest" if kinds & {"index", "manifest", "backlog"} else "eligible"


def linkable(text):
    # Preserve opaque boundaries and original newlines. Deleting code/comments
    # can manufacture a link target or turn a mid-line block mention into a definition.
    def mask(value):
        return "".join(char if char in "\r\n" else "\x00" for char in value)
    text = re.sub(r"<!--.*?-->|%%.*?%%", lambda match: mask(match[0]), text, flags=re.S)
    output, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = (marker[1][0], len(marker[1]))
            elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not line[marker.end():].strip():
                fence = None
            output.append(mask(line))
            continue
        if fence is None:
            output.append(line)
        else:
            output.append(mask(line))
    return re.sub(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", lambda match: mask(match[0]), "".join(output), flags=re.S)


def extract_links(text):
    clean = linkable(text).replace(r"\|", "|")
    wiki_matches = list(WIKILINK.finditer(clean))
    markdown_matches = list(MARKDOWN_LINK.finditer(clean))
    result = [(m[1].split("|", 1)[0].strip(), "wiki") for m in wiki_matches if "\x00" not in m[1]]
    result += [(m[1].strip("<>"), "markdown") for m in markdown_matches if "\x00" not in m[1]]
    unsupported = re.findall(r"(?<!!)\[[^\]\n]+\]\[[^\]\n]*\]", clean)
    unsupported += ["citation overlaps opaque code/comment" for m in wiki_matches + markdown_matches if "\x00" in m[1]]
    # Inline links with titles/complex syntax are not silently treated as citations.
    remainder = MARKDOWN_LINK.sub("", clean)
    unsupported += re.findall(r"!?\[[^\]\n]*\]\([^\n]*", remainder)
    return list(dict.fromkeys(result)), unsupported


def locator_exists(text, locator):
    if "\x00" in locator:
        return False
    clean = linkable(text)
    if not locator:
        return True
    if locator.startswith("^"):
        return len(re.findall(r"(?:^|[ \t])" + re.escape(locator) + r"[ \t]*$", clean, re.M)) == 1
    headings = [re.sub(r"\s+#+\s*$", "", m[1]).strip()
                for m in re.finditer(r"^\s{0,3}#{1,6}\s+(.+)$", clean, re.M)]
    matches = [h for h in headings if h == locator or re.sub(r"\s+", "-", h.casefold()) == locator]
    return len(matches) == 1


def scan(vault):
    root = Path(vault).resolve()
    entries, wiki, errors, root_status = {}, {}, [], {}
    for top in ("raw", "wiki"):
        base = root / top
        root_status[top] = "complete"
        if not base.is_dir() or is_link_or_junction(base):
            errors.append(f"Missing or linked source root: {top}")
            root_status[top] = "incomplete"
            continue
        def onerror(error):
            errors.append(f"{top}: {error}")
            root_status[top] = "incomplete"
        for directory, dirs, files in os.walk(base, onerror=onerror, followlinks=False):
            kept = []
            for name in dirs:
                if name.startswith(".") or name == "__pycache__":
                    continue
                if is_link_or_junction(Path(directory) / name):
                    onerror(f"Linked directory: {Path(directory) / name}")
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                try:
                    secure_path(root, relative)
                    if path.suffix.lower() not in TEXT_SUFFIXES:
                        if top == "raw":
                            entries[relative] = {"path": relative, "eligibility": "attachment-only", "text": ""}
                        continue
                    hashes, text, fields, body = fingerprint(path)
                    (wiki if top == "wiki" else entries)[relative] = {
                        "path": relative, "eligibility": classify(relative, fields),
                        "aliases": fields.get("aliases", fields.get("alias", [])),
                        "text": text, "empty_body": not body.strip(), **hashes,
                    }
                except (OSError, UnicodeError, TrackingError) as error:
                    onerror(f"{relative}: {error}")
    documents = {**entries, **wiki}
    paths, names = defaultdict(set), defaultdict(set)
    for relative, entry in documents.items():
        paths[relative.casefold()].add(relative)
        paths[Path(relative).with_suffix("").as_posix().casefold()].add(relative)
        names[Path(relative).stem.casefold()].add(relative)
        aliases = entry.get("aliases", [])
        for alias in aliases if isinstance(aliases, list) else [aliases]:
            if alias and not str(alias).startswith(("{", ">", "|")):
                names[str(alias).casefold()].add(relative)
    references, ambiguous, unresolved = defaultdict(list), [], []
    for wiki_path, entry in wiki.items():
        targets, unsupported = extract_links(entry["text"])
        unresolved.extend({"wiki": wiki_path, "target": t, "reason": "unsupported syntax"} for t in unsupported)
        for target, syntax in targets:
            if re.match(r"^[a-zA-Z][\w+.-]*:", target) or target.startswith("//"):
                continue
            base, separator, fragment = target.partition("#")
            base, fragment = unquote(base).replace("\\", "/"), unquote(fragment)
            if "\x00" in base or "\x00" in fragment:
                unresolved.append({"wiki": wiki_path, "target": "invalid control character", "reason": "unsupported syntax"})
                continue
            try:
                if not base:
                    candidates = {wiki_path}
                elif base.casefold().startswith(("raw/", "wiki/")):
                    base = portable_relative(posixpath.normpath(base))
                    candidates = paths.get(base.casefold(), set())
                elif syntax == "wiki" and "/" not in base and not base.startswith("."):
                    candidates = names.get(base.casefold(), set()) or paths.get(base.casefold(), set())
                    if not candidates and Path(base).suffix:
                        candidates = {p for p in documents if Path(p).name.casefold() == base.casefold()}
                else:
                    relative = posixpath.normpath(posixpath.join(posixpath.dirname(wiki_path), base))
                    portable_relative(relative)
                    candidates = paths.get(relative.casefold(), set())
            except TrackingError:
                candidates = set()
            if len(candidates) > 1:
                ambiguous.append({"wiki": wiki_path, "target": target, "candidates": sorted(candidates)})
            elif candidates:
                destination = next(iter(candidates))
                if separator and not locator_exists(documents[destination]["text"], fragment):
                    unresolved.append({"wiki": wiki_path, "target": target, "reason": "missing/ambiguous locator"})
                elif destination in entries:
                    references[destination].append({"wiki": wiki_path, "target": target})
            else:
                unresolved.append({"wiki": wiki_path, "target": target, "reason": "unresolved destination"})
    return {"entries": entries, "wiki": wiki, "references": dict(references), "ambiguous": ambiguous,
            "unresolved": unresolved, "errors": errors, "roots": root_status, "scanned_at": now()}


def require_complete(inventory):
    if inventory["errors"]:
        raise TrackingError("Scan incomplete; no review/source record will be written: " + "; ".join(inventory["errors"]))


def entry_for(entries, relative):
    """Resolve portable case spelling, never pick between case-colliding files."""
    matches = [entry for path, entry in entries.items() if path.casefold() == relative.casefold()]
    if len(matches) > 1:
        raise TrackingError("Ambiguous case-colliding captured paths")
    return matches[0] if matches else None


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_record(record):
    common = {"schema", "sequence", "record_id", "recorded_at", "runtime", "kind"}
    if not common <= record.keys() or type(record["schema"]) is not int or record["schema"] != 1 or type(record["sequence"]) is not int or record["sequence"] < 1:
        raise TrackingError("Unsupported schema or malformed record sequence")
    if not all(nonempty(record[k]) for k in ("record_id", "recorded_at", "runtime", "kind")):
        raise TrackingError("Missing record attribution/identity")
    try:
        stamp = datetime.fromisoformat(record["recorded_at"])
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("Expected UTC timezone")
    except ValueError as error:
        raise TrackingError("Invalid record timestamp") from error
    kind = record["kind"]
    if kind in {"source", "binding", "start"}:
        if not re.fullmatch(r"S-[0-9a-f]{32}", record.get("source_id", "")):
            raise TrackingError("Invalid source ID")
        if not portable_relative(record.get("path", "")).startswith("raw/"):
            raise TrackingError("Source path must be under raw/")
    if kind in {"source", "start"}:
        fp = record.get("fingerprint", {})
        if fp.get("normalization") != NORMALIZATION or not all(re.fullmatch(r"[0-9a-f]{64}", fp.get(k, "")) for k in ("byte_sha256", "body_sha256")):
            raise TrackingError("Invalid source fingerprint")
    if kind in {"start", "completion", "failure"} and not re.fullmatch(r"R-[0-9a-f]{32}", record.get("run_id", "")):
        raise TrackingError("Invalid review ID")
    if kind == "source":
        if record["record_id"] != record["source_id"] or record.get("historical_review") != "unknown" or not isinstance(record.get("legacy_references"), list):
            raise TrackingError("Invalid legacy registration")
        for ref in record["legacy_references"]:
            if (not isinstance(ref, dict) or not nonempty(ref.get("target"))
                    or not portable_relative(ref.get("wiki", "")).startswith("wiki/")):
                raise TrackingError("Invalid legacy citation reference")
    elif kind == "start":
        if record["record_id"] != record["run_id"] or record.get("extent") not in {"partial", "all-captured"} or not all(nonempty(record.get(k)) for k in ("scope", "purpose")):
            raise TrackingError("Invalid review start")
    elif kind == "completion":
        if record.get("outcome") not in {"incorporated", "rechecked-existing", "no-addition"} or record.get("reviewed_extent") not in {"partial", "all-captured"} or not all(nonempty(record.get(k)) for k in ("reviewed_scope", "note")):
            raise TrackingError("Invalid completion outcome/scope")
        contributions = record.get("contributions")
        if not isinstance(contributions, list) or not isinstance(record.get("evidence"), list):
            raise TrackingError("Invalid contribution/evidence lists")
        if any(not nonempty(e) for e in record["evidence"]):
            raise TrackingError("Invalid evidence reference")
        if (record["outcome"] == "no-addition") != (not contributions):
            raise TrackingError("Outcome does not match contributions")
        if any(not isinstance(c, dict) or not all(nonempty(c.get(k)) for k in ("target", "description")) for c in contributions):
            raise TrackingError("Invalid contribution")
    elif kind == "failure":
        if record.get("outcome") != "failed" or not nonempty(record.get("note")):
            raise TrackingError("Invalid failure record")
    elif kind == "binding":
        if not portable_relative(record.get("old_path", "")).startswith("raw/") or not re.fullmatch(r"[0-9a-f]{64}", record.get("byte_sha256", "")):
            raise TrackingError("Invalid binding")
    else:
        raise TrackingError("Unknown record kind")
    if kind in {"completion", "failure"} and record["record_id"] != record["run_id"] + "-end":
        raise TrackingError("Terminal record does not match its review")
    if kind == "binding" and not re.fullmatch(r"B-[0-9a-f]{32}", record["record_id"]):
        raise TrackingError("Invalid binding ID")


def replay(records):
    sources, runs = {}, {}
    for record in records:
        kind = record["kind"]
        if kind == "source":
            if record["source_id"] in sources:
                raise TrackingError("Duplicate source identity")
            sources[record["source_id"]] = {**record, "prior_paths": []}
        elif kind == "binding":
            source = sources.get(record["source_id"])
            if source is None or source["path"] != record["old_path"]:
                raise TrackingError("Conflicting path-binding history")
            source["prior_paths"] = source["prior_paths"] + [source["path"]]
            source["path"] = record["path"]
        elif kind == "start":
            if record["source_id"] not in sources or record["run_id"] in runs or sources[record["source_id"]]["path"] != record["path"]:
                raise TrackingError("Unknown/conflicting review start")
            if any(r["start"]["source_id"] == record["source_id"] and r["end"] is None for r in runs.values()):
                raise TrackingError("Source already has a pending review")
            runs[record["run_id"]] = {"start": record, "end": None}
        else:
            run = runs.get(record["run_id"])
            if run is None or run["end"] is not None:
                raise TrackingError("Orphan/conflicting terminal event")
            run["end"] = record
        paths = [s["path"].casefold() for s in sources.values()]
        if len(paths) != len(set(paths)):
            raise TrackingError("Case-colliding source paths")
    return sources, runs


def cell(value):
    return html.escape(re.sub(r"\x00+", "[opaque]", str(value))).replace("|", r"\|").replace("\n", "<br>")


def wikilink(path, label=None):
    if any(c in path for c in "[]|#"):
        return "`" + cell(path).replace("`", "&#96;") + "`"
    return "[[" + cell(path) + (r"\|" + cell(label) if label else "") + "]]"


class Tracker:
    def __init__(self, vault, runtime="unattributed/read-only", tracking_dir="tracking"):
        self.vault = Path(vault).resolve()
        self.relative_store = portable_relative(tracking_dir)
        if self.relative_store.split("/", 1)[0].casefold() in RESERVED:
            raise TrackingError("Tracking cannot overlap source or reserved kit directories")
        self.store = secure_path(self.vault, self.relative_store)
        self.runtime = runtime
        if not nonempty(runtime) or any(ord(c) < 32 for c in runtime):
            raise TrackingError("Use a nonempty runtime/model without control characters")
        self._locked = False

    def path(self, relative):
        return secure_path(self.vault, self.relative_store + "/" + relative)

    @contextmanager
    def lock(self):
        if self.runtime == "unattributed/read-only":
            raise TrackingError("Writes require explicit runtime/model attribution")
        secure_path(self.vault, self.relative_store).mkdir(parents=True, exist_ok=True)
        lock_path = self.path(".lock")
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise TrackingError("Tracking lock exists; inspect its owner, never auto-clear it") from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "started": now(), "runtime": self.runtime}, handle)
            self.state()
            self._locked = True
            yield
        finally:
            self._locked = False
            lock_path.unlink()

    def load(self):
        secure_path(self.vault, self.relative_store)
        records = []
        for group in ("sources", "events"):
            folder = self.path(group)
            if not folder.exists():
                continue
            for path in folder.iterdir():
                self.path(group + "/" + path.name)
                if path.suffix != ".md" or not path.is_file():
                    raise TrackingError("Unexpected/partial record file: " + path.name)
                matches = RECORD_JSON.findall(path.read_text(encoding="utf-8"))
                if len(matches) != 1:
                    raise TrackingError("Malformed record: " + path.name)
                try:
                    record = json.loads(matches[0])
                    validate_record(record)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise TrackingError("Invalid record: " + path.name) from error
                if record["record_id"] != path.stem or (record["kind"] == "source") != (group == "sources"):
                    raise TrackingError("Conflict/misplaced record: " + path.name)
                records.append(record)
        records.sort(key=lambda r: r["sequence"])
        if [r["sequence"] for r in records] != list(range(1, len(records) + 1)):
            raise TrackingError("Missing/duplicate record sequence")
        return records

    def state(self):
        return replay(self.load())

    def record(self, kind, identifier, payload):
        if not self._locked:
            raise TrackingError("Record writes require the local lock")
        existing = self.load()
        record = {"schema": 1, "sequence": len(existing) + 1, "record_id": identifier,
                  "recorded_at": now(), "runtime": self.runtime, "kind": kind, **payload}
        validate_record(record)
        replay(existing + [record])
        lines = ["---", f"title: {json.dumps('Synthesis ' + kind + ': ' + identifier)}",
                 "tags: [synthesis-tracking, provenance]", f"runtime: {json.dumps(self.runtime)}",
                 "generated: synthesis-tracker", "---", "", "# Synthesis " + kind, ""]
        for key in ("source_id", "path", "run_id", "scope", "extent", "reviewed_scope", "reviewed_extent", "outcome", "note"):
            if key in record:
                lines.append(f"- **{key.replace('_', ' ').title()}:** {cell(record[key])}")
        for item in record.get("contributions", []):
            lines.append("- " + wikilink(item["target"].split("#", 1)[0]) + ": " + cell(item["description"]))
        if kind == "source":
            lines.append("- Historical review: **unknown**; registration is not a completion.")
        lines += ["", "## Machine record", "", "```json", json.dumps(record, ensure_ascii=False, indent=2), "```", ""]
        group = "sources" if kind == "source" else "events"
        path = self.path(group + "/" + identifier + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def register(self, relative):
        relative = portable_relative(relative)
        inventory = scan(self.vault)
        require_complete(inventory)
        entry = entry_for(inventory["entries"], relative)
        sources, _ = self.state()
        for source in sources.values():
            if source["path"].casefold() == relative.casefold():
                # Return historical registration, not current privacy/review permission.
                return source
        if entry is None or entry["eligibility"] != "eligible":
            raise TrackingError("Registration requires an eligible text capture under raw/")
        relative = entry["path"]
        identifier = "S-" + uuid.uuid4().hex
        return self.record("source", identifier, {
            "source_id": identifier, "path": relative, "fingerprint": {k: entry[k] for k in ("byte_sha256", "body_sha256", "normalization")},
            "legacy_references": inventory["references"].get(relative, []), "historical_review": "unknown",
        })

    def begin(self, source_id, scope, extent, purpose):
        require_complete(scan(self.vault))
        sources, runs = self.state()
        if source_id not in sources:
            raise TrackingError("Unknown source ID")
        source = sources[source_id]
        hashes, _, fields, _ = fingerprint(secure_path(self.vault, source["path"]))
        if classify(source["path"], fields) != "eligible":
            raise TrackingError("Source requires privacy/destination review")
        payload = {"source_id": source_id, "path": source["path"], "fingerprint": hashes,
                   "scope": scope, "extent": extent, "purpose": purpose}
        for run in runs.values():
            if run["start"]["source_id"] == source_id and run["end"] is None:
                if all(run["start"].get(k) == v for k, v in payload.items()):
                    return run["start"]
                raise TrackingError("Source has a different or changed pending review")
        rid = "R-" + uuid.uuid4().hex
        return self.record("start", rid, {"run_id": rid, **payload})

    def complete(self, run_id, outcome, contributions, note, evidence=(), reviewed_scope=None, reviewed_extent=None):
        sources, runs = self.state()
        if run_id not in runs:
            raise TrackingError("Unknown review ID")
        run = runs[run_id]
        payload = {"run_id": run_id, "outcome": outcome, "contributions": contributions, "note": note,
                   "evidence": list(evidence), "reviewed_scope": run["start"]["scope"] if reviewed_scope is None else reviewed_scope,
                   "reviewed_extent": run["start"]["extent"] if reviewed_extent is None else reviewed_extent}
        if run["end"] is not None:
            if run["end"]["kind"] == "completion" and all(run["end"].get(k) == v for k, v in payload.items()):
                return run["end"]
            raise TrackingError("Review already closed with a different result")
        inventory = scan(self.vault)
        require_complete(inventory)
        source = sources[run["start"]["source_id"]]
        current = entry_for(inventory["entries"], source["path"])
        fp = run["start"]["fingerprint"]
        if current is None or current["eligibility"] != "eligible" or any(current[k] != v for k, v in fp.items()):
            raise TrackingError("Source changed/unavailable during review")
        checked = {current["path"]: fp["byte_sha256"]}
        for item in contributions:
            target, _, locator = item["target"].partition("#")
            target = portable_relative(target)
            target = target if target.endswith(".md") else target + ".md"
            destination = entry_for(inventory["wiki"], target)
            if destination is None or not locator_exists(destination["text"], locator):
                raise TrackingError("Missing/ambiguous contribution destination or heading")
            target = destination["path"]
            if not any(r["wiki"] == target for r in inventory["references"].get(current["path"], [])):
                raise TrackingError("Destination lacks an unambiguous source citation")
            checked[target] = destination["byte_sha256"]
        for evidence_target in evidence:
            target, _, locator = evidence_target.partition("#")
            target = portable_relative(target)
            target = target if target.endswith(".md") else target + ".md"
            hashes, text, _, _ = fingerprint(secure_path(self.vault, target))
            if not locator_exists(text, locator):
                raise TrackingError("Evidence locator does not resolve uniquely")
            checked[target] = hashes["byte_sha256"]
        for target, expected in checked.items():
            if hashlib.sha256(read_source(secure_path(self.vault, target))).hexdigest() != expected:
                raise TrackingError("Source/destination/evidence changed before completion publication")
        return self.record("completion", run_id + "-end", payload)

    def fail(self, run_id, note):
        _, runs = self.state()
        if run_id not in runs:
            raise TrackingError("Unknown review ID")
        run = runs[run_id]
        if run["end"] is not None:
            if run["end"]["kind"] == "failure" and run["end"]["note"] == note:
                return run["end"]
            raise TrackingError("Review already closed")
        return self.record("failure", run_id + "-end", {"run_id": run_id, "outcome": "failed", "note": note})

    def bind(self, source_id, relative, expected_hash):
        relative = portable_relative(relative)
        sources, runs = self.state()
        if source_id not in sources:
            raise TrackingError("Unknown source ID")
        source = sources[source_id]
        bindings = [r for r in self.load() if r["kind"] == "binding" and r["source_id"] == source_id]
        if source["path"] == relative and bindings and bindings[-1]["path"] == relative and bindings[-1]["byte_sha256"] == expected_hash:
            return bindings[-1]
        inventory = scan(self.vault)
        require_complete(inventory)
        destination = entry_for(inventory["entries"], relative)
        case_only = (relative.casefold() == source["path"].casefold() and relative != source["path"]
                     and destination is not None and destination["path"] == relative)
        if ((secure_path(self.vault, source["path"]).exists() and not case_only)
                or any(sid != source_id and s["path"].casefold() == relative.casefold() for sid, s in sources.items())):
            raise TrackingError("Old path exists or destination already has an identity")
        known = {source["fingerprint"]["byte_sha256"]}
        known.update(r["start"]["fingerprint"]["byte_sha256"] for r in runs.values() if r["start"]["source_id"] == source_id)
        if destination is None or destination["eligibility"] != "eligible" or destination["byte_sha256"] != expected_hash or expected_hash not in known:
            raise TrackingError("Binding requires an eligible capture matching a known byte hash")
        return self.record("binding", "B-" + uuid.uuid4().hex, {"source_id": source_id, "old_path": source["path"], "path": relative, "byte_sha256": expected_hash})

    def report(self, inventory):
        sources, runs = self.state()
        by_path = {s["path"].casefold(): sid for sid, s in sources.items()}
        case_counts = Counter(p.casefold() for p in inventory["entries"])
        lines = ["---", "title: Synthesis Coverage", "tags: [synthesis-tracking, provenance]",
                 f"runtime: {json.dumps(self.runtime)}", "generated: synthesis-tracker", "---", "",
                 "# Synthesis Coverage", "", "Generated: " + inventory["scanned_at"], "",
                 "> A citation is referenced, not verified. Reviews cover a captured file, not the whole book behind its highlights. Source reuse is welcome.", "",
                 "## Inventory", "", f"- Registered identities: {len(sources)}", f"- Scan errors: {len(inventory['errors'])}",
                 f"- Ambiguous links: {len(inventory['ambiguous'])}", f"- Unresolved links: {len(inventory['unresolved'])}"]
        counts = Counter(e["eligibility"] for e in inventory["entries"].values())
        lines += [f"- {key}: {count}" for key, count in sorted(counts.items())]
        lines += ["", "## Source coverage", "", "| Source | Eligibility | Recorded review | Current references | Revision |", "|---|---|---|---|---|"]
        for relative, entry in sorted(inventory["entries"].items()):
            if entry["eligibility"] == "attachment-only":
                continue
            refs = sorted({r["wiki"] for r in inventory["references"].get(relative, [])})
            ambiguous_case = case_counts[relative.casefold()] > 1
            sid = None if ambiguous_case else by_path.get(relative.casefold())
            history = [r for r in runs.values() if r["start"]["source_id"] == sid]
            completed = [r for r in history if r["end"] and r["end"]["kind"] == "completion"]
            status = "legacy review unknown" if refs else "no recorded review"
            if ambiguous_case:
                status = "case-colliding capture; no review identity inferred"
            revision = "no reviewed snapshot"
            if completed:
                last = completed[-1]
                status = last["end"]["reviewed_extent"] + "; " + last["end"]["outcome"]
                if any(r["start"]["fingerprint"]["body_sha256"] != entry["body_sha256"] for r in completed):
                    revision = "content differs from recorded reviews; inspect scopes/topics"
                elif any(r["start"]["fingerprint"]["byte_sha256"] != entry["byte_sha256"] for r in completed):
                    revision = "metadata/normalization-only difference; inspect provenance"
                else:
                    revision = "matches recorded snapshots"
            if any(r["end"] is None for r in history):
                status += "; pending/interrupted"
            if any(r["end"] and r["end"]["kind"] == "failure" for r in history):
                status += "; failure in history"
            if entry.get("empty_body"):
                status += "; empty capture body"
            lines.append("| " + " | ".join([wikilink(relative), cell(entry["eligibility"]), cell(status), "; ".join(wikilink(x) for x in refs) or "none", cell(revision)]) + " |")
        lines += ["", "## Review history and contributions", ""]
        for rid, run in runs.items():
            start, end = run["start"], run["end"]
            result = end["outcome"] if end else "pending/interrupted"
            scope = end.get("reviewed_scope", start["scope"]) if end else start["scope"]
            lines.append(f"- {cell(rid)}: **{cell(result)}**; scope: {cell(scope)}; source: {cell(start['source_id'])}")
            for contribution in end.get("contributions", []) if end else []:
                lines.append("  - " + cell(contribution["target"]) + ": " + cell(contribution["description"]))
        lines += ["", "## Missing/unobserved registered sources", ""]
        for sid, source in sources.items():
            if case_counts[source["path"].casefold()] > 1:
                lines.append(f"- {cell(sid)}: {cell(source['path'])} — ambiguous capture spelling; reconcile explicitly")
            elif not any(p.casefold() == source["path"].casefold() for p in inventory["entries"]):
                status = "unobserved (scan incomplete)" if inventory["errors"] else "not found in configured scan; identity retained"
                lines.append(f"- {cell(sid)}: {cell(source['path'])} — {status}")
        lines += ["", "## Scan/link issues", ""]
        lines += ["- " + cell(error) for error in inventory["errors"]]
        for issue in inventory["ambiguous"] + inventory["unresolved"]:
            lines.append("- " + cell(issue["wiki"]) + ": " + cell(issue["target"]) + " — " + cell(issue.get("reason", "ambiguous: " + ", ".join(issue.get("candidates", [])))))
        lines += ["", "## Limits", "", "- Explicit scans only; one writer machine. No semantic verification or proof of reading.",
                  "- Hashes are not archived source versions. Earlier differing snapshots are not automatically cleared by a later partial review.",
                  "- Local lock/append-only conventions do not guarantee distributed transactions, tamper resistance or external edit isolation.",
                  "- Private classification is a compilation boundary, not the existing indexer's privacy policy.", ""]
        return "\n".join(lines)

    def write_report(self, inventory):
        if not self._locked:
            raise TrackingError("Report writes require the local lock")
        path, temp = self.path("Coverage.md"), self.path("Coverage.md.tmp")
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(self.report(inventory))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path.cwd())
    parser.add_argument("--tracking-dir", default="tracking", help="Vault-relative record directory outside raw/wiki and kit folders")
    parser.add_argument("--runtime", help="Actual runtime/model, required for writes")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("scan")
    command.add_argument("--write", action="store_true")
    command.add_argument("--details", action="store_true")
    command = sub.add_parser("register")
    command.add_argument("path")
    command = sub.add_parser("begin")
    command.add_argument("source_id")
    command.add_argument("--scope", required=True)
    command.add_argument("--extent", choices=("partial", "all-captured"), required=True)
    command.add_argument("--purpose", required=True)
    command = sub.add_parser("complete")
    command.add_argument("run_id")
    command.add_argument("--outcome", choices=("incorporated", "rechecked-existing", "no-addition"), required=True)
    command.add_argument("--contribution", action="append", default=[], help="wiki/path#Heading|Specific contribution")
    command.add_argument("--evidence", action="append", default=[])
    command.add_argument("--note", required=True)
    command.add_argument("--reviewed-scope")
    command.add_argument("--reviewed-extent", choices=("partial", "all-captured"))
    command = sub.add_parser("fail")
    command.add_argument("run_id")
    command.add_argument("--note", required=True)
    command = sub.add_parser("bind")
    command.add_argument("source_id")
    command.add_argument("path")
    command.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    if (args.command != "scan" or args.write) and not args.runtime:
        raise TrackingError("--runtime is required for durable writes")
    tracker = Tracker(args.vault, runtime=args.runtime or "unattributed/read-only", tracking_dir=args.tracking_dir)
    if not tracker.vault.is_dir():
        raise TrackingError("Vault directory does not exist")
    if args.command == "scan":
        inventory = scan(tracker.vault)
        if args.write:
            with tracker.lock():
                tracker.write_report(inventory)
        else:
            tracker.state()
        result = {"text_captures": sum("byte_sha256" in e for e in inventory["entries"].values()),
                  "eligibility": dict(Counter(e["eligibility"] for e in inventory["entries"].values())),
                  "registered": len(tracker.state()[0]), "errors": inventory["errors"],
                  "ambiguous_links": len(inventory["ambiguous"]), "unresolved_links": len(inventory["unresolved"]),
                  "report_written": args.write}
        if args.details:
            result.update(ambiguous=inventory["ambiguous"], unresolved=inventory["unresolved"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if inventory["errors"] else 0
    with tracker.lock():
        if args.command == "register":
            result = tracker.register(args.path)
        elif args.command == "begin":
            result = tracker.begin(args.source_id, args.scope, args.extent, args.purpose)
        elif args.command == "complete":
            contributions = []
            for value in args.contribution:
                target, separator, description = value.partition("|")
                if not separator:
                    raise TrackingError("Contribution must be target|description")
                contributions.append({"target": target, "description": description})
            result = tracker.complete(args.run_id, args.outcome, contributions, args.note, args.evidence, args.reviewed_scope, args.reviewed_extent)
        elif args.command == "fail":
            result = tracker.fail(args.run_id, args.note)
        else:
            result = tracker.bind(args.source_id, args.path, args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.exit(main())
    except (TrackingError, OSError, UnicodeError) as error:
        print(f"Tracking error: {error}", file=sys.stderr)
        sys.exit(2)
