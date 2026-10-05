"""Run a real SQLite migration demo entirely in a temporary directory."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migratelab.core import preview

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / "characters.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript("CREATE TABLE players(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO players VALUES(1,'Mage'),(2,'Paladin');")
    good = preview(source, root / "good", "ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;", preserve_tables=["players"])
    bad = preview(source, root / "bad", "DELETE FROM players;", preserve_tables=["players"])
    assert good["passed"] and bad["rolled_back"] and bad["after"]["rows"]["players"] == 2
    print(json.dumps({"successful_upgrade": good["passed"], "dangerous_delete_rolled_back": bad["rolled_back"],
                      "characters_preserved": bad["after"]["rows"]["players"]}))
