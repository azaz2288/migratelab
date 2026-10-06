# Verified progress

## 2026-10-07 v0.4 graph review and explicit route stage

Implemented bounded migration catalog review without database access, SQL execution, SQL-body output or path enumeration. Sorted graph/edge SHA256, roots/sinks/branches, disconnected components and optional explicit subroute are visible. Rehearsal requires an explicit route even for a unique path, executes only selected edges on the backup, and preserves existing whole-chain rollback, version checks, policies and exclusive report publication. Optional expected graph digest binds the complete reviewed catalog, including unselected SQL, before DB/output access; it is not a signed approval or persistent migration history.

First new tests hit one module import error before the new API existed (not 13 executed failures). Initial 13 methods passed after implementation; WAL, final invariant rollback, input mutation isolation, 100-edge boundary, Unicode canonical digest and already-committed-copy report faults increased the suite to 71. Three digest-binding tests bring the final suite to 74 methods, 21 new; all 74 passed in 8.796 seconds, and all three synthetic demos, compile and diff checks passed. Final installed verification and exact-SHA CI are recorded in the external portfolio maintenance report, not inferred from earlier versions. All databases are synthetic temporary fixtures. CLI-only stage has no browser UI; actual subprocess and installed console verification apply.

Limits remain: review completeness does not prove SQL semantics/schema compatibility/safety; metadata hashes are not provenance, identity or anonymity. No automatic path selection, persistent history, production migrations, OS hard deadline or GUI. README separates completed graph review from still-open controlled history and other milestones.

Offline-built 0.4.0 wheel installed only in this project's venv; source-external cwd with isolated Python verified site-packages/version, all 21 graph tests (2.388s), all 12 existing fault tests (3.616s), graph demo, both console help commands and pip check. Portfolio root 5 maintenance-tool tests passed. Wheel SHA256 and later remote/CI evidence remain in the external stage report; no Release assets were replaced.

## 2026-10-06 v0.1
17 tests, real temporary SQLite demo, independent wheel installation and installed CLI passed. Read-only original backup, isolated transaction rollback, foreign-key/integrity protection, SQL authorizer, trigger statement handling and WAL visibility. Published 703522d506e2a3dd75d5754bf46563f3febbfe49; matching Windows/Linux CI succeeded.

## 2026-10-06 v0.2 data invariants
Added full typed row/column-layout preservation and read-only scalar checks before commit. Same row count with overwritten names now rolls back. Content ordering is independent of rowid, numeric type ties and NOCASE text ties. Reject ambiguous checks JSON; CLI check-file integration exercised. Checks and digest policies are opt-in and are not arbitrary SQL isolation.

Synthetic 50,000-row benchmark: 2.702 seconds, 26,790 bytes Python allocation peak, 933,888-byte database. Not SQLite/native RSS or a universal performance guarantee. Next milestones: migration versions/graph, relational declarations, disk/lock fault injection, hard resource isolation.

## 2026-10-06 v0.3 explicit versioned chain
Implemented controlled SQLite user_version transitions and a contiguous declared SQL chain in one transaction, on the backup copy only. Wrong snapshot start executes zero SQL; late SQL or final data-check failure rolls back schema, data and version together. Strict integer/range/edge/count/combined-SQL limits, no auto-routing or downgrade, original unversioned API preserved. CLI single-edge and JSON-chain integration plus WAL-backed version checks covered. New tests initially could not import the unimplemented API; after implementation all 38 tests passed. Packaging and current-SHA CI are recorded separately in the portfolio evidence; this entry is not a claim of full flagship completion.

## 2026-10-06 v0.3.1 fault and report publication stage

Initial 8 fault cases produced 6 failures: 5 demonstrated early/partial final JSON visibility, competing report overwrite, or absent file sync; one SQLITE_FULL fixture did not yet force overflow and was corrected to a 1 MiB synthetic BLOB in the limited copy. Existing lost-transaction protection was retained, not weakened. First fix passed all 49 tests; extended source/copy locks and publication checks bring the final suite to 53 tests.

Reports now serialize privately, flush/fsync, close and publish through exclusive hardlink, never replacing a concurrent report. Temporary cleanup does not delete preview.sqlite. Real SQLite page-limit exhaustion and exclusive locks plus injected multi-chunk backup, write/fsync/link faults are generated in temporary directories only. Output failure is CLI exit2, not a false success/rollback, even if the copy already committed. No directory-fsync/power-loss guarantee, malicious-directory isolation, hard OS deadline, or genuine host disk exhaustion is claimed. No browser UI exists at this CLI stage; source/installed command verification is applicable. Final installation, exact push SHA and matching CI evidence live in maintenance reports outside this repository to avoid recursive evidence-only commits.
