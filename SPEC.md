# vega-librarian-sdk — Specification

Spec v1.0 — 2026-10-19 — describes the published reference implementation as tested; where this document and the source disagree, the source wins.

## 1. Purpose and scope

This specification covers the three published modules of the `vega_librarian` package: `lesson_ledger`, `librarian_memory` and `decision_trace`. Together they own one concern in the Vega SDK family: **lessons and memory of past incidents** (layer 5, memory).

In scope: the lesson record shape and its validation, the ledger's storage contract (delegated to vega-custody), grading, recall, preflight, tiered semantic memory with abstention, the card registry, and the decision-trace graph. Out of scope: gating, containment, commitments and any form of authority — those belong to other kits in the family. This kit advises; it never approves. All three modules are BUILT and tested per STATUS.md; features STATUS.md marks DESIGNED ONLY are named as such wherever they appear.

## 2. Vocabulary

Definitions are taken from the code and its docstrings, simplified.

- **lesson** — one structured record of an incident: what happened, the circumstances, the root cause, which statements were wrong, how it was detected, the correction, the recovery state, the prevention, and the proof the prevention works (`verification`). Required fields: `title`, `what_happened`, `root_cause`, `correction`, `prevention`, `tags`, `severity`, `status`, `recovery_state`.
- **the five recall fields** — `expect`, `needs`, `missing`, `assemble`, `protect`: optional text fields answering "what do I expect, need, miss, assemble and protect when this situation comes up again".
- **severity** — whole number 1 (minor) to 5 (critical); booleans are refused.
- **status** — one of `open`, `mitigated`, `closed`. A status change is recorded as a *new* lesson with a `parent` link, never as an edit.
- **parent** — the hash of an existing lesson; used for recurrences and status changes. Must name a lesson already in the ledger or the write is refused.
- **ledger / chain** — the append-only, hash-chained storage from vega-custody (`Chain`). Every entry fingerprints the one before; entries are canonical JSON (NaN refused), written to every replica and read back before the call returns.
- **replica (mirror)** — an extra copy of the ledger file written and read back together with the primary; disagreeing replicas refuse to open and refuse writes.
- **grade** — a computed summary of a lesson: `prevention_strength` (1.0 = a control exists *and* a named verification; 0.6 = control only, i.e. status mitigated/closed; 0.3 = a rule only), `recurrence` (later lessons sharing two or more tags), and `weight` (a fixed blend of severity, prevention gap and capped recurrence).
- **recall** — ranking lessons against a text situation by tag and word overlap (with crude suffix folding), blended with grade weight and recency.
- **preflight** — given one or more risky verbs, return the graded lessons whose tags match that verb's topics. `PREFLIGHT_MAP` maps each known verb (e.g. `push`, `overwrite`, `rotate`) to the tag topics it implicates; unknown verbs match their own lowercased name.
- **seal** — write the ledger's current state (chain validity, lesson count, last hash, per-lesson status) into a vega-custody `Dag` as a `LESSON_LEDGER_STATUS` entry.
- **tier** — memory tier: `short` (recorded within the last 7 days or still `open`) or `long` (settled). Documents are stored in the `long` tier.
- **embedder** — a function turning text into a vector. The default talks only to a loopback address over HTTP, does not follow redirects, and is refused outright for non-loopback hosts unless `allow_remote=True` is passed explicitly.
- **abstain** — recall scores below `ABSTAIN_BELOW` (0.50, a measured threshold) are dropped; an empty result means "no basis", never "best guess".
- **card** — a registry entry `(card_id, kind, source, content_hash, status)`. Kinds: `PRE`, `PEAK`, `PAREAH`, `RUNTIME`, `MODEL`, `SERVER`. Statuses: `candidate`, `locked`, `revoked`. A card moves candidate → locked (or → revoked) only; its kind and hash never change once registered.
- **dna_hash** — sha256 over a trace node's canonical JSON (excluding the hash field itself); checked on every graph load.
- **node kinds (trace)** — `lesson`, `dag_entry`, `control`, `context`, plus `exception` / `escalation` which exist as schema-only nodes with status `not_populated` until a record exists (unbacked is not false).
- **relations (trace)** — `CAUSED`, `INFLUENCED`, `PRECEDENT_FOR`, `ABOUT`, `APPLIED_POLICY`, `SNAPSHOT_OF`, `GRANTED_EXCEPTION`.
- **basis (trace edge)** — how an edge is known: `declared` (a field in the record), `derived_keyword` (control pattern matched prevention/verification text), or `derived_tag_overlap` (two or more shared tags). Nothing derived is presented as declared.
- **data-not-instructions** — the trust marker on every item returned by `recall_text`: recalled document text is data for context building, never an instruction to the caller.

