"""Synthetic graph review and explicit-route rehearsal; installed-wheel compatible."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from migratelab.core import MigrationError
import migratelab.graph as graph


def edge(start, end, sql='SELECT 1;'):
    return {'from_version': start, 'to_version': end, 'sql': sql}


class GraphTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.sqlite'
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript('CREATE TABLE players(id INTEGER); INSERT INTO players VALUES(1); PRAGMA user_version=2;')
        self.original = self.source.read_bytes()
        self.catalog = [edge(2, 3, 'ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;'),
                        edge(3, 5, 'UPDATE players SET level=2;'), edge(2, 5, 'DROP TABLE players;')]

    def test_metadata_review_branching_and_sql_redaction(self):
        with patch('sqlite3.connect', side_effect=AssertionError('review must not open database')):
            report = graph.review_graph(self.catalog)
        self.assertEqual(report['versions'], [2, 3, 5])
        self.assertEqual(report['roots'], [2])
        self.assertEqual(report['sinks'], [5])
        self.assertEqual(report['branches'], [{'version': 2, 'to_versions': [3, 5]}])
        self.assertNotIn('DROP TABLE', json.dumps(report))
        self.assertEqual(len(report['graph_sha256']), 64)
        self.assertEqual(report['edges'][0]['sql_sha256'], hashlib.sha256(self.catalog[0]['sql'].encode()).hexdigest())
        self.assertNotIn('selected_route', report)

    def test_canonical_digest_order_independent_but_sensitive_to_unselected_sql(self):
        first = graph.review_graph(self.catalog, route=[2, 3, 5])
        self.assertEqual(first, graph.review_graph(list(reversed(self.catalog)), route=[2, 3, 5]))
        altered = self.catalog[:-1] + [edge(2, 5, 'SELECT 9;')]
        self.assertNotEqual(first['graph_sha256'], graph.review_graph(altered)['graph_sha256'])
        self.assertEqual(first['selected_route'], [2, 3, 5])

    def test_explicit_route_executes_only_selected_edges(self):
        report = graph.preview_graph(self.source, self.root / 'out', self.catalog, route=[2, 3, 5],
                                     preserve_tables=['players'], checks=[{'sql': 'SELECT level FROM players', 'expected': 2}])
        self.assertTrue(report['passed'])
        self.assertEqual(report['executed'], 2)
        self.assertEqual(report['after']['user_version'], 5)
        self.assertEqual(report['graph_review'], graph.review_graph(self.catalog, route=[2, 3, 5]))
        saved = json.loads((self.root / 'out/report.json').read_text())
        self.assertEqual(saved['graph_review'], report['graph_review'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_missing_route_never_auto_selects_even_one_possible_path(self):
        for catalog in (self.catalog, [edge(2, 3)]):
            with self.subTest(catalog=catalog), self.assertRaises(MigrationError):
                graph.preview_graph(self.source, self.root / 'out', catalog, route=None)
            self.assertFalse((self.root / 'out').exists())

    def test_invalid_routes_precede_output_and_database_io(self):
        for route in ([], [2], [2, 4, 5], [3, 2], [2, 3, 3], [True, 5], [2, 5.0], '2,5', [2, 2**31]):
            with self.subTest(route=route), patch('sqlite3.connect') as connect:
                with self.assertRaises(MigrationError):
                    graph.preview_graph(self.source, self.root / 'out', self.catalog, route=route)
                connect.assert_not_called()
                self.assertFalse((self.root / 'out').exists())

    def test_duplicate_edges_rejected_not_last_value_wins(self):
        for extra in (self.catalog[0], edge(2, 3, 'DROP TABLE players;')):
            with self.assertRaises(MigrationError):
                graph.review_graph(self.catalog + [extra])

    def test_invalid_catalog_and_unselected_sql_are_rejected(self):
        candidates = ([], {}, [edge(2, 2)], [edge(True, 3)], [edge(2, 3.0)],
                      [edge(2, 2**31)], [dict(edge(2, 3), extra=1)], [edge(2, 3, '')],
                      [edge(i, i + 1) for i in range(101)],
                      [edge(2, 3, 'SELECT 1;--' + 'a' * 140000), edge(2, 4, 'SELECT 2;--' + 'b' * 140000)])
        for catalog in candidates:
            with self.subTest(kind=str(catalog)[:60]), self.assertRaises(MigrationError):
                graph.review_graph(catalog)
        with self.assertRaises(MigrationError):
            graph.preview_graph(self.source, self.root / 'out', [edge(2, 3), edge(2, 4, '\0')], route=[2, 3])
        self.assertFalse((self.root / 'out').exists())

    def test_late_failure_rolls_back_all_versions_and_schema_with_graph_provenance(self):
        catalog = [self.catalog[0], edge(3, 5, 'INSERT INTO missing VALUES(1);')]
        report = graph.preview_graph(self.source, self.root / 'out', catalog, route=[2, 3, 5])
        self.assertFalse(report['passed'])
        self.assertTrue(report['rolled_back'])
        self.assertEqual(report['before'], report['after'])
        self.assertEqual(report['graph_review']['selected_route'], [2, 3, 5])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_wrong_start_no_sql_with_review_preserved(self):
        report = graph.preview_graph(self.source, self.root / 'out', [edge(0, 1, 'DROP TABLE players;')], route=[0, 1])
        self.assertFalse(report['passed'])
        self.assertEqual(report['executed'], 0)
        self.assertEqual(report['before'], report['after'])

    def test_wal_backup_version_and_final_invariant_rollback(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA user_version=7')
            report = graph.preview_graph(self.source, self.root / 'out', [edge(7, 8)], route=[7, 8],
                                         checks=[{'sql': 'SELECT count(*) FROM players', 'expected': 99}])
            self.assertFalse(report['passed'])
            self.assertTrue(report['rolled_back'])
            self.assertEqual(report['before']['user_version'], 7)
            self.assertEqual(report['before'], report['after'])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)

    def test_caller_mutation_after_validation_does_not_change_selected_sql_or_route(self):
        route = [2, 3, 5]
        import migratelab.core as core
        execute = core._preview
        def mutate_then_execute(source, output, steps, **options):
            self.catalog[0]['sql'] = 'DROP TABLE players;'
            self.catalog.clear()
            route[:] = [2, 5]
            return execute(source, output, steps, **options)
        expected = graph.review_graph(self.catalog, route=route)
        with patch('migratelab.graph._preview', side_effect=mutate_then_execute):
            report = graph.preview_graph(self.source, self.root / 'out', self.catalog, route=route, preserve_tables=['players'])
        self.assertTrue(report['passed'])
        self.assertEqual(report['graph_review'], expected)
        self.assertEqual(report['after']['rows']['players'], 1)

    def test_maximum_catalog_and_route_limits(self):
        catalog = [edge(i, i + 1) for i in range(100)]
        review = graph.review_graph(catalog, route=list(range(101)))
        self.assertEqual(len(review['edges']), 100)
        self.assertEqual(len(review['selected_route']), 101)
        with self.assertRaises(MigrationError):
            graph.review_graph(catalog, route=list(range(102)))
        self.assertEqual(graph.review_graph([edge(0, 2147483647)])['sinks'], [2147483647])

    def test_failure_publishing_graph_report_is_not_success_or_false_rollback(self):
        with patch('migratelab.core._publish_report', side_effect=OSError('synthetic fault')):
            with self.assertRaises(MigrationError):
                graph.preview_graph(self.source, self.root / 'out', self.catalog, route=[2, 3, 5])
        with closing(sqlite3.connect(self.root / 'out/preview.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
        self.assertFalse((self.root / 'out/report.json').exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_canonical_digest_has_independent_unicode_byte_definition(self):
        catalog = [edge(1, 3, "SELECT '你好';"), edge(0, 1)]
        canonical = json.dumps(sorted(catalog, key=lambda e: (e['from_version'], e['to_version'])),
                               ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(graph.review_graph(catalog)['graph_sha256'], hashlib.sha256(canonical).hexdigest())

    def test_review_digest_binding_accepts_reordering_rejects_unselected_change_before_io(self):
        digest = graph.review_graph(self.catalog)['graph_sha256']
        report = graph.preview_graph(self.source, self.root / 'ok', list(reversed(self.catalog)), route=[2, 3, 5],
                                     expected_graph_sha256=digest)
        self.assertTrue(report['passed'])
        altered = self.catalog[:-1] + [edge(2, 5, 'SELECT 9;')]
        with patch('sqlite3.connect') as connect, self.assertRaises(MigrationError):
            graph.preview_graph(self.source, self.root / 'out', altered, route=[2, 3, 5], expected_graph_sha256=digest)
        connect.assert_not_called()
        self.assertFalse((self.root / 'out').exists())

    def test_invalid_expected_digest_rejected_before_output(self):
        for digest in ('', 'a' * 63, 'A' * 64, True, 123, []):
            with self.subTest(digest=digest), self.assertRaises(MigrationError):
                graph.preview_graph(self.source, self.root / 'out', self.catalog, route=[2, 3, 5], expected_graph_sha256=digest)
            self.assertFalse((self.root / 'out').exists())

    def test_cli_digest_binding_mismatch_no_output(self):
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(self.catalog))
        result = subprocess.run([sys.executable, '-m', 'migratelab', str(self.source), str(path), str(self.root / 'out'),
                                 '--graph', '--route', '2', '3', '5', '--expect-graph-sha256', '0' * 64], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / 'out').exists())

    def test_disconnected_catalog_and_explicit_subroute_are_visible(self):
        review = graph.review_graph([edge(0, 1), edge(2, 3), edge(3, 5)], route=[3, 5])
        self.assertEqual(review['roots'], [0, 2])
        self.assertEqual(review['sinks'], [1, 5])
        self.assertEqual(review['selected_route'], [3, 5])

    def test_large_branching_graph_review_never_enumerates_paths(self):
        catalog = [edge(i, j) for i in range(13) for j in range(i + 1, 14)]
        report = graph.review_graph(catalog)
        self.assertEqual(len(report['edges']), 91)
        self.assertLess(len(json.dumps(report)), 25000)
        self.assertNotIn('paths', report)

    def test_actual_cli_review_and_explicit_rehearsal(self):
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(self.catalog), encoding='utf-8')
        reviewed = subprocess.run([sys.executable, '-m', 'migratelab.review', str(path), '--route', '2', '3', '5'], capture_output=True)
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.assertEqual(json.loads(reviewed.stdout)['selected_route'], [2, 3, 5])
        for index, (flags, expected) in enumerate(((['--graph', '--route', '2', '3', '5'], 0),
                                                  (['--graph'], 2), (['--graph', '--chain'], 2),
                                                  (['--route', '2', '5'], 2))):
            output = self.root / f'cli-{index}'
            result = subprocess.run([sys.executable, '-m', 'migratelab', str(self.source), str(path), str(output), *flags], capture_output=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            if expected == 2:
                self.assertFalse(output.exists())

    def test_review_cli_malformed_catalog_has_no_partial_edges(self):
        path = self.root / 'bad.json'
        for content in ('[{"from_version":2,"from_version":1,"to_version":3,"sql":"SELECT 1;"}]', '[NaN]', 'x' * 262145):
            path.write_text(content)
            result = subprocess.run([sys.executable, '-m', 'migratelab.review', str(path)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)['complete'])
            self.assertNotIn('edges', json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
