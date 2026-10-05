"""Demonstrate all-or-nothing upgrades without touching user databases."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migratelab.core import preview_chain


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / 'characters.sqlite'
    with closing(sqlite3.connect(source)) as db:
        db.executescript("CREATE TABLE players(id INTEGER, name TEXT); INSERT INTO players VALUES(1,'Mage'); PRAGMA user_version=2;")
    first = {'from_version': 2, 'to_version': 3, 'sql': 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;'}
    second = {'from_version': 3, 'to_version': 5, 'sql': 'UPDATE players SET level=2;'}
    good = preview_chain(source, root / 'good', [first, second],
                         checks=[{'sql': 'SELECT level FROM players', 'expected': 2}])
    bad = preview_chain(source, root / 'bad', [first, dict(second, sql='INSERT INTO missing VALUES(1);')])
    mismatch = preview_chain(source, root / 'mismatch', [dict(first, from_version=1)])
    assert good['passed'] and good['after']['user_version'] == 5
    assert bad['rolled_back'] and bad['before'] == bad['after']
    assert mismatch['rolled_back'] and mismatch['executed'] == 0
    with closing(sqlite3.connect(source)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert len(db.execute('PRAGMA table_info(players)').fetchall()) == 2
    print(json.dumps({'successful_chain_target': good['after']['user_version'],
                      'late_failure_rolled_back_to': bad['after']['user_version'],
                      'wrong_version_executed': mismatch['executed'], 'original_untouched': True}))
