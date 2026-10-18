"""Shared fixtures for the librarian tests: a fresh ledger in a temp folder and a deterministic stand-in embedder."""
import hashlib
import os
import re
import tempfile

from vega_librarian import lesson_ledger as ll

BASE = dict(title="t", what_happened="w", root_cause="rc", correction="c", prevention="p", tags=["a", "b"], severity=3,
            status="mitigated", recovery_state="rs")
SYN = {"login": "credential", "changes": "flips", "changing": "flips", "flipped": "flips", "saved": "stored", "value": "credential",
       "values": "credential", "password": "credential", "twice": "duplicate", "copies": "duplicate", "mysteriously": "flips", "keeps": "flips"}
IGNORE = {"someone", "the", "a", "an", "of", "with", "same", "between", "two", "was", "name", "what", "is"}


def fake_embed(text):
    v = [0.0] * 64
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in IGNORE:
            continue
        w = SYN.get(w, w)
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    return v


def fresh_ledger(mirrors=0):
    d = tempfile.TemporaryDirectory()
    paths = [os.path.join(d.name, "m%d.jsonl" % i) for i in range(mirrors)]
    ll.configure(os.path.join(d.name, "L.jsonl"), paths)
    return d
