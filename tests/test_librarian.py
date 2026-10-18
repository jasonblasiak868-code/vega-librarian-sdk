"""Functional tests: lesson ledger, memory, decision trace (ported from the flat-script versions, plus the new chain behavior)."""
import json
import os
import pathlib
import unittest

from support import BASE, fake_embed, fresh_ledger
from vega_librarian import decision_trace as dt
from vega_librarian import lesson_ledger as ll
from vega_librarian import librarian_memory as lm


class Ledger(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger()

    def tearDown(self):
        self.d.cleanup()

    def test_required_fields_enforced(self):
        for k in ll.REQUIRED:
            with self.assertRaises(ll.LessonRefused):
                ll.add({x: v for x, v in BASE.items() if x != k})

    def test_chain_and_tamper(self):
        ll.add(BASE)
        ll.add({**BASE, "title": "t2"})
        self.assertEqual(ll.verify(), (True, None))
        p = ll._paths()[0]
        lines = pathlib.Path(p).read_text(encoding="utf-8").splitlines()
        r = json.loads(lines[0])
        r["payload"]["root_cause"] = "edited"
        lines[0] = json.dumps(r, sort_keys=True)
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        ok, problems = ll.verify()
        self.assertFalse(ok)
        self.assertTrue(problems)

    def test_recall_ranks_the_relevant_lesson_first(self):
        ll.add({**BASE, "title": "manifest overwritten by tool", "tags": ["overwrite", "ledger", "append-only"],
                "what_happened": "tool opened the ledger file in write mode", "protect": "never open a ledger in w mode"})
        ll.add({**BASE, "title": "scan took too long", "tags": ["scan", "performance"], "what_happened": "walked millions of files"})
        self.assertIn("manifest", ll.recall("about to overwrite the ingest manifest ledger")[0][1]["title"])

    def test_preflight_surfaces_protect_text(self):
        ll.add({**BASE, "tags": ["overwrite", "ledger"], "protect": "make a hashed backup first", "severity": 5})
        hits = ll.preflight(["overwrite"])
        self.assertEqual((len(hits), hits[0][1]["protect"]), (1, "make a hashed backup first"))

    def test_grade_rewards_verified_prevention_and_counts_recurrence(self):
        ll.add({**BASE, "tags": ["x", "y"], "status": "open"})
        ll.add({**BASE, "tags": ["x", "y"], "verification": "test_real_path"})
        rows = ll._read()
        g0, g1 = ll.grade(rows[0], rows), ll.grade(rows[1], rows)
        self.assertEqual((g0["recurrence"], g1["prevention_strength"]), (1, 1.0))
        self.assertGreater(g0["weight"], g1["weight"])

    def test_parent_links_to_an_existing_lesson(self):
        first = ll.add(BASE)
        child = ll.add({**BASE, "title": "recurrence", "parent": first["hash"]})
        self.assertEqual(child["parent"], first["hash"])

    def test_mirrors_hold_identical_copies(self):
        d = fresh_ledger(mirrors=2)
        try:
            ll.add(BASE)
            a, b, c = (pathlib.Path(p).read_bytes() for p in ll._paths())
            self.assertTrue(a == b == c and a)
        finally:
            d.cleanup()

    def test_seal_writes_status_into_a_dag(self):
        import tempfile
        from vega_custody.dag.dag import Dag
        t = pathlib.Path(tempfile.mkdtemp())
        dag = Dag([t / "c.jsonl"], [t / "l.jsonl"])
        ll.add(BASE)
        e = ll.seal(dag)
        self.assertEqual((e["kind"], e["payload"]["lessons"], e["payload"]["chain_ok"]), ("LESSON_LEDGER_STATUS", 1, True))

    def test_legacy_flat_ledger_imports_with_its_chain_checked(self):
        import hashlib
        rows, prev = [], "0" * 64
        for i in (1, 2):
            rec = {"seq": i, "recorded_at": "2026-10-01T00:00:00Z", **BASE, "title": "old %d" % i, "prev_hash": prev}
            h = hashlib.sha256((prev + json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
            rec["hash"], rec["id"] = h, h[:12]
            rows.append(rec)
            prev = h
        p = os.path.join(self.d.name, "legacy.jsonl")
        open(p, "w", encoding="utf-8").write("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n")
        out = ll.import_legacy(p)
        self.assertEqual([r["title"] for r in out], ["old 1", "old 2"])
        self.assertEqual(ll.verify(), (True, None))


class Memory(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger()
        self.lib = lm.Librarian(os.path.join(self.d.name, "lib.db"), embed=fake_embed)

    def tearDown(self):
        self.lib.close()
        self.d.cleanup()

    def test_paraphrase_with_no_shared_words_finds_the_lesson(self):
        ll.add({**BASE, "title": "Credential flipped between two values", "what_happened": "the same credential name was stored twice with duplicate copies",
                "tags": ["credentials", "persistence"], "expect": "a credential that flips exists twice"})
        ll.add({**BASE, "title": "Scan took too long", "what_happened": "walked millions of files", "tags": ["scan", "performance"]})
        self.lib.ingest_lessons()
        hit = self.lib.recall("someone keeps changing the saved login value mysteriously")
        self.assertTrue(hit)
        self.assertIn("Credential flipped", hit[0]["title"])

    def test_abstains_when_nothing_matches(self):
        ll.add({**BASE, "title": "Scan took too long", "what_happened": "walked millions of files", "tags": ["scan"]})
        self.lib.ingest_lessons()
        self.assertEqual(self.lib.recall("what is the capital of France"), [])

    def test_embedder_down_falls_back_to_word_match_and_says_so(self):
        ll.add({**BASE, "title": "Watchdog restarted the service", "what_happened": "a watchdog restarted neo4j", "tags": ["watchdog", "restart", "service"]})

        def down(t):
            raise RuntimeError("ollama down")
        with lm.Librarian(os.path.join(self.d.name, "lib2.db"), embed=down) as lib:
            lib.ingest_lessons()
            hit = lib.recall("watchdog restart of the service")
            self.assertTrue(hit)
            self.assertEqual(hit[0]["mode"], "words-only")
            self.assertGreater(lib.embed_failures, 0)

    def test_tiers(self):
        r1 = ll.add({**BASE, "title": "recent one"})
        self.assertEqual(self.lib.tier_of(r1), "short")
        old = dict(r1, recorded_at="2026-01-01T00:00:00Z", status="closed")
        self.assertEqual(self.lib.tier_of(old), "long")
        self.assertEqual(self.lib.tier_of(dict(old, status="open")), "short")

    def test_full_text_is_stored_whole(self):
        long = "x" * 6000
        ll.add({**BASE, "what_happened": long})
        self.lib.ingest_lessons()
        self.assertIn(long, self.lib.con.execute("select text_full from memory").fetchone()[0])

    def test_cards_move_forward_only(self):
        h = "ab" * 32
        self.lib.register_card("pre:1", "PRE", "conductor", h, "candidate")
        self.lib.register_card("pre:1", "PRE", "conductor", h, "locked")
        self.assertEqual(self.lib.cards("PRE")[0][2], "locked")

    def test_conductor_registry_sync(self):
        rows = [{"kind": "WORKFLOW_CANDIDATE_PRE", "workflow_id": "w" * 20, "result_hash": "cd" * 32},
                {"kind": "WORKFLOW_REPLAY", "workflow_id": "w" * 20, "result_hash": "cd" * 32, "status": "locked"}]
        self.assertEqual(self.lib.sync_conductor_registry(rows), 1)
        self.assertEqual(self.lib.cards("PRE")[0][2], "locked")


class Trace(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger()

    def tearDown(self):
        self.d.cleanup()

    def test_build_why_precedents(self):
        a = ll.add({**BASE, "title": "first", "tags": ["x", "y"], "prevention": "append-only writers and a preflight check"})
        ll.add({**BASE, "title": "second", "tags": ["x", "y"], "parent": a["hash"]})
        out = os.path.join(self.d.name, "trace")
        nodes, edges = dt.build(out)
        self.assertTrue({"lesson", "context", "control"} <= {n["kind"] for n in nodes.values()})
        walk = dt.why(out, "lesson:" + ll._read()[1]["id"])
        self.assertTrue(any(n["label"] == "first" for _, n, _, _ in walk))
        self.assertTrue(dt.precedents(out, "x y second first"))

    def test_every_edge_states_its_basis(self):
        ll.add(BASE)
        ll.add({**BASE, "title": "again"})
        _, edges = dt.build(os.path.join(self.d.name, "trace"))
        self.assertTrue(all(e["basis"] in ("declared", "derived_keyword", "derived_tag_overlap") for e in edges))


if __name__ == "__main__":
    unittest.main()
