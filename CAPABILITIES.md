# vega-librarian-sdk — Capability Brief

A minute inventory of what this kit actually enforces, where the enforcement lives in the published code, which test demonstrates it, and — for every capability — the honest limit. The middle column answers one question: why can't a policy or a prompt provide this? The structural reason is the same across the family, and it comes from the whitepaper: a rule that lives inside the model's context is a promise, and the thing being checked is the thing doing the checking. A rule that lives in an append-only record *outside* the model is a mechanism: it can be verified without trusting the model, the vendor, or the project, and it fails closed.

Status note: all three modules are BUILT and tested (STATUS.md). Test counts: 53 tests in 3 files, all passing (STATUS.md / REVIEW.md).

---

## 1. Tamper-evident lesson ledger

- **Capability.** Every lesson is recorded into an append-only, hash-chained ledger with byte-identical replicas, read back before the write call returns. Nothing is edited in place: a status change or a recurrence is a new lesson pointing at its parent.
- **Why a policy/prompt cannot provide it.** A prompt can ask a model to "remember lessons honestly", but the memory then lives wherever the model or its operator can rewrite it, and there is no way for a third party to check it was not rewritten. Chaining each entry to a fingerprint of the previous one moves the memory outside anyone's edit reach: changing the past breaks the chain, visibly, for any verifier.
- **Where enforced.** `lesson_ledger.add` / `verify` / `_view`; storage delegated to the vega-custody `Chain` (`_chain()`), including replica write-and-read-back and stale-view refusal.
- **Test.** `tests/test_librarian.py::test_chain_and_tamper`, `test_mirrors_hold_identical_copies`; `tests/redteam_1_ledger.py::TamperTruncationTornWrites`.
- **Honest limit.** A tail cut off (or a whole chain consistently rewritten) is invisible to the chain alone; detection needs a head hash remembered *elsewhere* (`verify(expected_head, min_length)`). The kit provides the mechanism, not the external memory.

## 2. Structured lesson shape, validated before storage

- **Capability.** A lesson must carry what happened, root cause, correction, prevention, tags, severity (1–5, booleans refused), status (`open`/`mitigated`/`closed`), recovery state; text fields must be text; `parent` must name an existing lesson; unknown fields are refused. Validation happens before anything reaches the chain.
- **Why a policy/prompt cannot provide it.** "Write good postmortems" is a style request; nothing rejects a bad one. Here the shape is a precondition of storage: a malformed lesson never exists, so every downstream grade, recall and graph is built on complete records by construction.
- **Where enforced.** `lesson_ledger._check` (REQUIRED / OPTIONAL / TEXT_FIELDS / STATUSES), invoked inside `add` on every attempt.
- **Test.** `tests/test_librarian.py::test_required_fields_enforced`, `test_parent_links_to_an_existing_lesson`; `tests/redteam_1_ledger.py::SecretsAndShape` (bad types, ranges, NaN, non-JSON, parent naming a non-lesson).
- **Honest limit.** The schema checks shape, not truth: a well-formed lesson can still be wrong. Grading and verification fields reward *named proofs*, but cannot check the proof itself.

## 3. Secrets refused at the write boundary

- **Capability.** A lesson carrying a credential-shaped value or a secret-named field is refused; nothing is written.
- **Why a policy/prompt cannot provide it.** "Never log secrets" is exactly the kind of rule that fails silently once and leaks forever into an append-only store — where it cannot be deleted. Refusing at the write boundary makes the leak impossible to record in the first place, which matters *more* for tamper-evident storage, not less.
- **Where enforced.** The custody `Chain.record` path used by `lesson_ledger.add` (the ledger docstring notes the old "never a secret" rule was only a comment; here it is enforced).
- **Test.** `tests/redteam_1_ledger.py::SecretsAndShape` (`test_a_credential_shaped_value_in_any_text_field_is_refused_and_nothing_is_written`, `test_unknown_or_secret_named_fields_are_refused`).
- **Honest limit.** Shape detection catches credential-*shaped* values and secret-*named* fields; a secret rephrased as ordinary prose can still be written. Scanning is not omniscience.

