"""Contract fixtures and reproducible generated invariants; no personal vaults."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import index_vault, synthesis_tracker as tracking


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "raw").mkdir()
        (self.root / "wiki").mkdir()
        self.tracker = tracking.Tracker(self.root, runtime="test/fixture")
        self.source("raw/books/Highlights.md", "# Highlights\nSelected thought.\n")
        self.source("wiki/Topic A.md", "# Topic A\n## Idea\nA claim.[[raw/books/Highlights]]\n")
        self.source("wiki/Topic B.md", "# Topic B\nAnother claim.[[raw/books/Highlights]]\n")

    def source(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def register(self, path="raw/books/Highlights.md"):
        with self.tracker.lock():
            return self.tracker.register(path)["source_id"]

    def begin(self, sid, scope="First captured highlight", extent="partial", purpose="Assess selected idea"):
        with self.tracker.lock():
            return self.tracker.begin(sid, scope, extent, purpose)["run_id"]

    def finish(self, rid, outcome="incorporated", contributions=None, **kwargs):
        if contributions is None:
            contributions = [
                {"target": "wiki/Topic A#Idea", "description": "Selected idea"},
                {"target": "wiki/Topic B", "description": "Related selected idea"},
            ] if outcome != "no-addition" else []
        with self.tracker.lock():
            return self.tracker.complete(rid, outcome, contributions, "Bounded source/prose assessment", **kwargs)

    def test_category_inventory_and_legacy_uncertainty(self):
        for category in ("articles", "podcasts", "videos", "courses", "notes"):
            self.source(f"raw/{category}/Example.md", "A captured source.")
        self.source("raw/podcasts/Episode.vtt", "WEBVTT\n\n00:00.000 --> 00:01.000\nA line.")
        self.source("raw/videos/Episode.srt", "1\n00:00:00,000 --> 00:00:01,000\nA line.")
        self.source("raw/images/Figure.png", "fixture placeholder, not image content")
        self.source("raw/Index.md", "---\ntype: index\n---\nAn index.")
        inventory = tracking.scan(self.root)
        self.assertFalse(inventory["errors"])
        self.assertEqual(inventory["entries"]["raw/images/Figure.png"]["eligibility"], "attachment-only")
        self.assertEqual(inventory["entries"]["raw/Index.md"]["eligibility"], "manifest")
        self.assertEqual(len(inventory["references"]["raw/books/Highlights.md"]), 2)
        sid = self.register()
        source = self.tracker.state()[0][sid]
        self.assertEqual(source["historical_review"], "unknown")
        self.assertNotIn("reviewed_at", source)
        self.assertFalse(self.tracker.state()[1])
        self.assertEqual(self.register(), sid)

    def test_source_reuse_scoped_history_and_retries(self):
        originals = {p: p.read_bytes() for p in self.root.rglob("*.md")}
        sid = self.register()
        rid = self.begin(sid)
        self.assertEqual(self.begin(sid), rid)
        first = self.finish(rid)
        self.assertEqual(self.finish(rid), first)
        self.assertEqual(len(first["contributions"]), 2)
        rid2 = self.begin(sid, scope="Different captured passage")
        self.finish(rid2, "no-addition")
        self.assertEqual(len(self.tracker.state()[1]), 2)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        report = self.tracker.report(tracking.scan(self.root))
        self.assertIn("partial", report)
        self.assertIn("not the whole book", report)
        self.assertIn("no-addition", report)
        self.assertNotIn("100%", report)

    def test_historical_completion_retry_after_later_changes(self):
        rid = self.begin(self.register())
        first = self.finish(rid)
        self.source("raw/books/Highlights.md", "Changed later")
        self.source("wiki/Topic A.md", "Changed later too")
        self.assertEqual(self.finish(rid), first)
        self.assertIn("content differs", self.tracker.report(tracking.scan(self.root)))

    def test_actual_scope_narrows_and_previous_revisions_remain_visible(self):
        sid = self.register()
        rid = self.begin(sid, extent="all-captured")
        self.finish(rid, "no-addition", reviewed_scope="Only a visible prefix", reviewed_extent="partial")
        self.source("raw/books/Highlights.md", "Selected thought.\nNew thought.")
        rid2 = self.begin(sid, scope="Only new thought")
        self.finish(rid2, "no-addition")
        report = self.tracker.report(tracking.scan(self.root))
        self.assertIn("Only a visible prefix", report)
        self.assertIn("content differs", report)
        self.assertEqual(self.tracker.state()[1][rid]["end"]["reviewed_extent"], "partial")

    def test_metadata_and_body_changes_are_distinguished(self):
        rid = self.begin(self.register())
        self.finish(rid)
        self.source("raw/books/Highlights.md", "---\ntitle: New metadata\n---\n# Highlights\nSelected thought.\n")
        self.assertIn("metadata/normalization", self.tracker.report(tracking.scan(self.root)))
        self.source("raw/books/Highlights.md", "# Highlights\nSelected thought changed.\n")
        self.assertIn("content differs", self.tracker.report(tracking.scan(self.root)))

    def test_changed_pending_source_different_start_and_terminal_conflict(self):
        sid = self.register()
        rid = self.begin(sid)
        with self.assertRaises(tracking.TrackingError):
            self.begin(sid, scope="Another scope")
        self.source("raw/books/Highlights.md", "Changed during review")
        with self.assertRaises(tracking.TrackingError):
            self.begin(sid)
        with self.assertRaises(tracking.TrackingError):
            self.finish(rid)
        self.assertIsNone(self.tracker.state()[1][rid]["end"])
        with self.tracker.lock():
            failed = self.tracker.fail(rid, "Source changed during review")
            self.assertEqual(self.tracker.fail(rid, "Source changed during review"), failed)
            with self.assertRaises(tracking.TrackingError):
                self.tracker.fail(rid, "A different reason")
        with self.assertRaises(tracking.TrackingError):
            self.finish(rid, "no-addition")

    def test_rename_binding_and_matching_copies(self):
        sid = self.register()
        source = self.tracker.state()[0][sid]
        original = self.root / source["path"]
        copy = self.source("raw/books/Copy.md", original.read_text(encoding="utf-8"))
        self.assertNotEqual(self.register("raw/books/Copy.md"), sid)
        with self.tracker.lock():
            with self.assertRaises(tracking.TrackingError):
                self.tracker.bind(sid, "raw/books/Copy.md", source["fingerprint"]["byte_sha256"])
        original.rename(original.with_name("Renamed.md"))
        with self.tracker.lock():
            binding = self.tracker.bind(sid, "raw/books/Renamed.md", source["fingerprint"]["byte_sha256"])
            self.assertEqual(self.tracker.bind(sid, "raw/books/Renamed.md", source["fingerprint"]["byte_sha256"]), binding)
        current = self.tracker.state()[0][sid]
        self.assertEqual(current["path"], "raw/books/Renamed.md")
        self.assertEqual(current["prior_paths"], [source["path"]])
        self.assertTrue(copy.is_file())

    def test_ambiguous_basenames_aliases_comments_and_code(self):
        self.source("raw/articles/Highlights.md", "---\naliases: [shared]\n---\nA different source")
        self.source("raw/articles/Another.md", "---\naliases:\n  - shared\n---\nAnother source")
        self.source("wiki/Ambiguous.md", "[[Highlights|label]]\n[[shared]]\n```md\n[[raw/articles/Another]]\n```\n`[[raw/articles/Another]]`\n%% [[raw/articles/Another]] %%\n<!-- [[raw/articles/Another]] -->")
        inventory = tracking.scan(self.root)
        self.assertEqual(len(inventory["ambiguous"]), 2)
        self.assertNotIn("raw/articles/Another.md", inventory["references"])

    def test_relative_encoded_markdown_and_block_links(self):
        self.source("raw/articles/An idea.md", "# Unique heading\nA line. ^passage\n")
        self.source("wiki/Relative.md", "[source](../raw/articles/An%20idea.md#unique-heading)\n[[../raw/articles/An idea#^passage]]")
        inventory = tracking.scan(self.root)
        self.assertEqual(len(inventory["references"]["raw/articles/An idea.md"]), 2)
        self.source("wiki/Bad.md", "[escape](%2e%2e/%2e%2e/outside.md)\n[unsupported][ref]")
        inventory = tracking.scan(self.root)
        self.assertTrue(any(x["wiki"] == "wiki/Bad.md" for x in inventory["unresolved"]))

    def test_destination_citation_heading_and_evidence_locator(self):
        rid = self.begin(self.register())
        self.source("wiki/Unrelated.md", "# Unrelated\nNo source citation")
        for target in ("wiki/Unrelated", "wiki/Topic A#Missing"):
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid, contributions=[{"target": target, "description": "Claim"}])
        self.source("wiki/Topic A.md", "# Topic A\n## Idea\n[[raw/books/Highlights]]\n## Idea\nDuplicate heading")
        with self.assertRaises(tracking.TrackingError):
            self.finish(rid)
        self.source("wiki/Topic A.md", "# Topic A\n## Idea\n[[raw/books/Highlights]]\n")
        self.source("docs/evidence.md", "# Evidence\nRecorded observation. ^evidence-1\n")
        with self.assertRaises(tracking.TrackingError):
            self.finish(rid, evidence=["docs/evidence#Missing"])
        self.finish(rid, evidence=["docs/evidence#^evidence-1"])

    def test_final_completion_recheck_observes_source_and_destination_edits(self):
        rid = self.begin(self.register())
        real_scan = tracking.scan
        def changing_scan(root):
            result = real_scan(root)
            self.source("wiki/Topic A.md", "Citation removed after scan")
            return result
        with patch.object(tracking, "scan", side_effect=changing_scan):
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid)
        self.assertIsNone(self.tracker.state()[1][rid]["end"])

    def test_privacy_metadata_and_malformed_frontmatter(self):
        for name, fields in {
            "Diary": "", "Quoted": "privacy: 'private'", "List": "privacy: [private]",
            "Block": "privacy:\n  - private", "Unknown": "privacy: team-only",
            "Mapping": "privacy: {access: private}", "Receipt": "type: receipt",
        }.items():
            path = f"raw/diaries/{name}.md" if name == "Diary" else f"raw/notes/{name}.md"
            self.source(path, f"---\n{fields}\n---\nPrivate fixture")
        inventory = tracking.scan(self.root)
        self.assertTrue(all(x["eligibility"] == "private-review-needed" for p, x in inventory["entries"].items() if p.startswith(("raw/diaries/", "raw/notes/"))))
        with self.assertRaises(tracking.TrackingError):
            self.register("raw/notes/List.md")
        self.source("raw/notes/Broken.md", "---\nprivacy: [private\n---\nBody")
        self.assertTrue(tracking.scan(self.root)["errors"])

    def test_unreadable_or_missing_roots_do_not_manufacture_deletion(self):
        sid = self.register()
        real_read = tracking.read_source
        def unavailable(path):
            if Path(path).name == "Highlights.md":
                raise OSError("offline cloud placeholder")
            return real_read(path)
        with patch.object(tracking, "read_source", side_effect=unavailable):
            inventory = tracking.scan(self.root)
        self.assertTrue(inventory["errors"])
        self.assertIn("unobserved", self.tracker.report(inventory))
        self.assertIn(sid, self.tracker.state()[0])
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(tracking.scan(Path(temp))["errors"])

    def test_lock_conflict_corrupt_record_and_unsupported_schema(self):
        self.register()
        with self.tracker.lock():
            with self.assertRaises(tracking.TrackingError):
                with self.tracker.lock():
                    pass
        record = next((self.tracker.store / "sources").glob("*.md"))
        original = record.read_bytes()
        record.write_text("Truncated partial write", encoding="utf-8")
        with self.assertRaises(tracking.TrackingError):
            self.tracker.state()
        record.write_bytes(original)
        record.with_name(record.stem + " (conflict).md").write_bytes(original)
        with self.assertRaises(tracking.TrackingError):
            self.tracker.state()

    def test_sequence_gap_and_backwards_clocks(self):
        with patch.object(tracking, "now", return_value="2026-10-11T12:00:00+00:00"):
            sid = self.register()
            rid = self.begin(sid)
        with patch.object(tracking, "now", return_value="2026-10-10T12:00:00+00:00"):
            self.finish(rid)
        self.assertEqual([r["sequence"] for r in self.tracker.load()], [1, 2, 3])
        (self.tracker.store / "events" / f"{rid}.md").unlink()
        with self.assertRaises(tracking.TrackingError):
            self.tracker.state()

    def test_custom_tracking_path_containment_and_junctions(self):
        other = tracking.Tracker(self.root, runtime="test/fixture", tracking_dir="metadata/reviews")
        with other.lock():
            other.register("raw/books/Highlights.md")
        self.assertTrue((self.root / "metadata/reviews/sources").is_dir())
        for relative in (".", "raw", "raw/reviews", "wiki", "docs/reviews", "../outside", "C:/outside", "tracking:stream", "tracking.", "NUL", "tracking/COM1.md"):
            with self.subTest(relative=relative), self.assertRaises(tracking.TrackingError):
                tracking.Tracker(self.root, runtime="test/fixture", tracking_dir=relative)
        actual = tracking.is_link_or_junction
        with patch.object(tracking, "is_link_or_junction", side_effect=lambda p: str(p) == str(self.root / "raw/books") or actual(p)):
            self.assertTrue(tracking.scan(self.root)["errors"])

    def test_report_escaping_and_index_scope_remains_unchanged(self):
        rid = self.begin(self.register())
        self.finish(rid, contributions=[{"target": "wiki/Topic A#Idea", "description": "A | description <script>\nsecond line"}])
        report = self.tracker.report(tracking.scan(self.root))
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        pointers = index_vault.scan_vault(str(self.root))
        self.assertTrue(all(p.rel_path.startswith(("raw/", "wiki/")) for p in pointers))
        self.assertFalse(any(p.rel_path.startswith("tracking/") for p in pointers))

    def test_unsupported_and_unnumbered_journal_formats_fail_closed(self):
        self.register()
        record = next((self.tracker.store / "sources").glob("*.md"))
        original = record.read_text(encoding="utf-8")
        for mutate in (lambda r: r.update(schema=99), lambda r: r.update(schema=True),
                       lambda r: r.pop("sequence"), lambda r: r.update(legacy_references=[{}])):
            payload = json.loads(tracking.RECORD_JSON.findall(original)[0])
            mutate(payload)
            record.write_text(tracking.RECORD_JSON.sub(lambda _: "```json\n" + json.dumps(payload) + "\n```", original), encoding="utf-8")
            with self.assertRaises(tracking.TrackingError):
                self.tracker.state()
        record.write_text(original, encoding="utf-8")

    def test_interrupted_report_does_not_overwrite_existing_report(self):
        inventory = tracking.scan(self.root)
        with self.tracker.lock():
            self.tracker.write_report(inventory)
        before = (self.tracker.store / "Coverage.md").read_bytes()
        (self.tracker.store / "Coverage.md.tmp").write_text("partial report", encoding="utf-8")
        with self.tracker.lock():
            with self.assertRaises(FileExistsError):
                self.tracker.write_report(inventory)
        self.assertEqual((self.tracker.store / "Coverage.md").read_bytes(), before)

    def test_source_change_at_final_recheck_and_pending_report(self):
        rid = self.begin(self.register())
        real_read = tracking.read_source
        source_reads = 0
        def changing_read(path):
            nonlocal source_reads
            if Path(path).name == "Highlights.md":
                source_reads += 1
                if source_reads == 2:
                    self.source("raw/books/Highlights.md", "Changed after structural validation")
            return real_read(path)
        with patch.object(tracking, "read_source", side_effect=changing_read):
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid)
        self.assertIn("pending/interrupted", self.tracker.report(tracking.scan(self.root)))

    def test_private_scalar_cannot_be_discarded_by_malformed_list_or_duplicate(self):
        examples = ["privacy: private\n  - public", "type: receipt\n  - article",
                    "privacy: [private]\n  - public", "privacy: private\nprivacy: public"]
        for number, fields in enumerate(examples):
            path = f"raw/notes/Unsafe-{number}.md"
            self.source(path, f"---\n{fields}\n---\nA fixture")
            self.assertTrue(tracking.scan(self.root)["errors"])
            with self.assertRaises(tracking.TrackingError):
                self.register(path)
            (self.root / path).unlink()
        self.source("raw/notes/Commented.md", "---\nprivacy: # classification\n  - private\n---\nA fixture")
        self.assertEqual(tracking.scan(self.root)["entries"]["raw/notes/Commented.md"]["eligibility"], "private-review-needed")

    def test_matching_backtick_runs_and_shorter_fences_do_not_credit_code(self):
        rid = self.begin(self.register())
        for body in ("# Topic A\n## Idea\n``[[raw/books/Highlights]]``",
                     "# Topic A\n## Idea\nPrefix ```[[raw/books/Highlights]]``` suffix",
                     "# Topic A\n## Idea\n`````md\n```\n[[raw/books/Highlights]]\n`````"):
            self.source("wiki/Topic A.md", body)
            self.assertFalse(any(r["wiki"] == "wiki/Topic A.md" for r in tracking.scan(self.root)["references"].get("raw/books/Highlights.md", [])))
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid)

    def test_block_mentions_are_not_locator_definitions(self):
        rid = self.begin(self.register())
        self.source("wiki/Topic A.md", "# Topic A\n## Idea\n[[raw/books/Highlights]]\nSee ^passage later.")
        with self.assertRaises(tracking.TrackingError):
            self.finish(rid, contributions=[{"target": "wiki/Topic A#^passage", "description": "Idea"}])
        self.source("wiki/Topic A.md", "# Topic A\n## Idea\n[[raw/books/Highlights]] ^passage\n")
        self.finish(rid, contributions=[{"target": "wiki/Topic A#^passage", "description": "Idea"}])

    def test_case_only_rename_keeps_identity_and_pending_completion(self):
        sid = self.register()
        rid = self.begin(sid)
        source = self.tracker.state()[0][sid]
        original = self.root / source["path"]
        renamed = original.with_name("highlights.md")
        original.rename(renamed)
        self.assertEqual(self.register("raw/books/highlights.md"), sid)
        self.finish(rid)
        report = self.tracker.report(tracking.scan(self.root))
        self.assertNotIn("not found in configured scan", report)
        self.assertIn("matches recorded snapshots", report)
        with self.tracker.lock():
            self.tracker.bind(sid, "raw/books/highlights.md", source["fingerprint"]["byte_sha256"])
        self.assertEqual(self.tracker.state()[0][sid]["path"], "raw/books/highlights.md")

    def test_generated_metadata_cannot_hide_a_capture_or_missing_identity(self):
        sid = self.register()
        self.source("raw/books/Highlights.md", "---\ngenerated: synthesis-tracker\n---\n# Highlights\nSelected thought.\n")
        inventory = tracking.scan(self.root)
        self.assertIn("raw/books/Highlights.md", inventory["entries"])
        self.assertNotIn("not found in configured scan", self.tracker.report(inventory))
        self.assertEqual(self.register(), sid)

    def test_registration_retry_does_not_grant_changed_privacy_permission(self):
        sid = self.register()
        self.source("raw/books/Highlights.md", "---\nprivacy: private\n---\nA changed capture")
        self.assertEqual(self.register(), sid)
        with self.assertRaises(tracking.TrackingError):
            self.begin(sid)

    def test_case_colliding_report_does_not_assign_history_to_two_captures(self):
        sid = self.register()
        self.finish(self.begin(sid))
        inventory = tracking.scan(self.root)
        original = inventory["entries"]["raw/books/Highlights.md"]
        inventory["entries"]["raw/books/highlights.md"] = {**original, "path": "raw/books/highlights.md"}
        report = self.tracker.report(inventory)
        self.assertIn("case-colliding capture; no review identity inferred", report)
        self.assertIn("ambiguous capture spelling", report)
        with self.assertRaises(tracking.TrackingError):
            tracking.entry_for(inventory["entries"], "raw/books/Highlights.md")

    def test_opaque_code_and_comments_cannot_manufacture_citation_targets(self):
        rid = self.begin(self.register())
        for middle in ("``hide``", "<!-- hide -->", "%% hide %%"):
            self.source("wiki/Topic A.md", f"# Topic A\n## Idea\n[[raw/{middle}books/Highlights]]")
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid)
            self.assertTrue(any(x["wiki"] == "wiki/Topic A.md" for x in tracking.scan(self.root)["unresolved"]))

    def test_opaque_trailing_content_cannot_manufacture_block_definitions(self):
        rid = self.begin(self.register())
        for trailing in ("``later``", "``\nlater``", "<!-- later -->", "%% later %%"):
            body = f"# Topic A\n## Idea\n[[raw/books/Highlights]]\nsee ^passage {trailing}\n"
            self.source("wiki/Topic A.md", body)
            clean = tracking.linkable(body)
            self.assertEqual(len(clean), len(body))
            self.assertEqual(clean.count("\n"), body.count("\n"))
            with self.assertRaises(tracking.TrackingError):
                self.finish(rid, contributions=[{"target": "wiki/Topic A#^passage", "description": "Idea"}])


class GeneratedInvariantTests(unittest.TestCase):
    def test_generated_bom_newline_and_frontmatter_normalization(self):
        rng = random.Random(2026)
        expected = hashlib.sha256(b"# Highlights\nA thought.\n").hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Capture.md"
            for _ in range(48):
                newline = rng.choice(["\n", "\r\n", "\r"])
                text = f"---\ntitle: Version {rng.randrange(10000)}\n---\n# Highlights\nA thought.\n"
                data = text.replace("\n", newline).encode("utf-8")
                if rng.choice([False, True]):
                    data = b"\xef\xbb\xbf" + data
                path.write_bytes(data)
                self.assertEqual(tracking.fingerprint(path)[0]["body_sha256"], expected)
                self.assertEqual(path.read_bytes(), data)

    def test_generated_review_sequences_preserve_history_identity_and_source_bytes(self):
        rng = random.Random(20261011)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "raw/books").mkdir(parents=True)
            (root / "wiki").mkdir()
            tracker = tracking.Tracker(root, runtime="test/generated")
            identifiers, expected_records = set(), 0
            for number in range(24):
                path = root / f"raw/books/Capture-{number}.md"
                body = f"# Highlights\nSelected idea {rng.randrange(1000000)}.\n"
                path.write_text(body, encoding="utf-8")
                with tracker.lock():
                    sid = tracker.register(path.relative_to(root).as_posix())["source_id"]
                identifiers.add(sid)
                expected_records += 1
                for _ in range(rng.randrange(1, 4)):
                    previous = [(r["record_id"], r["sequence"]) for r in tracker.load()]
                    before = path.read_bytes()
                    with tracker.lock():
                        start = tracker.begin(sid, f"Scope {expected_records}", "partial", "Generated fixture")
                        end = tracker.complete(start["run_id"], "no-addition", [], "Reviewed captured scope")
                        self.assertEqual(tracker.complete(start["run_id"], "no-addition", [], "Reviewed captured scope"), end)
                    expected_records += 2
                    current = tracker.load()
                    self.assertEqual([(r["record_id"], r["sequence"]) for r in current[:len(previous)]], previous)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(len(current), expected_records)
                    self.assertEqual([r["sequence"] for r in current], list(range(1, expected_records + 1)))
            self.assertEqual(len(identifiers), 24)

    def test_generated_unsafe_paths_never_create_tracking_files(self):
        rng = random.Random(71)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for _ in range(100):
                leaf = f"outside-{rng.randrange(1000000)}"
                unsafe = rng.choice([f"../{leaf}", f"raw/{leaf}", f"docs/{leaf}", f"{leaf}:stream", f"{leaf}.", f"C:/{leaf}"])
                with self.assertRaises(tracking.TrackingError):
                    tracking.Tracker(root, runtime="test/generated", tracking_dir=unsafe)
            self.assertEqual(list(root.iterdir()), [])


class CLITests(unittest.TestCase):
    def test_disposable_multi_category_cli_pilot(self):
        script = Path(tracking.__file__).resolve()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            captures = {
                "raw/books/Highlights.md": "# Highlights\nAn idea.\n",
                "raw/articles/Article.md": "# Article\nAnother idea.\n",
                "raw/podcasts/Episode.vtt": "WEBVTT\n\n00:00.000 --> 00:01.000\nA line.\n",
                "raw/videos/Episode.srt": "1\n00:00:00,000 --> 00:00:01,000\nA line.\n",
                "raw/courses/Handout.md": "# Handout\nA lesson.\n",
                "raw/notes/Original.md": "# Original note\nMy thought.\n",
                "wiki/Topic.md": "# Topic\n## Idea\nClaim.[[raw/books/Highlights]]\n",
                "wiki/Related.md": "# Related\nRelated claim.[[raw/books/Highlights]]\n",
            }
            for relative, body in captures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            before = {p: (root / p).read_bytes() for p in captures}
            def run(*arguments):
                result = subprocess.run([sys.executable, "-B", str(script), "--vault", str(root),
                                         "--runtime", "test/cli-pilot", *arguments],
                                        cwd=temp, capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            inventory = run("scan")
            self.assertEqual(inventory["text_captures"], 6)
            self.assertFalse((root / "tracking").exists())
            source = run("register", "raw/books/Highlights.md")
            started = run("begin", source["source_id"], "--scope", "Selected highlight", "--extent", "partial", "--purpose", "Temporary CLI pilot")
            args = ("complete", started["run_id"], "--outcome", "rechecked-existing",
                    "--contribution", "wiki/Topic#Idea|Selected idea",
                    "--contribution", "wiki/Related|Related idea", "--note", "Reviewed captured excerpt, not the book")
            ended = run(*args)
            self.assertEqual(run(*args), ended)
            run("scan", "--write")
            report = (root / "tracking/Coverage.md").read_text(encoding="utf-8")
            self.assertIn("Related idea", report)
            for path, data in before.items():
                self.assertEqual((root / path).read_bytes(), data)
            (root / "raw/books/Highlights.md").write_text(captures["raw/books/Highlights.md"] + "New highlight.\n", encoding="utf-8")
            run("scan", "--write")
            self.assertIn("content differs", (root / "tracking/Coverage.md").read_text(encoding="utf-8"))

    def test_write_requires_attribution_and_empty_kit_is_portable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "raw").mkdir()
            (root / "wiki").mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tracking.main(["--vault", str(root), "scan"]), 0)
                self.assertFalse((root / "tracking").exists())
                with self.assertRaises(tracking.TrackingError):
                    tracking.main(["--vault", str(root), "scan", "--write"])
                self.assertEqual(tracking.main(["--vault", str(root), "--runtime", "test/cli", "scan", "--write"]), 0)
            self.assertTrue((root / "tracking/Coverage.md").is_file())


if __name__ == "__main__":
    unittest.main()
