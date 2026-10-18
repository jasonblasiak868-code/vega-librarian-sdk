"""RED TEAM PASS 1 (librarian, lesson ledger): secrets in lessons, forged edits, cut-off tails, torn writes, concurrent writers, hostile inputs."""
import hashlib
import json
import pathlib
import threading
import unittest

from support import BASE, fresh_ledger
from vega_librarian import lesson_ledger as ll


def lines(p):
    return pathlib.Path(p).read_text(encoding="utf-8").splitlines()


class SecretsAndShape(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger()

    def tearDown(self):
        self.d.cleanup()

    def test_a_credential_shaped_value_in_any_text_field_is_refused_and_nothing_is_written(self):
        for field in ("what_happened", "root_cause", "prevention", "protect", "circumstances", "title"):
            with self.assertRaises(ll.LessonRefused, msg=field):
                ll.add({**BASE, field: "we pasted sk-" + "a1B2c3D4" * 4 + " into the chat"})
        with self.assertRaises(ll.LessonRefused):
            ll.add({**BASE, "wrong_statements": ["the key is AKIA" + "ABCDEFGHIJKLMNOP"]})
        with self.assertRaises(ll.LessonRefused):
            ll.add({**BASE, "detection": "-----BEGIN " + "PRIVATE KEY-----\nabc"})
        self.assertEqual(ll._read(), [])

    def test_unknown_or_secret_named_fields_are_refused(self):
        for extra in ({"password": "x"}, {"api_key": "x"}, {"token": "x"}, {"__proto__": 1}, {"hash": "0" * 64}, {"seq": 99}, {"id": "forged"}, {"prev_hash": "0" * 64}):
            with self.assertRaises(ll.LessonRefused, msg=repr(extra)):
                ll.add({**BASE, **extra})

    def test_bad_types_and_ranges_never_reach_the_chain(self):
        for kw in ({"severity": True}, {"severity": "3"}, {"severity": 3.5}, {"severity": 0}, {"severity": 6}, {"severity": None},
                   {"tags": "a,b"}, {"tags": []}, {"tags": ["a", 5]}, {"tags": ["a", " "]}, {"status": "done"}, {"title": 5}, {"title": ["x"]},
                   {"related_dag": ["1"]}, {"related_dag": [True]}, {"related_dag": 5}, {"wrong_statements": "x"}, {"occurred_at": "yesterday"},
                   {"parent": "0" * 64}, {"parent": 5}):
            with self.assertRaises(ll.LessonRefused, msg=repr(kw)):
                ll.add({**BASE, **kw})
        for bad in (None, [], "x", 5):
            with self.assertRaises(ll.LessonRefused):
                ll.add(bad)
        self.assertEqual(ll._read(), [])

    def test_nan_and_non_json_values_are_refused(self):
        with self.assertRaises(ll.LessonRefused):
            ll.add({**BASE, "related_dag": [float("nan")]})

    def test_a_parent_must_be_a_lesson_not_some_other_chain_entry(self):
        from vega_custody.dag.dag import Dag
        import tempfile
        ll.add(BASE)
        # point the parent at the hash of a LESSON_LEDGER_STATUS entry written into the same file
        chain = ll._chain()
        status = chain.record("LESSON_LEDGER_STATUS", {"what": "x"})
        with self.assertRaises(ll.LessonRefused):
            ll.add({**BASE, "parent": status["entry_hash"]})

    def test_very_long_text_is_stored_whole(self):
        long = "z" * 200_000
        r = ll.add({**BASE, "what_happened": long})
        self.assertEqual(ll._read()[0]["what_happened"], long)
        self.assertEqual(r["what_happened"], long)


class TamperTruncationTornWrites(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger(mirrors=1)
        self.first = ll.add({**BASE, "title": "one"})
        ll.add({**BASE, "title": "two"})
        self.last = ll.add({**BASE, "title": "three"})
        self.primary, self.mirror = ll._paths()

    def tearDown(self):
        self.d.cleanup()

    def test_an_edited_entry_breaks_the_chain(self):
        rows = lines(self.primary)
        r = json.loads(rows[1])
        r["payload"]["root_cause"] = "forged"
        rows[1] = json.dumps(r, sort_keys=True, ensure_ascii=False)
        pathlib.Path(self.primary).write_text("\n".join(rows) + "\n", encoding="utf-8")
        ok, problems = ll.verify()
        self.assertFalse(ok)
        self.assertTrue(problems)

    def test_a_cut_off_tail_is_invisible_to_the_chain_alone_but_caught_by_a_remembered_head(self):
        for p in (self.primary, self.mirror):
            rows = lines(p)
            pathlib.Path(p).write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
        self.assertEqual(ll.verify(), (True, None))                                   # a shorter chain is still a valid chain: the honest limit
        ok, problems = ll.verify(expected_head=self.last["hash"])
        self.assertFalse(ok)
        self.assertTrue(any("truncated" in x or "expected head" in x for x in problems))
        ok, problems = ll.verify(min_length=3)
        self.assertFalse(ok)

    def test_a_whole_chain_rewritten_with_consistent_hashes_is_caught_by_the_remembered_head(self):
        """Attack: an attacker with disk access edits lesson one and recomputes every later hash so the chain is internally valid."""
        from vega_custody.dag import dag as dagmod
        rows = [json.loads(x) for x in lines(self.primary)]
        rows[0]["payload"]["root_cause"] = "rewritten history"
        prev = None
        for r in rows:
            if prev is not None:
                r["parents"] = [prev]
            r["entry_hash"] = dagmod._entry_hash(r)
            prev = r["entry_hash"]
        text = "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows) + "\n"
        for p in (self.primary, self.mirror):
            pathlib.Path(p).write_text(text, encoding="utf-8")
        self.assertEqual(ll.verify(), (True, None))                                   # internally consistent
        ok, _ = ll.verify(expected_head=self.last["hash"])
        self.assertFalse(ok)                                                          # but the head kept elsewhere no longer appears

    def test_replicas_that_disagree_refuse_to_open_and_refuse_writes(self):
        pathlib.Path(self.mirror).write_text("", encoding="utf-8")
        ok, problems = ll.verify()
        self.assertFalse(ok)
        with self.assertRaises(Exception):
            ll.add(BASE)
        with self.assertRaises(Exception):
            ll._read()

    def test_a_torn_last_line_fails_closed(self):
        for p in (self.primary, self.mirror):
            with open(p, "ab") as fh:
                fh.write(b'{"seq": 3, "kind": "LES')
        ok, problems = ll.verify()
        self.assertFalse(ok)
        with self.assertRaises(Exception):
            ll.add(BASE)

    def test_legacy_import_refuses_a_broken_old_chain(self):
        rows, prev = [], "0" * 64
        for i in (1, 2):
            rec = {"seq": i, "recorded_at": "2026-10-01T00:00:00Z", **BASE, "prev_hash": prev}
            h = hashlib.sha256((prev + json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode()).hexdigest()
            rec["hash"], rec["id"] = h, h[:12]
            rows.append(rec)
            prev = h
        rows[0]["root_cause"] = "edited after the fact"
        p = pathlib.Path(self.d.name) / "legacy.jsonl"
        p.write_text("\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows) + "\n", encoding="utf-8")
        before = len(ll._read())
        with self.assertRaises(ll.LessonRefused):
            ll.import_legacy(str(p))
        self.assertEqual(len(ll._read()), before)


class Concurrency(unittest.TestCase):
    def test_eight_writers_at_once_all_land_and_the_chain_stays_valid(self):
        d = fresh_ledger()
        try:
            errors = []

            def writer(n):
                for i in range(4):
                    try:
                        ll.add({**BASE, "title": f"w{n}-{i}"})
                    except Exception as exc:
                        errors.append(repr(exc))
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            rows = ll._read()
            self.assertEqual(len(rows), 32)
            self.assertEqual(sorted(r["seq"] for r in rows), list(range(1, 33)))
            self.assertEqual(ll.verify(), (True, None))
        finally:
            d.cleanup()


class HostileQueries(unittest.TestCase):
    def setUp(self):
        self.d = fresh_ledger()
        ll.add({**BASE, "tags": ["overwrite", "ledger"], "title": "overwrote a ledger", "protect": "back it up"})

    def tearDown(self):
        self.d.cleanup()

    def test_odd_recall_and_preflight_arguments_do_not_crash(self):
        self.assertEqual(ll.recall("overwrite", n=-3), [])
        self.assertEqual(ll.recall("overwrite", n=0), [])
        self.assertEqual(ll.recall("", n=3), [])
        self.assertTrue(ll.recall("x" * 5_000_000 + " overwrite", n=1) is not None)     # huge query is cut before tokenising
        self.assertTrue(ll.preflight("overwrite"))                                       # a bare string is one verb, not letters
        self.assertTrue(ll.preflight(["OVERWRITE"]))
        self.assertEqual(ll.preflight([]), [])
        self.assertEqual(ll.preflight([5]), [])


if __name__ == "__main__":
    unittest.main()
