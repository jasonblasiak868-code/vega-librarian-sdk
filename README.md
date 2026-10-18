# vega-librarian-sdk

![vega-librarian-sdk](assets/tile.svg)

The Librarian: graded lessons from mistakes, tiered semantic memory, and a card registry.

> **Licensing.** Source-available, not open source. Free for personal use; free for registered small businesses (under US$1M revenue and valuation); everyone else needs a commercial license (see LICENSE; for a commercial license write to jasonblasiak.ai.mail@gmail.com). Patent pending. Draft licence text, not yet reviewed by an attorney.

Package: `vega_librarian` (src layout). Depends on: vega-custody-sdk.

## Modules

| Module | What it does |
|---|---|
| `lesson_ledger` | Every mistake becomes a graded lesson on the custody chain: append-only, replica-checked, credential-shaped values refused, recall by situation, preflight before a risky verb. |
| `librarian_memory` | Short- and long-term memory over lessons and documents with local-only embeddings, word-match fallback, abstain-below-threshold, and a card registry that only moves candidate to locked. |
| `decision_trace` | A context graph over lessons: every edge says whether it was declared or derived; nodes are hash-checked on load. |

## What it does NOT do (read this)

- Recall is a ranking, not a guarantee; below the abstain threshold it returns nothing rather than a guess.
- The embedder must be local: a non-loopback address is refused unless explicitly allowed.
- Recalled document text is data, never instructions.

## Status

See STATUS.md. Tests: 53 tests in 3 files, all passing.  See SECURITY.md for the threat model.

## Run the tests

Put `src` of this repo and of the repos it depends on on PYTHONPATH, then run each file in `tests/`.