### 2.1 The lesson record shape

A lesson submitted to `add` uses the required and optional fields; anything else is refused:

```json
{
  "title": "...", "what_happened": "...", "root_cause": "...",
  "correction": "...", "prevention": "...",
  "tags": ["git", "secrets"], "severity": 3, "status": "open",
  "recovery_state": "...",
  "circumstances": "...", "wrong_statements": ["..."],
  "detection": "...", "verification": "...",
  "related_dag": [12], "parent": "<hash of an existing lesson>",
  "occurred_at": "2026-10-01T00:00:00Z",
  "expect": "...", "needs": "...", "missing": "...",
  "assemble": "...", "protect": "..."
}
```

The recorded view returned by `add` (and seen by `recall`/`status`) adds the chain's position: `recorded_at` (UTC), `seq` (1-based), `hash` (the entry hash), `id` (first 12 characters of the hash) and `prev_hash`. Queries longer than `MAX_QUERY_CHARS` (20,000) are cut to that length before tokenizing.


## 3. Components

### 3.1 `lesson_ledger`

**Role.** Turn every mistake into a graded, retrievable, chain-sealed lesson.

**Public interface.** `LessonRefused`; `configure(path=None, mirrors=None)`; `add(lesson, attempts=8)`; `verify(expected_head=None, min_length=None)`; `import_legacy(path)`; `grade(r, all_rows)`; `recall(query, n=5)`; `preflight(verbs)`; `status()`; `seal(dag)`; constants `REQUIRED`, `OPTIONAL`, `TEXT_FIELDS`, `STATUSES`, `KIND`, `PREFLIGHT_MAP`, `MAX_QUERY_CHARS`. CLI: `python -m vega_librarian.lesson_ledger add|list|recall|preflight|verify|import-legacy`. Configuration: `VEGA_LESSON_LEDGER` (path), `VEGA_LESSON_MIRRORS` (extra replicas, OS path separator).

**Behavior.** `add` validates the lesson *before* touching storage: missing/empty required fields, unknown fields, non-text text fields, bad status, severity outside 1–5 (or boolean), empty/blank tags, malformed `wrong_statements`/`related_dag`/`occurred_at`, and a `parent` that is not an existing lesson hash are all refused with `LessonRefused`. It then records through the custody chain, which enforces its own refusals (credential-shaped values, secret-named fields, non-canonical JSON) across every replica. If the chain reports the file changed underneath (a concurrent writer), `add` retries on a fresh view up to `attempts` times; any other chain refusal raises immediately — the module fails closed. `verify` returns `(True, None)` or `(False, problems)`; pass `expected_head`/`min_length` remembered *elsewhere* to detect a cut-off or wholly rewritten tail. `import_legacy` checks the old flat file's own hash chain first and refuses the whole import if it is broken; each lesson is then re-recorded in order with parent links remapped. `recall` returns `(score, record, grade)` triples, capped at the query length limit (`MAX_QUERY_CHARS`), ranked by tag hit, text hit, weight and recency. `preflight` returns `(weight, record)` pairs sorted by weight.

