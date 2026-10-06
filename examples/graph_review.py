"""End-to-end declared branches, explicit choice and rollback in a new synthetic DB."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1])) if not sys.flags.isolated else None
from migratelab.graph import preview_graph, review_graph


def main():
    with tempfile.TemporaryDirectory(prefix='migratelab-graph-demo-') as temporary:
        root = Path(temporary)
        source = root / 'source.sqlite'
        with closing(sqlite3.connect(source)) as db:
            db.executescript('CREATE TABLE players(id INTEGER); INSERT INTO players VALUES(1); PRAGMA user_version=2;')
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        catalog = [{'from_version': 2, 'to_version': 3, 'sql': 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;'},
                   {'from_version': 3, 'to_version': 5, 'sql': 'UPDATE players SET level=2;'},
                   {'from_version': 2, 'to_version': 5, 'sql': 'DROP TABLE players;'}]
        review = review_graph(catalog)
        assert review['branches'] == [{'version': 2, 'to_versions': [3, 5]}]
        chosen = preview_graph(source, root / 'chosen', catalog, route=[2, 3, 5], preserve_tables=['players'],
                               expected_graph_sha256=review['graph_sha256'])
        rejected = preview_graph(source, root / 'rejected', catalog, route=[2, 5], preserve_tables=['players'])
        assert chosen['passed'] and chosen['after']['user_version'] == 5
        assert not rejected['passed'] and rejected['rolled_back'] and rejected['before'] == rejected['after']
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original
        print(json.dumps({'synthetic_only': True, 'graph_sha256': review['graph_sha256'],
                          'branch_reviewed': True, 'explicit_route_passed': True,
                          'destructive_route_rolled_back': True, 'source_unchanged': True}))


if __name__ == '__main__':
    main()
