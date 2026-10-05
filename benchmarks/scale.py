"""Synthetic SQLite rehearsal benchmark; never opens a user's database."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tracemalloc
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migratelab.core import preview

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / "synthetic.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE players(id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany("INSERT INTO players VALUES(?,?)", ((i, f"hero-{i}") for i in range(50000)))
        connection.commit()
    tracemalloc.start()
    started = time.perf_counter()
    result = preview(source, root / "preview", "CREATE INDEX names ON players(name);",
                     preserve_data_tables=["players"], timeout=60)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result["passed"] and result["after"]["rows"]["players"] == 50000
    print(json.dumps({"rows":50000,"database_bytes":source.stat().st_size,
                      "seconds":round(elapsed,3), "python_peak_bytes":peak,
                      "note":"One local synthetic measurement, not SQLite native RSS or a production guarantee"}))
