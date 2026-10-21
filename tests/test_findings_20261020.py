"""Regression test (2026-10-20): register_card read the old row and then wrote the new one as two steps, so two writers could register one card id with two different hashes,
though 'a registered card's hash never changes'. The check and the write are now one transaction."""
import os
import tempfile
import threading
import time
import unittest

from vega_librarian import librarian_memory as lm

H1, H2 = "a" * 64, "b" * 64


class SlowRead:
    """Wraps a connection so the read of the old card takes a moment: widens the window between the read and the write."""

    def __init__(self, con):
        self._con = con

    def execute(self, sql, *a):
        out = self._con.execute(sql, *a)
        if sql.lstrip().lower().startswith("select kind, content_hash, status from cards"):
            time.sleep(0.1)
        return out

    def __getattr__(self, k):
        return getattr(self._con, k)


class CardRegistryIsAtomic(unittest.TestCase):
    def test_two_writers_cannot_register_one_card_with_two_hashes(self):
        for n in range(4):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "mem.db")
                lm.Librarian(path, embed=lambda t: [1.0, 0.0]).close()          # create the tables once
                gate, out, lock = threading.Barrier(2), [], threading.Lock()

                def go(h):
                    lib = lm.Librarian(path, embed=lambda t: [1.0, 0.0])           # a connection belongs to the thread that made it
                    lib.con = SlowRead(lib.con)
                    gate.wait()
                    try:
                        lib.register_card("pre:card-1", "PRE", "conductor", h)
                        r = "registered"
                    except Exception as exc:
                        r = "refused" if isinstance(exc, lm.LibrarianRefused) else "error:%s:%s" % (type(exc).__name__, exc)
                    finally:
                        lib.close()
                    with lock:
                        out.append(r)
                ts = [threading.Thread(target=go, args=(h,)) for h in (H1, H2)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(60)
                self.assertEqual(sorted(out), ["refused", "registered"], out)
                chk = lm.Librarian(path, embed=lambda t: [1.0, 0.0])
                self.assertEqual(len(chk.cards()), 1)
                chk.close()


if __name__ == "__main__":
    unittest.main()