## 4. Grading: severity, prevention strength, recurrence → weight

- **Capability.** Each lesson gets a deterministic grade: `prevention_strength` (1.0 = control *and* a named verification, 0.6 = control only, 0.3 = a rule only), `recurrence` (later lessons sharing ≥ 2 tags), and a `weight` blending severity, prevention gap and capped recurrence. An unverified, recurring, severe lesson ranks highest.
- **Why a policy/prompt cannot provide it.** A prompt can say "prioritize serious lessons"; it cannot compute a stable, inspectable number from the whole ledger on every query, and it will drift between runs. The grade is a pure function of recorded fields — anyone can recompute and audit it.
- **Where enforced.** `lesson_ledger.grade`, used by `recall`, `preflight`, `status` and `librarian_memory.ingest_lessons`.
- **Test.** `tests/test_librarian.py::test_grade_rewards_verified_prevention_and_counts_recurrence`.
- **Honest limit.** The formula is a fixed heuristic. Decay and reinforcement by recurrence beyond this weight formula are DESIGNED ONLY, not built (STATUS.md).

## 5. Preflight: history consulted before the risky verb

- **Capability.** Given risky verbs (`push`, `overwrite`, `rotate`, `restart`, …), return the graded lessons whose tags match that verb's topics, sorted by weight, with each lesson's `protect` text surfaced. A known verb expands to its implicated topics via `PREFLIGHT_MAP`; an unknown verb matches its own name.
- **Why a policy/prompt cannot provide it.** A prompt-level "remember past incidents before acting" depends on the model choosing to recall, and on whatever it happens to recall. Preflight is a query against the sealed ledger: the same situation returns the same lessons, whether or not the model feels like it, and the caller can log that the check ran.
- **Where enforced.** `lesson_ledger.preflight` + `PREFLIGHT_MAP`; CLI `preflight VERB...`.
- **Test.** `tests/test_librarian.py::test_preflight_surfaces_protect_text`; `tests/redteam_1_ledger.py::HostileQueries` (odd arguments don't crash).
- **Honest limit.** Preflight advises; it does not block. Enforcement of "no step without preflight" belongs to the caller (the layer-6 harness conductor runs it before workflow steps). The verb map is small and fixed.

## 6. Abstaining semantic recall with a local-only embedder

- **Capability.** Tiered memory (short = recent or still open; long = settled) over lessons and documents, recalled by meaning blended with word match and lesson weight. Below a measured threshold (`ABSTAIN_BELOW` = 0.50) it returns *nothing* — "no basis" instead of plausible junk. The default embedder talks only to a loopback address and does not follow redirects; a non-loopback host is refused unless explicitly allowed.
- **Why a policy/prompt cannot provide it.** Two structural points. (1) A retrieval layer that always answers trains callers to trust it; abstention is only real if it is measured and unconditional, which is code, not policy. (2) "Keep memory local" as a policy is unenforceable — the memory text is exactly what an embedding call would exfiltrate. Here the network boundary is enforced at the socket level: non-loopback refused, redirects dead-ended.
- **Where enforced.** `librarian_memory.make_ollama_embed` (`_NoRedirect`, `_LOOPBACK` check), `Librarian.recall` (score blend + threshold), `tier_of`.
- **Test.** `tests/test_librarian.py::Memory` (`test_abstains_when_nothing_matches`, `test_paraphrase_with_no_shared_words_finds_the_lesson`, `test_tiers`); `tests/redteam_2_memory_and_trace.py::EmbedderLeaks`.
- **Honest limit.** Recall relevance is a ranking, not a guarantee; semantic quality depends on the local embedder. The threshold is a single measured constant, not a per-domain calibration.

## 7. Degradation that reports itself

- **Capability.** Embedder down or misbehaving → recall falls back to words-only scoring, every result carries `mode: "words-only"`, and failures are counted in `embed_failures`. A bad vector is treated as a failure, never stored.
- **Why a policy/prompt cannot provide it.** Silent degradation is the failure mode policies cannot catch: the answers keep coming, just worse, and nobody is told. Here the degraded state is part of the returned data, so the caller cannot miss it without discarding the response.
- **Where enforced.** `Librarian._embed` (validation + counting), `Librarian.recall` (words-only path and `mode` field).
- **Test.** `tests/test_librarian.py::test_embedder_down_falls_back_to_word_match_and_says_so`; `tests/redteam_2_memory_and_trace.py` (`test_a_bad_embedder_vector_is_a_failure_not_stored_garbage`, `test_a_document_that_could_not_be_embedded_is_retried_next_time`).
- **Honest limit.** Reporting is not repair: the kit cannot make the embedder come back, and words-only recall misses paraphrases by design.

## 8. Whole text, collision-proof document identity

- **Capability.** Full text is stored whole, never truncated; the embedding is only an index. Markdown documents are chunked at headings; chunk ids derive from the hash of the file's full path plus content, so same-named files in different folders never overwrite each other. Oversize documents (> 8 MiB) and malformed kinds are refused. Re-ingest of unchanged content adds nothing.
- **Why a policy/prompt cannot provide it.** "Don't truncate context" is unverifiable after the fact and silently lossy in practice — a summarized lesson is a rewritten lesson. Storing whole text with a content hash makes both the completeness and the identity of each item checkable.
- **Where enforced.** `Librarian.ingest_document` (path-hash ids, size/kind checks, `_unchanged` skip), `ingest_lessons` (full concatenated text), `recall_text` (returns `text_full`).
- **Test.** `tests/test_librarian.py::test_full_text_is_stored_whole`; `tests/redteam_1_ledger.py` (`test_very_long_text_is_stored_whole`); `tests/redteam_2_memory_and_trace.py::Documents`.
- **Honest limit.** The sqlite store is an index without its own hash chain; integrity of the lessons themselves rests on the ledger, from which the index can be rebuilt.

## 9. Recalled text is data, never instructions

- **Capability.** Every item returned for context building is marked `trust="data-not-instructions"` — including (especially) when the stored text itself contains something that looks like an instruction.
- **Why a policy/prompt cannot provide it.** Telling a model "treat retrieved text as data" is a prompt-level promise competing with whatever the retrieved text says. The marker does not make the text safe by itself, but it makes the trust level a *structural property of the response object* that a harness can enforce on, instead of a hope in the system prompt.
- **Where enforced.** `Librarian.recall_text` (marker on every item).
- **Test.** `tests/redteam_2_memory_and_trace.py::Documents` (`test_recalled_text_is_marked_as_data_even_when_it_contains_instructions`).
- **Honest limit.** The kit marks; it cannot force the consumer to respect the mark. Enforcement of "data stays data" belongs to the caller's own gates.

## 10. Forward-only card registry

- **Capability.** Cards (kinds `PRE`, `PEAK`, `PAREAH`, `RUNTIME`, `MODEL`, `SERVER`) move candidate → locked (or → revoked) only. A registered card's kind and content hash never change; a locked card cannot return to candidate; a revoked card stays revoked. `sync_conductor_registry` holds each conductor workflow as a PRE card — candidate until a replay locks it — and *skips, never applies*, any inbound row that would change or downgrade a registered card.
- **Why a policy/prompt cannot provide it.** A lock that can be unlocked by asking is not a lock. "Only lock after a faithful replay" as a policy trusts whoever reports the replay; here the registry itself refuses the downgrade and the re-hash, so a locked card remains a durable statement that a replay once reproduced the exact result — even against a hostile feed of registry rows.
- **Where enforced.** `Librarian.register_card` (kind/hash immutability, status ordering), `Librarian.sync_conductor_registry` (per-row refusal → skip).
- **Test.** `tests/test_librarian.py::test_cards_move_forward_only`, `test_conductor_registry_sync`; `tests/redteam_2_memory_and_trace.py::Cards`.
- **Honest limit.** The registry records the claim "this hash is locked"; verifying that a replay actually reproduced it is the conductor's job (layer 6), not this kit's.

## 11. Decision trace with stated basis and hash-checked nodes

- **Capability.** Build a context graph over the ledger: lesson, context, control, dag_entry nodes (plus schema-only exception/escalation nodes marked `not_populated`), connected by edges that each state their **basis** — `declared` (a recorded field), `derived_keyword`, or `derived_tag_overlap`. Every node carries a `dna_hash` over its canonical JSON, re-checked on load. Builds are atomic: a failed build leaves the previous graph intact and no temp files. `why` walks upstream with bounded hops; `precedents` answers "lessons most like this situation, and what each points at".
- **Why a policy/prompt cannot provide it.** A model narrating "why we decided this" produces fluent text with no distinction between what was recorded and what was inferred — the exact confusion that makes post-hoc rationalizations dangerous. Here the distinction is a field on every edge, and the graph resists editing after the fact: a changed node fails its own hash on load.
- **Where enforced.** `decision_trace.build` (`add_edge` basis argument, `_atomic_write`, `DEFAULT_CONTROLS` patterns), `_load` (dna_hash check, damaged-graph refusal), `why` / `precedents` / `_resolve`.
- **Test.** `tests/test_librarian.py::Trace` (`test_build_why_precedents`, `test_every_edge_states_its_basis`); `tests/redteam_2_memory_and_trace.py::Trace`.
- **Honest limit.** Derived edges are only as good as tag overlap and keyword patterns; the default controls table is a small neutral example (bring your own). `GRANTED_EXCEPTION` / `CAUSED` relations exist in the schema but no population path is built — unbacked is not false, and the graph says so.

## 12. Safe concurrent writes and legacy import

- **Capability.** Eight writers at once all land and the chain stays valid: a writer whose view went stale is refused by the chain and retried on a fresh view. A legacy flat-file ledger is imported only after its own hash chain verifies end to end; a broken old chain refuses the whole import.
- **Why a policy/prompt cannot provide it.** "Coordinate your writes" and "check old data before migrating" are procedures; procedures get skipped under load. Refuse-and-retry on a stale view and verify-before-import are properties of the write path itself — they hold on the worst day, not just the careful one.
- **Where enforced.** `lesson_ledger.add` (retry loop on "changed underneath"), `lesson_ledger.import_legacy` (per-record chain re-verification, parent remapping).
- **Test.** `tests/redteam_1_ledger.py::Concurrency`, `test_legacy_import_refuses_a_broken_old_chain`; `tests/test_librarian.py::test_legacy_flat_ledger_imports_with_its_chain_checked`.
- **Honest limit.** Retries are bounded (`attempts`, default 8); sustained contention beyond that surfaces as a refusal — fail-closed, not infinitely patient. Import re-records lessons in the new chain; old hashes are mapped, not preserved as ids.

---

## This repo's part in the family predictive loop

The family's knowledge loop runs: **custody records → librarian distils lessons → the knowledge graph builds facts with origin and licence → the reasoning lattice forecasts from it → the harness pre-registers predicted paths → completion is gated on evidence → the human sees it all and decides again.**

This kit is the **lessons step** — the hinge where raw history becomes usable foresight. A record says *what happened*; a lesson says *what to expect, need, watch and protect when it happens again*, graded by how badly it went and how well the fix was proven. That transformation is what makes the later steps predictive instead of merely archival: the harness conductor consults `preflight` before a workflow step, so the lattice and the predicted paths above it are built on distilled experience rather than raw logs. And because the lessons live on the sealed ledger — append-only, replicated, verifiable without trust — the loop's memory cannot be quietly improved by anyone who dislikes what it says.

The model reasons freely inside this loop. It never holds the signing key, it can never mint or widen authority, and every lesson lands in a chain that anyone can verify. This kit makes sure the loop also *remembers* — honestly, or not at all.