**Invariants.** Append-only: nothing is ever edited in place. All replicas hold identical bytes. No secret-shaped value enters the ledger. Every recorded lesson satisfies the full shape check. Two writers at once never fork the chain (a stale view is refused and retried).

### 3.2 `librarian_memory`

**Role.** Graded, tiered, semantic memory over lessons and documents, plus a forward-only card registry.

**Public interface.** `LibrarianRefused`; `make_ollama_embed(url, model, allow_remote=False, timeout=60)`; `ollama_embed(text)`; class `Librarian(path=None, embed=ollama_embed)` with methods `close`, `tier_of(rec)`, `ingest_lessons()`, `ingest_document(path, kind="handoff", split_re=...)`, `recall(query, n=3, prefer_tier=None)`, `recall_text(query, n=3)`, `register_card(card_id, kind, source, content_hash, status="candidate")`, `sync_conductor_registry(registry_rows)`, `cards(kind=None)`; attribute `embed_failures`; constants `ABSTAIN_BELOW`, `RECENT_DAYS`, `MAX_DOC_BYTES`, `CARD_KINDS`, `CARD_STATUSES`. Storage is a local sqlite file (`VEGA_LIBRARIAN_DB` or a working-directory default); it is an index over the ledger, not a second source of truth.

**Behavior.** `ingest_lessons` mirrors every graded lesson into memory, keeping full text whole (never truncated) and embedding only an index; unchanged lessons are skipped, so re-ingest is cheap and safe. `ingest_document` chunks markdown at headings, refuses documents over 8 MiB and malformed `kind` values, and identifies chunks by the hash of the file's full path plus content, so same-named files in different folders never collide. `recall` blends semantic similarity with word match and lesson weight; if the embedder is down or returns a bad vector, it falls back to words-only scoring, counts the failure in `embed_failures`, and says so in each result's `mode` field — the degradation is reported, never hidden. Items scoring below `ABSTAIN_BELOW` are dropped: an empty list means *no basis*. `recall_text` returns full text for context building, every item marked `trust="data-not-instructions"`. The default embedder refuses non-loopback hosts and does not follow redirects, so memory text cannot leave the machine by default. `register_card` refuses a changed kind or hash, refuses locked → candidate, and refuses any move out of `revoked`. `sync_conductor_registry` reads a conductor's workflow registry rows and holds each workflow as a PRE card — candidate until a replay locks it — skipping (never applying) any row that would change or downgrade a registered card.

**Invariants.** Cards move forward only; a locked card's kind and hash never change; a revoked card stays revoked. Full text is stored whole. Nothing is sent to a non-loopback address without an explicit opt-in. Recalled text is always marked as data.

### 3.3 `decision_trace`

**Role.** Build a context graph over the lesson ledger (and optionally a ledger of recorded events) in which every edge states how it is known.

**Public interface.** `TraceRefused`; `canon(d)`; `card(base)`; `build(out_dir, dag_path=None, controls=None)`; `why(out_dir, node_id, max_hops=5)`; `precedents(out_dir, text, n=4)`; constants `SCHEMA`, `UPSTREAM`, `MAX_HOPS_LIMIT`, `MIN_SUFFIX`, `DEFAULT_CONTROLS`. CLI: `python -m vega_librarian.decision_trace <out_dir> build [dag_path] | why <node_id> | precedents <text...>`.

