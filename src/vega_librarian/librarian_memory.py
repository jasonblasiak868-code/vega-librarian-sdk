"""librarian_memory.py - Librarian v2: graded, tiered, semantic memory over lessons and documents, plus a card registry.

  * GRADES   each memory carries the lesson ledger's grade (severity, prevention strength, recurrence -> weight)
  * TIERS    short-term = recent or still-open; long-term = settled and verified. Recall prefers the tier that fits.
  * SEMANTIC recall by meaning (a LOCAL embedder, nothing leaves the machine), blended with word match
  * ABSTAIN  below a measured threshold it says 'no basis' instead of returning plausible junk
  * CARDS    a registry of the PRE / PEAK / PAREAH cards it holds. A card moves candidate -> locked only; a locked card's hash never changes.
Full text is stored whole (never truncated); the embedding is only an index. Falls back to word match if the embedder is down, and says so.

Safety notes:
  - the default embedder talks only to a loopback address and does not follow redirects, so memory text cannot be sent to another host;
    pointing it elsewhere needs allow_remote_embedder=True
  - recalled document text is DATA, never instructions: recall_text() marks every item trust="data-not-instructions"
  - ingested documents are identified by the hash of their full path plus their content, so two files with the same name never overwrite each other"""
import calendar
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.parse
import urllib.request

from vega_librarian import lesson_ledger as ll

