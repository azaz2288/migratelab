from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from migratelab.core import MigrationError, load_migrations, preview, preview_chain


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite'
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript('CREATE TABLE players(id INTEGER); INSERT INTO players VALUES(1); PRAGMA user_version=2;')
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def step(self, start, end, sql='SELECT 1;'):
        return {'from_version': start, 'to_version': end, 'sql': sql}

    def version(self, path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def test_single_upgrade_records_version_and_keeps_original(self):
        report = preview(self.source, self.root / 'out', 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;',
                         from_version=2, to_version=3, preserve_tables=['players'])
        self.assertTrue(report['passed'])
        self.assertEqual(report['before']['user_version'], 2)
        self.assertEqual(report['after']['user_version'], 3)
        self.assertEqual(report['migration_chain'][0]['to_version'], 3)
        self.assertEqual(self.version(self.root / 'out/preview.sqlite'), 3)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def test_chain_is_one_transaction_and_records_each_sql_digest(self):
        chain = [self.step(2, 3, 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;'),
                 self.step(3, 5, 'UPDATE players SET level=2;')]
        report = preview_chain(self.source, self.root / 'out', chain,
                               checks=[{'sql': 'SELECT level FROM players', 'expected': 2}])
        self.assertTrue(report['passed'], report)
        self.assertEqual(report['after']['user_version'], 5)
        self.assertEqual([item['executed'] for item in report['migration_chain']], [1, 1])
        self.assertEqual(report['migration_chain'][1]['sql_sha256'], hashlib.sha256(chain[1]['sql'].encode()).hexdigest())
        self.assertEqual(report['executed'], 2)

    def test_wrong_start_executes_no_sql(self):
        report = preview_chain(self.source, self.root / 'out', [self.step(0, 1, 'DROP TABLE players;')])
        self.assertFalse(report['passed'])
        self.assertTrue(report['rolled_back'])
        self.assertEqual(report['executed'], 0)
        self.assertEqual(report['before'], report['after'])

    def test_late_failure_rolls_back_earlier_schema_data_and_version(self):
        report = preview_chain(self.source, self.root / 'out', [
            self.step(2, 3, 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;'),
            self.step(3, 4, 'INSERT INTO missing VALUES(1);')])
        self.assertFalse(report['passed'])
        self.assertTrue(report['rolled_back'])
        self.assertEqual(report['before'], report['after'])
        self.assertEqual(self.version(self.root / 'out/preview.sqlite'), 2)
        self.assertEqual(report['migration_chain'][1]['executed'], 0)

    def test_failed_final_invariant_rolls_back_all_versions(self):
        report = preview_chain(self.source, self.root / 'out', [self.step(2, 3), self.step(3, 4)],
                               checks=[{'sql': 'SELECT count(*) FROM players', 'expected': 9}])
        self.assertFalse(report['passed'])
        self.assertEqual(report['before'], report['after'])

    def test_sql_cannot_set_its_own_version(self):
        report = preview_chain(self.source, self.root / 'out', [self.step(2, 3, 'PRAGMA user_version=100;')])
        self.assertFalse(report['passed'])
        self.assertEqual(self.version(self.root / 'out/preview.sqlite'), 2)

    def test_invalid_edges_are_rejected_before_creating_output(self):
        candidates = [[], 'not a chain', [self.step(2, 2)], [self.step(3, 2)],
                      [self.step(True, 3)], [self.step(2, 3.0)], [self.step(-1, 2)],
                      [self.step(2, 2**31)], [self.step(2, 3), self.step(4, 5)],
                      [self.step(2, 3), self.step(2, 4)], [{'from_version': 2, 'to_version': 3}],
                      [dict(self.step(2, 3), extra=1)], [self.step(2, 3, '')],
                      [self.step(i, i + 1) for i in range(101)],
                      [self.step(2, 3, 'SELECT 1;--' + 'a' * 140000), self.step(3, 4, 'SELECT 2;--' + 'b' * 140000)]]
        for chain in candidates:
            with self.subTest(chain=str(chain)[:80]), self.assertRaises(MigrationError):
                preview_chain(self.source, self.root / 'out', chain)
            self.assertFalse((self.root / 'out').exists())

    def test_incomplete_single_version_pair_rejected_before_output(self):
        for kwargs in ({'from_version': 2}, {'to_version': 3}, {'from_version': False, 'to_version': 3}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MigrationError):
                preview(self.source, self.root / 'out', 'SELECT 1;', **kwargs)
            self.assertFalse((self.root / 'out').exists())

    def test_legacy_preview_reports_but_does_not_change_user_version(self):
        report = preview(self.source, self.root / 'out', 'SELECT 1;')
        self.assertTrue(report['passed'])
        self.assertEqual(report['after']['user_version'], 2)
        self.assertNotIn('migration_chain', report)

    def test_wal_snapshot_is_the_source_of_version_check(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA user_version=7')
            report = preview_chain(self.source, self.root / 'out', [self.step(7, 8)])
            self.assertTrue(report['passed'])
            self.assertEqual(report['before']['user_version'], 7)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)

    def test_cli_chain_and_version_flags(self):
        migrations = self.root / 'chain.json'
        migrations.write_text(json.dumps([self.step(2, 3), self.step(3, 4)]))
        sql = self.root / 'change.sql'
        sql.write_text('SELECT 1;')
        cases = [(migrations, ['--chain'], 0), (sql, ['--from-version', '2', '--to-version', '3'], 0),
                 (sql, ['--from-version', '1', '--to-version', '3'], 1),
                 (migrations, ['--chain', '--to-version', '3'], 2), (sql, ['--from-version', '2'], 2)]
        for index, (path, flags, expected) in enumerate(cases):
            with self.subTest(flags=flags):
                result = subprocess.run([sys.executable, '-m', 'migratelab', str(self.source), str(path),
                                         str(self.root / f'cli-{index}'), *flags], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_chain_json_rejects_duplicates_nonfinite_and_oversize(self):
        path = self.root / 'chain.json'
        for content in ('[{"from_version":2,"from_version":0,"to_version":3,"sql":"SELECT 1;"}]',
                        '[{"from_version":2,"to_version":NaN,"sql":"SELECT 1;"}]',
                        'x' * (256 * 1024 + 1), '[broken'):
            path.write_text(content)
            with self.subTest(content=content[:80]), self.assertRaises(MigrationError):
                load_migrations(path)

    def test_chain_timeout_rolls_back_prior_step(self):
        report = preview_chain(self.source, self.root / 'out', [
            self.step(2, 3, 'ALTER TABLE players ADD COLUMN level INTEGER;'),
            self.step(3, 4, 'WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n;')], timeout=0.1)
        self.assertFalse(report['passed'])
        self.assertTrue(report['rolled_back'])
        self.assertEqual(report['before'], report['after'])

    def test_maximum_version_can_be_set_but_not_incremented(self):
        report = preview_chain(self.source, self.root / 'out', [self.step(2, 2147483647)])
        self.assertTrue(report['passed'])
        self.assertEqual(self.version(self.root / 'out/preview.sqlite'), 2147483647)


if __name__ == '__main__':
    unittest.main()
