"""decision_trace.py - a decision-trace graph (context graph) over a lesson ledger and, optionally, a DAG of recorded events.

Nodes (each a DNA-style card: full text kept, never truncated, with a dna_hash):
  lesson     one graded lesson from lesson_ledger
  dag_entry  one recorded event a lesson names in related_dag (only when a ledger file of events is supplied)
  control    a prevention control that was put in place (derived from the lesson's prevention text by CONTROLS patterns)
  context    the state snapshot at decision time (recovery_state + evidence) for a lesson
  exception / escalation   schema only; status 'not_populated' until a record exists (unbacked is not false)
Relations: CAUSED, INFLUENCED, PRECEDENT_FOR, ABOUT, APPLIED_POLICY, SNAPSHOT_OF, GRANTED_EXCEPTION.
Every edge says HOW it is known: basis = declared (a field in the record) or derived (tag overlap / keyword), so nothing derived is mistaken for a recorded fact.

  build(out_dir, dag_path=None, controls=None)    write nodes.jsonl and edges.jsonl (atomically: a half-written graph is never left behind)
  why(out_dir, node_id)                           walk upstream: what led to / explains this node
  precedents(out_dir, text)                       lessons most like a situation, plus what each one points at

The CONTROLS table maps wording in a lesson's prevention text to a named control. The default table is a small neutral example; pass your own."""
import hashlib
import json
import os
import re
import sys
import tempfile

from vega_librarian import lesson_ledger as ll

SCHEMA = "vega.decision_trace.v0"
UPSTREAM = {"CAUSED", "INFLUENCED", "PRECEDENT_FOR"}
MAX_HOPS_LIMIT = 20
MIN_SUFFIX = 6

DEFAULT_CONTROLS = [
    (r"append-only", "append-only-writer", "Writers may only append or create new; no overwrite verb on records."),
    (r"preflight", "lesson-preflight", "Check graded lessons before a risky verb."),
    (r"real (entry|launch|reader)|isolated mode|real path|real entry", "test-the-real-path", "Tests run the real entry point in the mode it runs, not only stubs."),
    (r"never cut|pointer plus hash|end to end", "no-truncation-check", "Never cut stored values; store whole or by pointer plus hash."),
    (r"batch|retry", "small-batches-with-retry", "Small batches, retry with backoff."),
]


class TraceRefused(ValueError):
    pass


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def card(base):
    base = {"schema": SCHEMA, **base}
    base["dna_hash"] = hashlib.sha256(canon(base).encode()).hexdigest()
    return base


