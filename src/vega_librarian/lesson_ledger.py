"""lesson_ledger.py - every mistake becomes a graded, retrievable, chain-sealed LESSON (the seed of the Librarian's memory).

A lesson keeps the whole event so a harness can learn from it: what happened, the circumstances that let it propagate, the root cause,
which of OUR statements were wrong, how it was detected, the correction, the recovery state, the prevention that was added and the proof
that the prevention works. Plus five fields the Librarian uses to answer "what do I expect, need, miss, assemble and protect when this
situation comes up again": expect, needs, missing, assemble, protect.

Storage is a vega_custody Chain, so every safety property of that chain applies here and is not re-implemented:
  - append-only, hash-chained, canonical JSON (NaN refused), written to every replica (mirror) and read back before the call returns
  - a lesson that carries a credential-shaped value or a secret-named field is REFUSED (the old "never a secret" rule was only a comment)
  - cross-process file lock; a writer that finds the file changed underneath it refuses instead of forking the chain
  - tail truncation is detectable when the caller keeps the head hash elsewhere: verify(expected_head=..., min_length=...)
Nothing is edited in place: a changed status or a recurrence is a NEW lesson pointing at its parent.

  python -m vega_librarian.lesson_ledger add LESSON.json | list | recall "text" | preflight VERB... | verify | import-legacy OLD.jsonl
Configure with VEGA_LESSON_LEDGER (path) and VEGA_LESSON_MIRRORS (extra replica paths, separated by the OS path separator)."""
import datetime
import json
import os
import re
import sys
import time

from vega_custody.dag import dag as dagmod

REQUIRED = ("title", "what_happened", "root_cause", "correction", "prevention", "tags", "severity", "status", "recovery_state")
OPTIONAL = ("circumstances", "wrong_statements", "detection", "verification", "related_dag", "parent", "occurred_at",
            "expect", "needs", "missing", "assemble", "protect")
TEXT_FIELDS = ("title", "what_happened", "root_cause", "correction", "prevention", "recovery_state",
               "circumstances", "detection", "verification", "expect", "needs", "missing", "assemble", "protect")
STATUSES = ("open", "mitigated", "closed")
KIND = "LESSON"
STOP = set("the a an of to in on and or for with is was were be by it this that from at as are not no".split())
MAX_QUERY_CHARS = 20_000

_state = {"path": None, "mirrors": None}


class LessonRefused(ValueError):
    pass


def configure(path=None, mirrors=None):
    """Point the module-level functions at a ledger. mirrors: extra replica paths (written and read back with the primary)."""
    _state["path"], _state["mirrors"] = (str(path) if path else None), ([str(m) for m in mirrors] if mirrors is not None else None)


def _paths():
    primary = _state["path"] or os.environ.get("VEGA_LESSON_LEDGER") or os.path.join(os.getcwd(), "LESSON_LEDGER.jsonl")
    mirrors = _state["mirrors"]
    if mirrors is None:
        env = os.environ.get("VEGA_LESSON_MIRRORS", "")
        mirrors = [m for m in env.split(os.pathsep) if m]
    return [primary] + list(mirrors)


def _chain():
    return dagmod.Chain(_paths())


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _view(entry):
    """A chain entry as a lesson record: the payload plus its position and hashes."""
    r = dict(entry["payload"])
    r.update(seq=entry["seq"] + 1, hash=entry["entry_hash"], id=entry["entry_hash"][:12],
             prev_hash=entry["parents"][0] if entry["parents"] else "0" * 64)
    return r


def _read():
    return [_view(e) for e in _chain().entries() if e["kind"] == KIND]


