# Verified progress

## 2026-10-06 v0.1
17 tests, real temporary SQLite demo, independent wheel installation and installed CLI passed. Read-only original backup, isolated transaction rollback, foreign-key/integrity protection, SQL authorizer, trigger statement handling and WAL visibility. Published 703522d506e2a3dd75d5754bf46563f3febbfe49; matching Windows/Linux CI succeeded.

## 2026-10-06 v0.2 data invariants
Added full typed row/column-layout preservation and read-only scalar checks before commit. Same row count with overwritten names now rolls back. Content ordering is independent of rowid, numeric type ties and NOCASE text ties. Reject ambiguous checks JSON; CLI check-file integration exercised. Checks and digest policies are opt-in and are not arbitrary SQL isolation.

Synthetic 50,000-row benchmark: 2.702 seconds, 26,790 bytes Python allocation peak, 933,888-byte database. Not SQLite/native RSS or a universal performance guarantee. Next milestones: migration versions/graph, relational declarations, disk/lock fault injection, hard resource isolation.