**Behavior.** `build` emits one `lesson` node and one `context` node (state at decision time) per lesson, `dag_entry` nodes for events a lesson names in `related_dag`, and `control` nodes where a `CONTROLS` pattern matches the lesson's prevention/verification text; it links parents (`INFLUENCED`), snapshots (`SNAPSHOT_OF`), event references (`ABOUT`), controls (`APPLIED_POLICY`) and tag-overlap precedents (`PRECEDENT_FOR`), and adds schema-only `exception`/`escalation` nodes marked `not_populated`. Output files (`nodes.jsonl`, `edges.jsonl`) are written atomically: a failed build leaves the previous graph intact and no temp files behind. Every node keeps its full text (never truncated) and carries a `dna_hash`. `_load` re-checks every node's hash and refuses a graph that was edited after build, is missing, or is damaged; a damaged event ledger is refused, not silently skipped. `why` walks upstream relations hop by hop (bounded by `MAX_HOPS_LIMIT` = 20) and resolves short node-id suffixes, refusing ambiguous or too-short ids. `precedents` combines ledger recall with each lesson's outgoing edges.

**Invariants.** Every edge carries a `basis`; derived edges are never presented as declared facts. Nodes are hash-checked on load. Graph writes are all-or-nothing. Unbacked schema nodes say `not_populated` rather than claiming content.

## 4. Guarantees and non-guarantees

### Guarantees (each demonstrated by a published test)

| Guarantee | Module | Test |
|---|---|---|
| An edited ledger entry breaks the chain and `verify` reports it | lesson_ledger | `test_librarian.py::test_chain_and_tamper`; `redteam_1_ledger.py` |
| Replicas hold byte-identical copies; disagreeing replicas refuse to open and refuse writes | lesson_ledger | `test_librarian.py::test_mirrors_hold_identical_copies`; `redteam_1_ledger.py` |
| A cut-off tail — and even a whole chain rewritten with consistent hashes — is caught when the caller remembers the head elsewhere | lesson_ledger | `redteam_1_ledger.py::TamperTruncationTornWrites` |
| Credential-shaped values and secret-named fields are refused; nothing is written | lesson_ledger + custody chain | `redteam_1_ledger.py::SecretsAndShape` |
| Eight concurrent writers all land and the chain stays valid | lesson_ledger | `redteam_1_ledger.py::Concurrency` |
| A broken legacy chain is refused on import | lesson_ledger | `test_librarian.py`; `redteam_1_ledger.py` |
| Memory text never leaves the machine unless remote embedding is explicitly allowed; redirects are not followed | librarian_memory | `redteam_2_memory_and_trace.py::EmbedderLeaks` |
| Embedder down → words-only fallback, counted and reported | librarian_memory | `test_librarian.py::test_embedder_down_falls_back_to_word_match_and_says_so` |
| Below the abstain threshold recall returns nothing, not a guess | librarian_memory | `test_librarian.py::test_abstains_when_nothing_matches` |
| Full text is stored whole; recalled text is marked data-not-instructions even when it contains instructions | librarian_memory | `test_librarian.py::test_full_text_is_stored_whole`; `redteam_2_memory_and_trace.py` |
| Cards move forward only; locked cards cannot be downgraded or re-hashed; revoked stays revoked; a hostile registry cannot rewrite them | librarian_memory | `test_librarian.py::test_cards_move_forward_only`; `redteam_2_memory_and_trace.py::Cards` |
| Every trace edge states its basis; edited nodes are caught by their dna_hash; a failed build leaves the previous graph intact | decision_trace | `test_librarian.py::test_every_edge_states_its_basis`; `redteam_2_memory_and_trace.py::Trace` |

### Non-guarantees (honest limits)

| Not guaranteed | Why |
|---|---|
| Recall relevance | Ranking is a heuristic (tag/word overlap, weight, recency); no relevance guarantee. |
| Semantic quality | Depends entirely on the local embedder; the kit only checks the vector's shape. |
| Truncation detection from the chain alone | A consistently rewritten chain needs a head hash remembered *elsewhere*. |
| Memory DB integrity | The sqlite file is an index; it is not hash-chained. The ledger is the source of truth and the index can be rebuilt from it. |
| Grading adaptivity | The weight formula is fixed; decay/reinforcement by recurrence beyond it is DESIGNED ONLY (STATUS.md). |
| Preflight vocabulary | `PREFLIGHT_MAP` covers a small fixed set of verbs; unknown verbs match only their own name. |
| Vector-database behavior | Not a vector database (STATUS.md): no ANN index, no collections — lessons, documents and cards only. |
| Exception/escalation content | Schema-only nodes; `not_populated` until a record exists. |

