# Synthetic preservation benchmark

Run `python benchmarks/scale.py`. It creates 50,000 synthetic players inside a temporary directory, rehearses an index migration, verifies an identical typed-data digest, and cleans up only its own temporary files.

Local Windows/Python 3.12 measurement on 2026-10-06: SQLite file 933,888 bytes; rehearsal 2.702 s; Python tracemalloc peak 26,790 bytes. One local run only; tracemalloc excludes SQLite/native allocations and is not process RSS. Disk caches and hardware affect timing. No user databases used. No CI absolute timing threshold.
