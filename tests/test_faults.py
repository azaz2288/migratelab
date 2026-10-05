"""Fault injection uses generated databases only; never exhausts host storage."""
from contextlib import closing, redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from migratelab import core
from migratelab.__main__ import main


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite"
        self.output = self.root / "output"
        self.sql = self.root / "migration.sql"
        self.sql.write_text("CREATE TABLE upgraded(x);", encoding="utf-8")
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("CREATE TABLE players(id INTEGER); INSERT INTO players VALUES(1);")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_preview(self, **options):
        return core.preview(self.source, self.output, self.sql.read_text(), **options)

    def assert_source_unchanged(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest)

    def assert_incomplete(self):
        self.assertFalse((self.output / "report.json").exists())
        self.assertTrue((self.output / "preview.sqlite").exists())
        self.assert_source_unchanged()

    def test_partial_json_write_never_publishes_final_report(self):
        def interrupted(report, stream, **options):
            stream.write('{"passed":')
            raise OSError(errno.ENOSPC, "synthetic output full")

        with patch.object(core.json, "dump", side_effect=interrupted):
            with self.assertRaises(core.MigrationError):
                self.run_preview()
        self.assert_incomplete()
        # Migration committed in the COPY before publication failed; do not
        # claim rollback or delete that copy as a publication cleanup step.
        with closing(sqlite3.connect(self.output / "preview.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM upgraded").fetchone(), (0,))
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["preview.sqlite"])

    def test_final_report_absent_until_serialization_complete(self):
        original_dump = json.dump
        observed = []

        def inspect(report, stream, **options):
            observed.append((self.output / "report.json").exists())
            original_dump(report, stream, **options)

        with patch.object(core.json, "dump", side_effect=inspect):
            expected = self.run_preview()
        self.assertEqual(observed, [False])
        self.assertEqual(json.loads((self.output / "report.json").read_text()), expected)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["preview.sqlite", "report.json"])
        self.assert_source_unchanged()

    def test_fsync_failure_never_publishes_report(self):
        with patch("os.fsync", side_effect=OSError(errno.EIO, "synthetic fsync error")):
            with self.assertRaises(core.MigrationError):
                self.run_preview()
        self.assert_incomplete()

    def test_competing_report_is_not_overwritten(self):
        original_dump = json.dump
        competitor = b'{"owner":"other-writer"}\n'

        def compete(report, stream, **options):
            original_dump(report, stream, **options)
            (self.output / "report.json").write_bytes(competitor)

        with patch.object(core.json, "dump", side_effect=compete):
            with self.assertRaises(core.MigrationError):
                self.run_preview()
        self.assertEqual((self.output / "report.json").read_bytes(), competitor)
        self.assert_source_unchanged()

    def test_report_failure_cli_is_exit_two_not_success_or_rollback(self):
        def interrupted(report, stream, **options):
            stream.write("{")
            raise OSError(errno.ENOSPC, "synthetic full")

        stdout = io.StringIO()
        with patch.object(core.json, "dump", side_effect=interrupted), redirect_stdout(stdout):
            result = main([str(self.source), str(self.sql), str(self.output)])
        self.assertEqual(result, 2)
        message = json.loads(stdout.getvalue())
        self.assertIs(message["complete"], False)
        self.assertNotIn("rolled_back", message)
        self.assert_incomplete()

    def test_unsupported_hardlinks_fail_closed(self):
        with patch("os.link", side_effect=OSError(errno.ENOTSUP, "synthetic unsupported link")):
            with self.assertRaises(core.MigrationError):
                self.run_preview()
        self.assert_incomplete()
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["preview.sqlite"])

    def test_link_readers_see_complete_report_after_fsync(self):
        real_link, real_fsync = os.link, os.fsync
        events = []

        def synced(fd):
            real_fsync(fd)
            events.append("synced")

        def publish(source, destination):
            self.assertEqual(events, ["synced"])
            self.assertFalse(Path(destination).exists())
            real_link(source, destination)
            events.append(json.loads(Path(destination).read_text())["passed"])

        with patch("os.fsync", side_effect=synced), patch("os.link", side_effect=publish):
            self.assertTrue(self.run_preview()["passed"])
        self.assertEqual(events, ["synced", True])
        self.assert_source_unchanged()

    def test_rolled_back_report_has_same_atomic_publication(self):
        original_dump = json.dump
        observed = []

        def inspect(report, stream, **options):
            observed.append((self.output / "report.json").exists())
            original_dump(report, stream, **options)

        with patch.object(core.json, "dump", side_effect=inspect):
            report = core.preview(self.source, self.output, "DELETE FROM players;",
                                  preserve_tables=["players"])
        self.assertEqual(observed, [False])
        self.assertFalse(report["passed"])
        self.assertTrue(report["rolled_back"])
        self.assertEqual(json.loads((self.output / "report.json").read_text()), report)
        self.assert_source_unchanged()

    def test_real_copy_lock_is_setup_failure_not_certified_rollback(self):
        connect = sqlite3.connect
        lockers = []

        class ContendedCopy(sqlite3.Connection):
            def execute(copy, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    locker = connect(self.output / "preview.sqlite")
                    lockers.append(locker)
                    locker.execute("BEGIN IMMEDIATE")
                return super().execute(sql, parameters)

        def factory(database, **options):
            if isinstance(database, Path):
                options["factory"] = ContendedCopy
            return connect(database, **options)

        try:
            with patch.object(core.sqlite3, "connect", side_effect=factory):
                with self.assertRaises(core.MigrationError) as raised:
                    self.run_preview()
            self.assertEqual(raised.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        finally:
            for locker in lockers:
                locker.rollback()
                locker.close()
        self.assert_incomplete()

    def test_real_source_exclusive_lock_then_fresh_output_retry(self):
        with closing(sqlite3.connect(self.source)) as locker:
            locker.execute("BEGIN EXCLUSIVE")
            with self.assertRaisesRegex(core.MigrationError, "Backup timed out"):
                self.run_preview(timeout=0.03)
            locker.rollback()
        self.assert_incomplete()
        with self.assertRaises(core.MigrationError):
            self.run_preview()  # never reuse even an incomplete directory
        report = core.preview(self.source, self.root / "retry", self.sql.read_text())
        self.assertTrue(report["passed"])
        self.assert_source_unchanged()

    def test_multichunk_backup_interruption_leaves_no_report(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("CREATE TABLE large(x); INSERT INTO large VALUES(zeroblob(2097152));")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        connect = sqlite3.connect
        remaining_pages = []

        class InterruptedBackup(sqlite3.Connection):
            def backup(self, target, *, pages, progress, sleep):
                def interrupt(status, remaining, total):
                    remaining_pages.append(remaining)
                    progress(status, remaining, total)
                    raise OSError(errno.EIO, "synthetic backup interruption")
                return super().backup(target, pages=pages, progress=interrupt, sleep=sleep)

        def factory(database, **options):
            return connect(database, factory=InterruptedBackup, **options)

        with patch.object(core.sqlite3, "connect", side_effect=factory):
            with self.assertRaises(core.MigrationError):
                self.run_preview()
        self.assertGreater(remaining_pages[0], 0)  # not merely after backup
        self.assert_incomplete()
        self.assertTrue(core.preview(self.source, self.root / "retry", self.sql.read_text())["passed"])

    def test_real_sqlite_full_auto_rollback_is_not_falsely_certified(self):
        connect = sqlite3.connect

        class LimitedCopy(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    pages = super().execute("PRAGMA page_count").fetchone()[0]
                    super().execute(f"PRAGMA max_page_count={pages}")
                return super().execute(sql, parameters)

        def factory(database, **options):
            if isinstance(database, Path):
                options["factory"] = LimitedCopy
            return connect(database, **options)

        with patch.object(core.sqlite3, "connect", side_effect=factory):
            with self.assertRaisesRegex(core.MigrationError, "lost transaction") as raised:
                core.preview(self.source, self.output,
                             "INSERT INTO players VALUES(2); INSERT INTO players VALUES(zeroblob(1048576));",
                             from_version=0, to_version=1)
        self.assertEqual(raised.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assert_incomplete()
        with closing(connect(self.output / "preview.sqlite")) as db:
            self.assertEqual(db.execute("SELECT * FROM players").fetchall(), [(1,)])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