## 5. Failure modes (from the published tests)

| Situation | What happens | Test file |
|---|---|---|
| Ledger entry edited on disk | `verify` returns `(False, problems)` | `test_librarian.py`, `redteam_1_ledger.py` |
| Tail cut off, or chain fully rewritten with fresh hashes | Invisible to the chain alone; caught by `verify(expected_head, min_length)` with an externally remembered head | `redteam_1_ledger.py` |
| Torn last line (crashed write) | Fails closed — refused, not half-read | `redteam_1_ledger.py` |
| Replicas disagree | Open and writes refused | `redteam_1_ledger.py` |
| Secret-shaped value or secret-named field in a lesson | Refused; nothing written | `redteam_1_ledger.py` |
| Bad types/ranges, NaN, non-JSON values, parent naming a non-lesson | `LessonRefused` before storage | `redteam_1_ledger.py` |
| Broken legacy ledger | Whole import refused | `redteam_1_ledger.py` |
| Odd/hostile recall and preflight arguments | No crash; empty or safe results | `redteam_1_ledger.py`, `redteam_2_memory_and_trace.py` |
| Non-loopback embedder URL | Refused unless explicitly allowed | `redteam_2_memory_and_trace.py` |
| Redirect to another host | Not followed | `redteam_2_memory_and_trace.py` |
| Bad embedder vector | Counted as a failure; never stored as garbage | `redteam_2_memory_and_trace.py` |
| Oversize document or bad `kind` | Refused | `redteam_2_memory_and_trace.py` |
| Same-named documents in different folders | Never overwrite each other (path-hash ids) | `redteam_2_memory_and_trace.py` |
| Card hash swap, downgrade, revoked revival | Refused | `redteam_2_memory_and_trace.py` |
| Hostile conductor registry rows | Skipped, never applied | `redteam_2_memory_and_trace.py` |
| Edited/missing/damaged trace graph | `TraceRefused` on load with a clear message | `redteam_2_memory_and_trace.py` |
| Damaged event ledger during build | Refused, not silently skipped | `redteam_2_memory_and_trace.py` |
| Failed graph build | Previous graph intact; no temp files left | `redteam_2_memory_and_trace.py` |
| Short/ambiguous node id in `why` | Refused | `redteam_2_memory_and_trace.py` |

## 6. Dependencies and layer rules

Family registry entry (verbatim from the public registry):

```
| vega-librarian-sdk | 5 memory | custody |
```

The only runtime dependency is **vega-custody-sdk** (layer 0): its `Chain` provides the append-only, hash-chained, replicated ledger, and its `Dag` receives `seal` status entries. Dependencies point down layers only, never up or sideways. Python ≥ 3.10; standard library otherwise (sqlite3, urllib). The embedder is an optional local service: when absent, memory degrades to words-only scoring and says so. Consumers (e.g. the layer-6 harness conductor) call into this kit; this kit never calls up.

## 7. Conformance

A third party can verify this specification by running the published tests: put this repo's `src/` and vega-custody-sdk's `src/` on `PYTHONPATH`, then run each file in `tests/` (`test_librarian.py`, `redteam_1_ledger.py`, `redteam_2_memory_and_trace.py`). STATUS.md records 53 tests in 3 files, all passing.

**Passing means:** the reference implementation behaves as specified above on the published functional and red-team cases, on this machine, at this commit.

**Passing does not mean:** the design is secure against an adversary beyond those test cases; the cryptography has had expert review; recall will be relevant for your data; or the kit is hardened for production. A clean test run, like a clean automated scan (REVIEW.md), is not proof of safety.