def _check(lesson, existing_hashes):
    if not isinstance(lesson, dict):
        raise LessonRefused("a lesson is an object")
    missing = [k for k in REQUIRED if k not in lesson or lesson[k] in (None, "", [])]
    if missing:
        raise LessonRefused("lesson is missing: " + ", ".join(missing))
    unknown = [k for k in lesson if k not in REQUIRED + OPTIONAL]
    if unknown:
        raise LessonRefused("unknown fields: %s" % unknown)
    for k in TEXT_FIELDS:
        if k in lesson and not isinstance(lesson[k], str):
            raise LessonRefused(f"{k} must be text")
    if lesson["status"] not in STATUSES:
        raise LessonRefused("status must be one of %s" % (STATUSES,))
    sev = lesson["severity"]
    if isinstance(sev, bool) or not isinstance(sev, int) or not 1 <= sev <= 5:
        raise LessonRefused("severity is a whole number, 1 (minor) to 5 (critical)")
    tags = lesson["tags"]
    if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise LessonRefused("tags is a non-empty list of non-blank strings")
    for k in ("wrong_statements",):
        if k in lesson and (not isinstance(lesson[k], list) or any(not isinstance(x, str) for x in lesson[k])):
            raise LessonRefused(f"{k} must be a list of text")
    if "related_dag" in lesson and (not isinstance(lesson["related_dag"], list) or any(isinstance(x, bool) or not isinstance(x, int) for x in lesson["related_dag"])):
        raise LessonRefused("related_dag must be a list of whole numbers")
    if "occurred_at" in lesson:
        try:
            datetime.datetime.fromisoformat(str(lesson["occurred_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise LessonRefused("occurred_at must be an ISO date-time") from exc
    if lesson.get("parent") is not None and lesson["parent"] not in existing_hashes:
        raise LessonRefused("parent must be the hash of an existing lesson")


def add(lesson, _recorded_at=None, attempts=8):
    """Record one lesson. Two writers at once are safe: the chain refuses a writer whose view is stale, and this retries on a fresh view."""
    payload = None
    for attempt in range(max(1, attempts)):
        chain = _chain()
        _check(lesson, {e["entry_hash"] for e in chain.entries() if e["kind"] == KIND})
        payload = payload or {**lesson, "recorded_at": _recorded_at or _now()}
        try:
            return _view(chain.record(KIND, payload))
        except dagmod.RecordRefused as exc:
            if "changed underneath" not in str(exc) or attempt == attempts - 1:
                raise LessonRefused(f"lesson refused by the chain: {exc}") from exc
            time.sleep(0.02 * (attempt + 1))
        except dagmod.DagError as exc:
            raise LessonRefused(f"lesson refused by the chain: {exc}") from exc


def verify(expected_head=None, min_length=None):
    """(True, None) or (False, problems). Pass expected_head / min_length remembered ELSEWHERE to detect a cut-off tail."""
    try:
        ok, problems = _chain().verify(expected_head=expected_head, min_length=min_length)
    except dagmod.DagError as exc:
        return False, [str(exc)]
    return (True, None) if ok else (False, problems)


def import_legacy(path):
    """Bring in a ledger written by the old flat-file version. Its own hash chain is checked first; each lesson is re-recorded in order."""
    import hashlib
    rows, prev = [], "0" * 64
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate((l for l in fh if l.strip()), 1):
            r = json.loads(line)
            body = {k: v for k, v in r.items() if k not in ("hash", "id")}
            canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if r.get("prev_hash") != prev or r.get("seq") != i or hashlib.sha256((prev + canon).encode()).hexdigest() != r.get("hash"):
                raise LessonRefused(f"legacy ledger chain broken at record {i}; refusing to import")
            prev = r["hash"]
            rows.append(r)
    old_to_new, out = {}, []
    for r in rows:
        lesson = {k: v for k, v in r.items() if k in REQUIRED + OPTIONAL}
        if lesson.get("parent") in old_to_new:
            lesson["parent"] = old_to_new[lesson["parent"]]
        elif lesson.get("parent"):
            lesson.pop("parent")
        new = add(lesson, _recorded_at=r["recorded_at"])
        old_to_new[r["hash"]] = new["hash"]
        out.append(new)
    return out


def _stem(t):
    """Crude suffix folding so 'secret' matches 'secrets' and 'pushing' matches 'push'. Same fold on both sides."""
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _tokens(text):
    return {_stem(t) for t in re.findall(r"[a-z0-9]+", str(text)[:MAX_QUERY_CHARS].lower()) if len(t) > 2 and t not in STOP}


def grade(r, all_rows):
    """prevention_strength: 1.0 = a control exists AND a named verification; 0.6 = control only; 0.3 = a rule only.
    recurrence = later lessons that share two or more tags. weight = severity, prevention gap, recurrence."""
    verified = bool(r.get("verification"))
    strength = 1.0 if verified else 0.6 if r["status"] in ("mitigated", "closed") else 0.3
    tags = set(r["tags"])
    recurrence = sum(1 for o in all_rows if o["seq"] > r["seq"] and len(tags & set(o["tags"])) >= 2)
    weight = round((r["severity"] / 5) * 0.5 + (1 - strength) * 0.3 + min(recurrence, 3) / 3 * 0.2, 3)
    return {"prevention_strength": strength, "recurrence": recurrence, "weight": weight}


def recall(query, n=5):
    rows = _read()
    q = _tokens(query)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for r in rows:
        text = " ".join(str(r.get(k, "")) for k in ("title", "what_happened", "root_cause", "prevention", "circumstances"))
        tag_hit = len(q & {_stem(t.lower()) for t in r["tags"]}) / max(1, len(r["tags"]))
        txt_hit = len(q & _tokens(text)) / max(1, len(q))
        g = grade(r, rows)
        recency = 1.0 / (1 + max(0, (now - datetime.datetime.fromisoformat(r["recorded_at"].replace("Z", "+00:00"))).days) / 30)
        score = round(0.45 * tag_hit + 0.30 * txt_hit + 0.15 * g["weight"] + 0.10 * recency, 4)
        if tag_hit or txt_hit:
            out.append((score, r, g))
    out.sort(key=lambda x: -x[0])
    return out[:max(0, int(n))]


PREFLIGHT_MAP = {"overwrite": ["overwrite", "ledger", "append-only"], "write": ["overwrite", "ledger"], "delete": ["delete", "overwrite"],
                 "rotate": ["rotation", "credentials"], "restart": ["restart", "service", "watchdog"], "stop": ["restart", "service", "watchdog"],
                 "push": ["git", "secrets", "commit"], "commit": ["git", "secrets", "commit"], "publish": ["git", "secrets", "publish"],
                 "scan": ["scan", "performance"], "instruct": ["unverified-claim", "ui"], "test": ["testing", "stub"]}


def preflight(verbs):
    if isinstance(verbs, str):
        verbs = [verbs]
    want = set()
    for v in verbs:
        want |= set(PREFLIGHT_MAP.get(str(v).lower(), [str(v).lower()]))
    rows = _read()
    hits = [(grade(r, rows)["weight"], r) for r in rows if want & {t.lower() for t in r["tags"]}]
    hits.sort(key=lambda x: -x[0])
    return hits


def status():
    rows = _read()
    return [{"id": r["id"], "seq": r["seq"], "title": r["title"], "severity": r["severity"], "status": r["status"], **grade(r, rows)} for r in rows]


def seal(dag):
    """Write the ledger's current state into a vega_custody Dag as a LESSON_LEDGER_STATUS entry. Returns that entry."""
    ok, problems = verify()
    rows = _read()
    payload = {"what": "Graded lessons from mistakes: what happened, why, the correction, the prevention and its proof.",
               "chain_ok": ok, "problems": problems or [], "lessons": len(rows), "last_hash": rows[-1]["hash"] if rows else "", "status": status()}
    return dag.record("LESSON_LEDGER_STATUS", payload)


def _fmt(r):
    return "%s #%d [sev %d, %s] %s" % (r["id"], r["seq"], r["severity"], r["status"], r["title"])


def _cli(a):
    if not a:
        print(__doc__)
        return 2
    c = a[0]
    try:
        if c == "add" and len(a) == 2:
            with open(a[1], encoding="utf-8") as fh:
                print("recorded", add(json.load(fh))["id"])
            return 0
        if c == "import-legacy" and len(a) == 2:
            print("imported", len(import_legacy(a[1])), "lessons")
            return 0
    except LessonRefused as exc:
        print("REFUSED:", exc)
        return 1
    if c == "list":
        for s in status():
            print("%s #%d sev%d %-9s strength %.1f recur %d weight %.2f | %s" % (s["id"], s["seq"], s["severity"], s["status"], s["prevention_strength"], s["recurrence"], s["weight"], s["title"]))
        return 0
    if c == "recall" and len(a) >= 2:
        for score, r, _g in recall(" ".join(a[1:])):
            print("%.2f  %s" % (score, _fmt(r)))
            for k in ("expect", "needs", "missing", "assemble", "protect"):
                if r.get(k):
                    print("      %-8s %s" % (k.upper(), r[k]))
        return 0
    if c == "preflight" and len(a) >= 2:
        hits = preflight(a[1:])
        print("PREFLIGHT for:", ", ".join(a[1:]), "|", len(hits), "relevant lesson(s)")
        for _w, r in hits:
            print("  - %s" % _fmt(r))
            if r.get("protect"):
                print("      PROTECT: %s" % r["protect"])
        return 0
    if c == "verify":
        ok, problems = verify()
        print("chain valid" if ok else "CHAIN BROKEN: %s" % problems)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
