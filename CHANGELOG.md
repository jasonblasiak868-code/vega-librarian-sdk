# Changelog

## 2026-10-20: fixes found by a source read

These were found by reading the source against its own documentation after release, not by the earlier test and review passes. Each fix has a regression test in
`tests/test_findings_20261020.py` that failed before the fix, and each fix was checked by undoing it and confirming a test fails. Behaviour that changed is listed under "Changed".

### Fixed
- **A card id could be registered twice with different hashes** by two writers, though "a registered card's hash never changes". The read of the old card and the write of the new one are now one write transaction.