def _dag_rows(path):
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate((l for l in fh if l.strip()), 1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise TraceRefused(f"event ledger line {i} is not JSON: refusing to build on a damaged ledger") from exc
            if isinstance(row, dict):
                out[i] = row
    return out


def _atomic_write(path, rows):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(canon(r) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # reviewed: the temp file may already be gone; the original error is re-raised below
        raise


def build(out_dir, dag_path=None, controls=None):
    controls = DEFAULT_CONTROLS if controls is None else controls
    compiled = []
    for rx, slug, desc in controls:
        try:
            compiled.append((re.compile(rx), rx, slug, desc))
        except re.error as exc:
            raise TraceRefused(f"control pattern {rx!r} is not a valid regex") from exc
    lessons = ll._read()
    dag = _dag_rows(dag_path)
    by_hash = {r["hash"]: r for r in lessons}
    nodes, edges = {}, []

    def add_node(n):
        nodes[n["id"]] = n

    def add_edge(src, rel, dst, basis, why=""):
        edges.append({"src": src, "rel": rel, "dst": dst, "basis": basis, "why": why})

    for r in lessons:
        lid = "lesson:" + r["id"]
        add_node(card({"id": lid, "kind": "lesson", "label": r["title"], "recorded_at": r["recorded_at"],
                       "severity": r["severity"], "status": r["status"], "tags": r["tags"],
                       "what_happened": r["what_happened"], "root_cause": r["root_cause"], "correction": r["correction"],
                       "prevention": r["prevention"], "protect": r.get("protect"), "expect": r.get("expect"),
                       "needs": r.get("needs"), "missing": r.get("missing"), "assemble": r.get("assemble"),
                       "wrong_statements": r.get("wrong_statements", []), "ledger_hash": r["hash"]}))
        cid = "context:" + r["id"]
        add_node(card({"id": cid, "kind": "context", "label": "state at decision time: " + r["title"],
                       "recovery_state": r["recovery_state"], "circumstances": r.get("circumstances"),
                       "detection": r.get("detection"), "verification": r.get("verification")}))
        add_edge(cid, "SNAPSHOT_OF", lid, "declared")
        for n in r.get("related_dag", []) or []:
            if n in dag:
                did = "dag:%d" % n
                if did not in nodes:
                    row = dag[n]
                    add_node(card({"id": did, "kind": "dag_entry", "label": str(row.get("kind", "?")), "seq": n,
                                   "recorded_at": row.get("recorded_at"), "entry_hash": row.get("entry_hash"),
                                   "what": row.get("what"), "cause": row.get("cause")}))
                add_edge(lid, "ABOUT", did, "declared", "related_dag in the lesson record")
        text = (r["prevention"] + " " + (r.get("verification") or "")).lower()
        for rx_c, rx, slug, desc in compiled:
            if rx_c.search(text):
                kid = "control:" + slug
                if kid not in nodes:
                    add_node(card({"id": kid, "kind": "control", "label": slug, "description": desc}))
                add_edge(lid, "APPLIED_POLICY", kid, "derived_keyword", "matched /%s/ in prevention/verification" % rx)
        if r.get("parent") in by_hash:
            add_edge("lesson:" + by_hash[r["parent"]]["id"], "INFLUENCED", lid, "declared", "parent field")
    for i, a in enumerate(lessons):
        for b in lessons[i + 1:]:
            shared = sorted(set(a["tags"]) & set(b["tags"]))
            if len(shared) >= 2:
                add_edge("lesson:" + a["id"], "PRECEDENT_FOR", "lesson:" + b["id"], "derived_tag_overlap", "shared tags: " + ", ".join(shared))
    for kind in ("exception", "escalation"):
        add_node(card({"id": "schema:" + kind, "kind": kind, "label": kind + " (schema only)", "status": "not_populated"}))
    os.makedirs(out_dir, exist_ok=True)
    _atomic_write(os.path.join(out_dir, "nodes.jsonl"), nodes.values())
    _atomic_write(os.path.join(out_dir, "edges.jsonl"), edges)
    return nodes, edges


def _load(out_dir):
    nodes, edges = {}, []
    try:
        with open(os.path.join(out_dir, "nodes.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n = json.loads(line)
                    nodes[n["id"]] = n
        with open(os.path.join(out_dir, "edges.jsonl"), encoding="utf-8") as fh:
            edges = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TraceRefused(f"decision trace in {out_dir} is missing or damaged: run build() again ({type(exc).__name__})") from exc
    for n in nodes.values():
        body = {k: v for k, v in n.items() if k != "dna_hash"}
        if hashlib.sha256(canon(body).encode()).hexdigest() != n.get("dna_hash"):
            raise TraceRefused(f"node {n.get('id')!r} does not match its dna_hash: the graph was edited after build")
    return nodes, edges


def _resolve(nodes, node_id):
    if node_id in nodes:
        return node_id
    if not isinstance(node_id, str) or len(node_id) < MIN_SUFFIX:
        raise KeyError("unknown node: give the full id or at least %d characters of it" % MIN_SUFFIX)
    cand = [k for k in nodes if k.endswith(node_id)]
    if len(cand) > 1:                      # a bare id also matches that lesson's context snapshot: prefer the lesson
        cand = [k for k in cand if k.startswith("lesson:")] or cand
    if len(cand) != 1:
        raise KeyError("unknown or ambiguous node: %s" % node_id)
    return cand[0]


def why(out_dir, node_id, max_hops=5):
    """Upstream explanation of a node. Returns [(hop, node, via, basis)]."""
    nodes, edges = _load(out_dir)
    node_id = _resolve(nodes, node_id)
    max_hops = max(0, min(int(max_hops), MAX_HOPS_LIMIT))
    out, seen, frontier = [(0, nodes[node_id], "start", "")], {node_id}, [node_id]
    for hop in range(1, max_hops + 1):
        nxt = []
        for cur in frontier:
            for e in edges:
                if e["dst"] == cur and e["rel"] in UPSTREAM and e["src"] not in seen and e["src"] in nodes:
                    seen.add(e["src"])
                    nxt.append(e["src"])
                    out.append((hop, nodes[e["src"]], e["rel"], e["basis"] + (": " + e["why"] if e["why"] else "")))
        frontier = nxt
        if not frontier:
            break
    for hop, n, _via, _basis in list(out):
        for e in edges:
            if e["src"] == n["id"] and e["rel"] in ("ABOUT", "APPLIED_POLICY") and e["dst"] in nodes:
                out.append((hop, nodes[e["dst"]], e["rel"] + " (from " + n["id"] + ")", e["basis"]))
    return out


def precedents(out_dir, text, n=4):
    nodes, edges = _load(out_dir)
    res = []
    for score, r, _g in ll.recall(text, n):
        lid = "lesson:" + r["id"]
        points = [(e["rel"], e["dst"], e["basis"]) for e in edges if e["src"] == lid and e["rel"] in ("ABOUT", "APPLIED_POLICY", "PRECEDENT_FOR")]
        res.append((score, nodes.get(lid), points))
    return res


def _cli(a):
    if len(a) < 2:
        print(__doc__)
        return 2
    out_dir, cmd = a[0], a[1]
    try:
        if cmd == "build":
            nodes, edges = build(out_dir, a[2] if len(a) > 2 else None)
            kinds, rels = {}, {}
            for n in nodes.values():
                kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
            for e in edges:
                rels[e["rel"]] = rels.get(e["rel"], 0) + 1
            print("nodes:", kinds)
            print("edges:", rels)
            return 0
        if cmd == "why" and len(a) == 3:
            for hop, n, via, basis in why(out_dir, a[2]):
                print("%s%s [%s] %s  <- %s %s" % ("  " * hop, n["id"], n["kind"], n["label"], via, ("(" + basis + ")") if basis else ""))
            return 0
        if cmd == "precedents" and len(a) >= 3:
            for score, n, points in precedents(out_dir, " ".join(a[2:])):
                print("%.2f  %s  [sev %s] %s" % (score, n["id"], n["severity"], n["label"]))
                print("       protect: %s" % n.get("protect"))
                for rel, dst, basis in points[:5]:
                    print("       %s -> %s (%s)" % (rel, dst, basis))
            return 0
    except (TraceRefused, KeyError) as exc:
        print("REFUSED:", exc)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
