from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time


class MigrationError(Exception):
    """Invalid input or an incomplete rehearsal."""


def statements(sql: str) -> list[str]:
    """Use SQLite's own completeness parser, including triggers and quoted ';'."""
    if not isinstance(sql, str) or len(sql.encode("utf-8")) > 256 * 1024 or "\x00" in sql:
        raise MigrationError("SQL must be NUL-free UTF-8 text at most 256 KiB")
    result, buffer = [], []
    for char in sql:
        buffer.append(char)
        if char == ";":
            candidate = "".join(buffer)
            if sqlite3.complete_statement(candidate):
                result.append(candidate)
                buffer.clear()
    tail = "".join(buffer)
    # Only use comment stripping to identify an empty tail, never to execute SQL.
    if re.sub(r"--[^\n]*(?:\n|$)|/\*.*?\*/", "", tail, flags=re.S).strip():
        if not sqlite3.complete_statement(tail + ";"):
            raise MigrationError("SQL ends with an incomplete statement")
        result.append(tail)
    if not result:
        raise MigrationError("Migration must contain a statement")
    return result


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _snapshot(connection):
    objects = {f"{kind}:{name}": sql for kind, name, sql in connection.execute(
        "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")}
    tables = [name for (name,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {name: connection.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0] for name in tables}
    return {"schema": objects, "rows": counts}


def _authorizer(action, first, second, database, trigger):
    forbidden = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                 sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                 sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE}
    if action in forbidden or database == "temp":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(second).lower() in {"load_extension", "writefile", "readfile"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def preview(source: Path, output: Path, sql: str, *, preserve_tables=(), timeout=10.0) -> dict:
    """Backup a read-only source and migrate ONLY the new isolated copy.

    Existing outputs are never replaced. Failed migrations leave a rolled-back
    copy and report. Setup/I/O failures raise instead of claiming rollback.
    """
    source, output = Path(source), Path(output)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise MigrationError("Timeout must be finite and in (0, 300] seconds")
    if source.is_symlink() or not source.is_file():
        raise MigrationError("Source must be an existing regular SQLite database")
    if any(not isinstance(name, str) or not name for name in preserve_tables):
        raise MigrationError("Preserved table names must be nonempty strings")
    commands = statements(sql)
    deadline = time.monotonic() + timeout

    def expired():
        return time.monotonic() >= deadline

    def backup_progress(status, remaining, total):
        if expired():
            raise MigrationError("Backup timed out")

    try:
        output.mkdir(parents=True, exist_ok=False)
        copy_path = output / "preview.sqlite"
        uri = source.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as original, closing(
            sqlite3.connect(copy_path, isolation_level=None, timeout=1)
        ) as copy:
            original.backup(copy, pages=256, progress=backup_progress, sleep=0.01)
            copy.execute("PRAGMA foreign_keys=ON")
            copy.execute("PRAGMA trusted_schema=OFF")
            copy.set_progress_handler(lambda: int(expired()), 1000)
            before = _snapshot(copy)
            if set(preserve_tables) - set(before["rows"]):
                raise MigrationError("Preserve policy refers to an unknown table")
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or copy.execute("PRAGMA foreign_key_check").fetchone():
                raise MigrationError("Source snapshot fails integrity or foreign-key checks")
            report = {"version": 1, "passed": False, "rolled_back": False,
                      "migration_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                      "statements": len(commands), "before": before}
            copy.execute("BEGIN IMMEDIATE")
            attempted = before
            try:
                copy.set_authorizer(_authorizer)
                for index, command in enumerate(commands, 1):
                    copy.execute(command)
                    report["executed"] = index
                copy.set_authorizer(None)
                if copy.execute("PRAGMA foreign_key_check").fetchone():
                    raise MigrationError("Migration breaks foreign-key integrity")
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise MigrationError("Migration breaks database integrity")
                attempted = _snapshot(copy)
                if any(attempted["rows"].get(name) != before["rows"][name] for name in preserve_tables):
                    raise MigrationError("Preserved table row count changed or table removed")
                copy.execute("COMMIT")
                report["passed"] = True
            except (sqlite3.Error, MigrationError) as exc:
                copy.set_authorizer(None)
                copy.set_progress_handler(None, 0)
                if not copy.in_transaction:
                    raise MigrationError("Migration lost transaction; rollback cannot be guaranteed") from exc
                copy.execute("ROLLBACK")
                report.update(rolled_back=True, error=str(exc))
            finally:
                copy.set_authorizer(None)
                copy.set_progress_handler(None, 0)
            report["after"] = _snapshot(copy)
            report["schema_changes"] = {
                "added": sorted(set(attempted["schema"]) - set(before["schema"])),
                "removed": sorted(set(before["schema"]) - set(attempted["schema"])),
                "modified": sorted(key for key in before["schema"].keys() & attempted["schema"].keys()
                                   if before["schema"][key] != attempted["schema"][key]),
            }
        with (output / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
        return report
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError("Rehearsal setup or output failed; inspect the new output directory") from exc
