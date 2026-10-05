from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from migratelab.core import MigrationError, preview, statements


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
