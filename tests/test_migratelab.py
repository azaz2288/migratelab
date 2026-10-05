from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from migratelab.core import MigrationError, load_checks, preview, statements


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.executescript("CREATE TABLE players(id INTEGER PRIMARY KEY, name TEXT UNIQUE); INSERT INTO players VALUES(1, 'mage');")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def check_original(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def run_preview(self, sql, **kwargs):
        return preview(self.source, self.root / "preview", sql, **kwargs)

    def test_schema_change_only_in_copy(self):
        report = self.run_preview("ALTER TABLE players ADD COLUMN level INTEGER NOT NULL DEFAULT 1;", preserve_tables=["players"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["schema_changes"]["modified"], ["table:players"])
        with closing(sqlite3.connect(self.root / "preview/preview.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT level FROM players").fetchone()[0], 1)
        self.check_original()

    def test_failed_second_statement_rolls_back_first(self):
        report = self.run_preview("ALTER TABLE players ADD COLUMN level INTEGER; INSERT INTO missing VALUES(1);")
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])
        self.assertEqual(report["before"], report["after"])
        self.check_original()

    def test_preserve_rows_policy_rolls_back_deletion(self):
        report = self.run_preview("DELETE FROM players;", preserve_tables=["players"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["after"]["rows"], {"players": 1})

    def test_preserve_data_rejects_equal_row_count_value_change(self):
        report = self.run_preview("UPDATE players SET name='erased';", preserve_data_tables=["players"])
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])
        with closing(sqlite3.connect(self.root / "preview/preview.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT name FROM players").fetchone()[0], "mage")
        self.check_original()

    def test_preserve_data_rejects_column_layout_change(self):
        report = self.run_preview("ALTER TABLE players ADD COLUMN level INTEGER;", preserve_data_tables=["players"])
        self.assertFalse(report["passed"])

    def test_preserved_typed_blob_and_values_are_deterministic(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.executescript("CREATE TABLE typed(value); INSERT INTO typed VALUES(1),(1.0),('1'),(NULL),(X'0102');")
            connection.commit()
        report = self.run_preview("CREATE TABLE new_table(x);", preserve_data_tables=["typed"])
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["preserved_data_sha256"]["typed"]), 64)

    def test_preserve_digest_is_stable_across_nocase_and_numeric_tie_reordering(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.executescript("CREATE TABLE mixed(value COLLATE NOCASE); INSERT INTO mixed VALUES('a'),('A'),(1),(1.0);")
            connection.commit()
        sql = "CREATE TABLE staging(value); INSERT INTO staging SELECT value FROM mixed ORDER BY rowid DESC; DELETE FROM mixed; INSERT INTO mixed SELECT value FROM staging; DROP TABLE staging;"
        report = self.run_preview(sql, preserve_data_tables=["mixed"])
        self.assertTrue(report["passed"], report)

    def test_scalar_invariant_accepts_required_upgrade_state(self):
        report = self.run_preview("ALTER TABLE players ADD COLUMN level INTEGER DEFAULT 1;",
                                  checks=[{"sql": "SELECT count(*) FROM players WHERE level=1", "expected": 1}])
        self.assertTrue(report["passed"])
        self.assertTrue(report["checks"][0]["passed"])

    def test_failed_scalar_invariant_rolls_back(self):
        report = self.run_preview("UPDATE players SET name='wrong';",
                                  checks=[{"sql": "SELECT name FROM players", "expected": "mage"}])
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])

    def test_checks_are_read_only_and_reject_multicolumn_results(self):
        for number, query in enumerate(("DELETE FROM players RETURNING id", "SELECT 1,2", "SELECT id FROM players WHERE 0")):
            with self.subTest(query=query):
                report = preview(self.source, self.root / f"check-{number}", "SELECT 1;", checks=[{"sql": query, "expected": 1}])
                self.assertFalse(report["passed"])
                self.assertEqual(report["after"]["rows"]["players"], 1)

    def test_malformed_checks_rejected_before_output(self):
        for candidate in ("wrong", [{}], [{"sql": "SELECT 1;SELECT 2;", "expected": 1}],
                          [{"sql": "SELECT 1;", "expected": True}], [{"sql": "SELECT 1;", "expected": float("nan")}]):
            with self.subTest(candidate=candidate), self.assertRaises(MigrationError):
                self.run_preview("SELECT 1;", checks=candidate)
            self.assertFalse((self.root / "preview").exists())

    def test_checks_json_rejects_duplicates_nonfinite_and_large_inputs(self):
        path = self.root / "checks.json"
        for content in ('[{"sql":"SELECT 1","expected":1,"expected":2}]', '[{"sql":"SELECT 1","expected":NaN}]', "x" * (256 * 1024 + 1)):
            path.write_text(content)
            with self.assertRaises(MigrationError):
                load_checks(path)

    def test_cli_declared_check_and_preserve_data(self):
        path = self.root / "checks.json"
        path.write_text(json.dumps([{"sql":"SELECT count(*) FROM players","expected":1}]))
        sql = self.root / "sql.txt"
        sql.write_text("CREATE TABLE metadata(x);")
        result = subprocess.run([sys.executable, "-m", "migratelab", str(self.source), str(sql), str(self.root / "cli"),
                                 "--checks", str(path), "--preserve-data-table", "players"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads((self.root / "cli/report.json").read_text())["checks"][0]["passed"])

    def test_unknown_preserve_table_is_setup_error(self):
        with self.assertRaises(MigrationError):
            self.run_preview("SELECT 1;", preserve_tables=["unknown"])

    def test_unique_constraint_failure(self):
        report = self.run_preview("INSERT INTO players VALUES(2,'mage');")
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])

    def test_foreign_keys_enabled(self):
        report = self.run_preview("CREATE TABLE items(owner INTEGER REFERENCES players(id)); INSERT INTO items VALUES(99);")
        self.assertFalse(report["passed"])
        self.assertNotIn("items", report["after"]["rows"])

    def test_explicit_transactions_attach_pragma_and_vacuum_denied(self):
        for number, sql in enumerate(("COMMIT;", "ROLLBACK;", "SAVEPOINT external;", "ATTACH ':memory:' AS other;",
                                      "PRAGMA writable_schema=ON;", "VACUUM;", "CREATE TEMP TABLE leak(x);")):
            with self.subTest(sql=sql):
                report = preview(self.source, self.root / f"blocked-{number}", sql)
                self.assertFalse(report["passed"])
                self.assertTrue(report["rolled_back"])
                self.check_original()

    def test_trigger_and_quoted_semicolon_split_correctly(self):
        sql = """CREATE TABLE log(value TEXT);
        CREATE TRIGGER changed AFTER INSERT ON players BEGIN
          INSERT INTO log VALUES('hello; world');
          INSERT INTO log VALUES('second');
        END;
        INSERT INTO players VALUES(2, 'warrior'); -- trailing comment
        """
        self.assertEqual(len(statements(sql)), 3)
        report = self.run_preview(sql)
        self.assertTrue(report["passed"])
        self.assertEqual(report["after"]["rows"]["log"], 2)

    def test_empty_and_incomplete_sql_rejected(self):
        for sql in ("", "-- comment only", "SELECT 'unterminated", "SELECT 1\x00;", "x" * (256 * 1024 + 1)):
            with self.subTest(sql=sql[:30]), self.assertRaises(MigrationError):
                statements(sql)

    def test_final_statement_without_semicolon(self):
        self.assertTrue(self.run_preview("SELECT 1")["passed"])

    def test_existing_output_never_overwritten(self):
        target = self.root / "preview"
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_text("keep")
        with self.assertRaises(MigrationError):
            self.run_preview("SELECT 1;")
        self.assertEqual(sentinel.read_text(), "keep")

    def test_missing_source_never_created(self):
        missing = self.root / "missing.sqlite"
        with self.assertRaises(MigrationError):
            preview(missing, self.root / "preview", "SELECT 1;")
        self.assertFalse(missing.exists())

    def test_invalid_source_database(self):
        self.source.write_bytes(b"not a database")
        with self.assertRaises(MigrationError):
            self.run_preview("SELECT 1;")

    def test_timeout_rolls_back_expensive_recursive_query(self):
        report = self.run_preview("INSERT INTO players VALUES(2,'rogue'); WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n;", timeout=0.1)
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])
        self.assertEqual(report["after"]["rows"]["players"], 1)

    def test_invalid_timeouts(self):
        for timeout in (0, -1, float("nan"), float("inf"), True, 301):
            with self.subTest(timeout=timeout), self.assertRaises(MigrationError):
                self.run_preview("SELECT 1;", timeout=timeout)

    def test_wal_backup_includes_uncheckpointed_commits(self):
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO players VALUES(2,'paladin')")
            connection.commit()
            report = self.run_preview("SELECT 1;")
            self.assertEqual(report["before"]["rows"]["players"], 2)

    def test_cli_success_failure_and_invalid_input(self):
        sql = self.root / "migration.sql"
        for number, (content, expected) in enumerate((("SELECT 1;", 0), ("INSERT INTO missing VALUES(1);", 1), ("", 2))):
            sql.write_text(content, encoding="utf-8")
            result = subprocess.run([sys.executable, "-m", "migratelab", str(self.source), str(sql), str(self.root / f"cli-{number}")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