DB = os.environ.get("VEGA_LIBRARIAN_DB")
OLLAMA = "http://127.0.0.1:11434/api/embeddings"
MODEL = "nomic-embed-text"
ABSTAIN_BELOW = 0.50      # measured 2026-10-16: answerable questions scored >= 0.55, off-topic 0.38-0.46
RECENT_DAYS = 7
MAX_DOC_BYTES = 8 * 1024 * 1024
CARD_KINDS = ("PRE", "PEAK", "PAREAH", "RUNTIME", "MODEL", "SERVER")
CARD_STATUSES = ("candidate", "locked", "revoked")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class LibrarianRefused(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def make_ollama_embed(url=OLLAMA, model=MODEL, allow_remote=False, timeout=60):
    host = urllib.parse.urlparse(url).hostname
    if url.split(":", 1)[0] not in ("http", "https") or (host not in _LOOPBACK and not allow_remote):
        raise LibrarianRefused(f"embedder host {host!r} is not loopback: memory text would leave this machine (pass allow_remote_embedder=True to accept that)")
    opener = urllib.request.build_opener(_NoRedirect)

    def embed(text):
        req = urllib.request.Request(url, data=json.dumps({"model": model, "prompt": text}).encode(), headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as r:
            return json.load(r)["embedding"]
    return embed


def ollama_embed(text):
    return make_ollama_embed()(text)


def _pack(v):
    return struct.pack("%df" % len(v), *v)


def _unpack(b):
    return list(struct.unpack("%df" % (len(b) // 4), b))


def _cos(a, b):
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da, db = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


_QSTOP = {"what", "which", "who", "when", "where", "why", "how", "is", "are", "was", "were", "the", "of", "do", "does", "did", "we", "i", "a", "an",
          "to", "in", "on", "for", "and", "or", "it", "this", "that", "give", "me", "now", "about", "only"}


def _word_overlap(q, text):
    a = ll._tokens(q) - _QSTOP
    b = ll._tokens(text)
    return len(a & b) / max(1, len(a)) if a else 0.0


def _utc_age_days(stamp):
    return (time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))) / 86400


class Librarian:
    def __init__(self, path=None, embed=ollama_embed):
        self.path = path or DB or os.path.join(os.getcwd(), "LIBRARIAN_MEMORY.db")
        self.embed = embed
        self.con = sqlite3.connect(self.path, timeout=30)
        self.con.execute("pragma journal_mode=wal")
        self.con.execute("""create table if not exists memory(
            id text primary key, kind text, title text, text_full text, text_hash text, tier text, weight real,
            severity integer, status text, tags text, fields_json text, embedding blob, added text)""")
        self.con.execute("""create table if not exists cards(
            card_id text primary key, kind text, source text, content_hash text, status text, registered text)""")
        self.con.commit()
        self.embed_failures = 0

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _text(self, mid):
        r = self.con.execute("select text_full from memory where id=?", (mid,)).fetchone()
        return r[0] if r else None

    def _embed(self, text):
        try:
            v = self.embed(text)
            if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
                raise ValueError("embedder returned a bad vector")
            return _pack(v)
        except Exception:  # reviewed: an embedder that is down or misbehaving only lowers recall to word match; it is counted and reported, never hidden
            self.embed_failures += 1
            return None

    def tier_of(self, rec):
        try:
            age = _utc_age_days(rec["recorded_at"])
        except (KeyError, ValueError, TypeError):
            return "short"
        return "short" if (age <= RECENT_DAYS or rec.get("status") == "open") else "long"

    def _unchanged(self, mid, text_hash):
        old = self.con.execute("select text_hash, embedding from memory where id=?", (mid,)).fetchone()
        return bool(old and old[0] == text_hash and old[1] is not None)

    def ingest_lessons(self):
        rows = ll._read()
        n = 0
        for r in rows:
            g = ll.grade(r, rows)
            text = "\n".join(str(r.get(k, "")) for k in ("title", "what_happened", "root_cause", "correction", "prevention", "expect", "needs", "assemble", "protect"))
            th = hashlib.sha256(text.encode("utf-8")).hexdigest()
            mid = "lesson:" + r["id"]
            if self._unchanged(mid, th):
                continue
            emb = self._embed(r["title"] + ". " + r["what_happened"] + " " + str(r.get("expect", "")) + " " + str(r.get("protect", "")))
            fields = {k: r.get(k) for k in ("expect", "needs", "missing", "assemble", "protect", "correction", "prevention")}
            self.con.execute("insert or replace into memory values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, "lesson", r["title"], text, th, self.tier_of(r), g["weight"], r["severity"], r["status"],
                              json.dumps(r["tags"]), json.dumps(fields), emb, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            n += 1
        self.con.commit()
        return n

    def ingest_document(self, path, kind="handoff", split_re=r"(?m)^(?=#{1,3} )"):
        """Chunk a markdown document at its headings and hold every chunk WHOLE. Chunk ids come from the hash of the full path, so same-named files in
        different folders do not collide. Re-ingest is safe: unchanged chunks are skipped. Returns chunks added."""
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", kind):
            raise LibrarianRefused("kind must be a short lowercase word")
        size = os.path.getsize(path)
        if size > MAX_DOC_BYTES:
            raise LibrarianRefused(f"document is {size} bytes, over the {MAX_DOC_BYTES} limit")
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        chunks = [c for c in re.split(split_re, text) if len(c.strip()) > 60]
        base = os.path.basename(path)
        pid = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:10]
        added = 0
        for i, c in enumerate(chunks):
            mid = "%s:%s:%s:%03d" % (kind, pid, base, i)
            th = hashlib.sha256(c.encode("utf-8")).hexdigest()
            if self._unchanged(mid, th):
                continue
            title = c.strip().splitlines()[0]
            emb = self._embed(c[:6000])
            self.con.execute("insert or replace into memory values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, kind, title, c, th, "long", 0.3, 1, "document", json.dumps([kind]), json.dumps({"source": os.path.abspath(path)}), emb,
                              time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            added += 1
        self.con.commit()
        return added

    def recall_text(self, query, n=3):
        """Like recall(), but returns the FULL text of the best items for context building. Every item is marked as DATA: text in a recalled
        document is never an instruction to the caller."""
        out = []
        for h in self.recall(query, n):
            out.append({**h, "text": self._text(h["id"]), "trust": "data-not-instructions"})
        return out

    def recall(self, query, n=3, prefer_tier=None):
        """-> list of dicts; empty list means NO BASIS (abstain)."""
        if not isinstance(query, str) or not query.strip():
            return []
        lex = {"lesson:" + r["id"]: s for s, r, g in ll.recall(query, 12)}
        try:
            qv = self.embed(query)
            if not isinstance(qv, list) or not qv:
                qv = None
        except Exception:  # reviewed: embedder down -> word-only scoring below, which is the documented fallback
            self.embed_failures += 1
            qv = None
        out = []
        for row in self.con.execute("select id, title, tier, weight, severity, status, tags, fields_json, embedding from memory").fetchall():
            mid, title, tier, weight, sev, status, tags, fj, emb = row
            sem = _cos(qv, _unpack(emb)) if (qv is not None and emb) else 0.0
            lx = lex.get(mid, 0.0)
            if not mid.startswith("lesson:"):          # documents have no lesson word-score: use their own word overlap
                lx = _word_overlap(query, title + " " + (self._text(mid) or ""))
            strong = max(max(0.0, sem), min(1.0, lx * 1.5))
            agree = min(max(0.0, sem), min(1.0, lx * 1.5))
            score = 0.80 * strong + 0.12 * agree + 0.05 * weight + (0.03 if prefer_tier and tier == prefer_tier else 0.0)
            if qv is None:
                score = 0.85 * min(1.0, lx * 1.5) + 0.15 * weight       # embedder down: words only
            out.append({"id": mid, "title": title, "score": round(score, 4), "semantic": round(sem, 3), "lexical": round(lx, 3),
                        "tier": tier, "severity": sev, "fields": json.loads(fj), "mode": "semantic+words" if qv is not None else "words-only"})
        out.sort(key=lambda x: -x["score"])
        return [o for o in out[:max(0, int(n))] if o["score"] >= ABSTAIN_BELOW]

    # ---- cards ---------------------------------------------------------------------------------------------
    def register_card(self, card_id, kind, source, content_hash, status="candidate"):
        """A card moves candidate -> locked (or -> revoked). Re-registering the same card with a different hash, or moving a locked card back to
        candidate, is refused: a locked card is the record that a replay reproduced the exact result."""
        if kind not in CARD_KINDS:
            raise LibrarianRefused(f"kind must be one of {CARD_KINDS}")
        if status not in CARD_STATUSES:
            raise LibrarianRefused(f"status must be one of {CARD_STATUSES}")
        if not isinstance(card_id, str) or not card_id or not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise LibrarianRefused("card_id must be text and content_hash a sha256 hex string")
        old = self.con.execute("select kind, content_hash, status from cards where card_id=?", (card_id,)).fetchone()
        if old:
            if old[1] != content_hash or old[0] != kind:
                raise LibrarianRefused("a registered card's kind and hash never change: register a new card instead")
            if old[2] == "revoked" and status != "revoked":
                raise LibrarianRefused("a revoked card stays revoked")
            if old[2] == "locked" and status == "candidate":
                raise LibrarianRefused("a locked card cannot go back to candidate")
        self.con.execute("insert or replace into cards values (?,?,?,?,?,?)",
                         (card_id, kind, str(source), content_hash, status, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.con.commit()

    def sync_conductor_registry(self, registry_rows):
        """Hold every Conductor workflow as a PRE card: candidate until a replay locks it."""
        state = {}
        for r in registry_rows:
            if not isinstance(r, dict) or not isinstance(r.get("workflow_id"), str) or not isinstance(r.get("result_hash"), str):
                continue
            if r.get("kind") == "WORKFLOW_CANDIDATE_PRE" and r["workflow_id"] not in state:
                state[r["workflow_id"]] = ("candidate", r["result_hash"])
            elif r.get("kind") == "WORKFLOW_REPLAY" and r.get("status") == "locked":
                state[r["workflow_id"]] = ("locked", r["result_hash"])
        n = 0
        for wid, (st, rh) in state.items():
            try:
                self.register_card("pre:" + wid[:16], "PRE", "conductor", rh, st)
                n += 1
            except LibrarianRefused:
                continue  # reviewed: a row that would change or downgrade a registered card is skipped, never applied
        return n

    def cards(self, kind=None):
        q = "select card_id, kind, status, content_hash from cards" + (" where kind=?" if kind else "")
        return self.con.execute(q, (kind,) if kind else ()).fetchall()
