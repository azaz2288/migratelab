from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time


class MigrationError(Exception):
    """Invalid input or an incomplete rehearsal."""


def _publish_report(output: Path, report: dict):
    """Publish complete, fsynced bytes without replacing another writer.

    The temporary name is private to this invocation. Hardlink support is
    required: unsupported filesystems fail closed instead of using replace.
    This is not a directory-fsync or machine-power-loss durability guarantee.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".report-", suffix=".tmp",
                                         dir=output, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike replace/rename, link fails if the final name already exists.
        os.link(temporary, output / "report.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise MigrationError("JSON contains duplicate keys")
        value[key] = item
    return value


def _invalid_constant(value):
    raise MigrationError("JSON contains nonfinite literals")


def load_checks(path: Path):
    return _load_json(path)


def load_migrations(path: Path):
    """Read bounded JSON; preview_chain validates the declared edges."""
    return _load_json(path)


def _load_json(path: Path):
    try:
        with path.open("rb") as stream:
            content = stream.read(256 * 1024 + 1)
        if len(content) > 256 * 1024:
            raise MigrationError("JSON file exceeds 256 KiB limit")
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MigrationError("Invalid JSON") from exc


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
    return {"schema": objects, "rows": counts,
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0]}


def _data_digest(connection, table):
    """Hash a deterministic multiset of typed row values without retaining rows.

    SQLite orders every selected column. Equal rows serialize identically, so
    insertion order and rowid changes don't matter. Column layout is included.
    """
    cursor = connection.execute(f"SELECT * FROM {_quote(table)} LIMIT 0")
    columns = [item[0] for item in cursor.description]
    hasher = hashlib.sha256(json.dumps(columns, ensure_ascii=True).encode())
    # Explicit type and binary collation prevent equal numeric values or NOCASE
    # text ties from falling back to insertion order with different encodings.
    order = ",".join(f"typeof({_quote(name)}),{_quote(name)} COLLATE BINARY" for name in columns)
    for row in connection.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}"):
        values = []
        for value in row:
            if value is None:
                values.append(["null", None])
            elif isinstance(value, bytes):
                values.append(["blob", value.hex()])
            elif type(value) is float:
                values.append(["real", value.hex()])
            elif type(value) is int:
                values.append(["integer", str(value)])
            else:
                values.append(["text", value])
        encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()


def _read_only_authorizer(action, first, second, database, trigger):
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and str(second).lower() not in {"load_extension", "writefile", "readfile"}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorizer(action, first, second, database, trigger):
    forbidden = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                 sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                 sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE}
    if action in forbidden or database == "temp":
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(second).lower() in {"load_extension", "writefile", "readfile"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _validate_chain(migrations):
    if not isinstance(migrations, (list, tuple)) or not 1 <= len(migrations) <= 100:
        raise MigrationError("A chain must contain 1 to 100 declared migrations")
    steps, previous, total_bytes = [], None, 0
    for migration in migrations:
        if not isinstance(migration, dict) or set(migration) != {"from_version", "to_version", "sql"}:
            raise MigrationError("Each migration needs exactly from_version, to_version and sql")
        start, end, sql = (migration[key] for key in ("from_version", "to_version", "sql"))
        if any(type(value) is not int or not 0 <= value <= 2147483647 for value in (start, end)) or start >= end:
            raise MigrationError("Versions must be increasing integers in [0, 2147483647]")
        if previous is not None and start != previous:
            raise MigrationError("Migration chain has a gap, branch or repeated edge")
        commands = statements(sql)
        total_bytes += len(sql.encode('utf-8'))
        if total_bytes > 256 * 1024:
            raise MigrationError("Combined migration SQL exceeds 256 KiB limit")
        steps.append({"from_version": start, "to_version": end, "sql": sql, "commands": commands})
        previous = end
    return steps


def preview(source: Path, output: Path, sql: str, *, preserve_tables=(), preserve_data_tables=(), checks=(), timeout=10.0,
            from_version=None, to_version=None) -> dict:
    """Rehearse SQL, optionally requiring an explicit schema-version edge."""
    if from_version is not None or to_version is not None:
        steps = _validate_chain([{"from_version": from_version, "to_version": to_version, "sql": sql}])
    else:
        steps = [{"sql": sql, "commands": statements(sql)}]
    return _preview(source, output, steps, preserve_tables=preserve_tables,
                    preserve_data_tables=preserve_data_tables, checks=checks, timeout=timeout)


def preview_chain(source: Path, output: Path, migrations, *, preserve_tables=(), preserve_data_tables=(), checks=(), timeout=10.0) -> dict:
    """Rehearse a contiguous declared upgrade chain as one atomic transaction."""
    steps = _validate_chain(migrations)
    return _preview(source, output, steps, preserve_tables=preserve_tables,
                    preserve_data_tables=preserve_data_tables, checks=checks, timeout=timeout)


def _preview(source, output, steps, *, preserve_tables, preserve_data_tables, checks, timeout, graph_review=None):
    """Backup a read-only source and migrate ONLY the new isolated copy.

    Existing outputs are never replaced. Failed migrations leave a rolled-back
    copy and report. Setup/I/O failures raise instead of claiming rollback.
    """
    source, output = Path(source), Path(output)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise MigrationError("Timeout must be finite and in (0, 300] seconds")
    if source.is_symlink() or not source.is_file():
        raise MigrationError("Source must be an existing regular SQLite database")
    if any(not isinstance(name, str) or not name for name in (*preserve_tables, *preserve_data_tables)):
        raise MigrationError("Preserved table names must be nonempty strings")
    if not isinstance(checks, (list, tuple)) or len(checks) > 100:
        raise MigrationError("At most 100 scalar checks are allowed")
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"sql", "expected"} or len(statements(check["sql"])) != 1:
            raise MigrationError("Each check needs one SQL statement and expected scalar")
        expected = check["expected"]
        if type(expected) not in (str, int, float, type(None)) or (type(expected) is float and not math.isfinite(expected)):
            raise MigrationError("Check expected value must be a finite JSON scalar (not boolean)")
    versioned = 'from_version' in steps[0]
    # Single-SQL hashes retain their original meaning. Multi-step chains have
    # explicit boundaries so different step partitions cannot share a digest.
    migration_bytes = steps[0]['sql'].encode() if len(steps) == 1 else json.dumps(
        [{key: step[key] for key in ('from_version', 'to_version', 'sql')} for step in steps],
        ensure_ascii=True, separators=(',', ':')).encode()
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
            if set((*preserve_tables, *preserve_data_tables)) - set(before["rows"]):
                raise MigrationError("Preserve policy refers to an unknown table")
            preserved = {name: _data_digest(copy, name) for name in preserve_data_tables}
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or copy.execute("PRAGMA foreign_key_check").fetchone():
                raise MigrationError("Source snapshot fails integrity or foreign-key checks")
            report = {"version": 1, "passed": False, "rolled_back": False,
                      "migration_sha256": hashlib.sha256(migration_bytes).hexdigest(),
                      "statements": sum(len(step['commands']) for step in steps), "executed": 0,
                      "before": before, "preserved_data_sha256": preserved}
            if versioned:
                report['migration_chain'] = [{"from_version": step['from_version'], "to_version": step['to_version'],
                                             "sql_sha256": hashlib.sha256(step['sql'].encode()).hexdigest(),
                                             "executed": 0} for step in steps]
            if graph_review is not None:
                report['graph_review'] = graph_review
            copy.execute("BEGIN IMMEDIATE")
            attempted = before
            try:
                if versioned and before['user_version'] != steps[0]['from_version']:
                    raise MigrationError("Source snapshot user_version does not match the declared starting version")
                for step_index, step in enumerate(steps):
                    copy.set_authorizer(_authorizer)
                    for command in step['commands']:
                        copy.execute(command)
                        report['executed'] += 1
                        if versioned:
                            report['migration_chain'][step_index]['executed'] += 1
                    copy.set_authorizer(None)
                    if versioned:
                        # Values are validated integers; only controlled code may
                        # write this PRAGMA, inside the same rollback transaction.
                        copy.execute(f"PRAGMA user_version={step['to_version']}")
                copy.set_authorizer(None)
                if copy.execute("PRAGMA foreign_key_check").fetchone():
                    raise MigrationError("Migration breaks foreign-key integrity")
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise MigrationError("Migration breaks database integrity")
                attempted = _snapshot(copy)
                if any(attempted["rows"].get(name) != before["rows"][name] for name in preserve_tables):
                    raise MigrationError("Preserved table row count changed or table removed")
                if any(name not in attempted["rows"] or _data_digest(copy, name) != digest for name, digest in preserved.items()):
                    raise MigrationError("Preserved table column layout or typed data changed")
                report["checks"] = []
                copy.set_authorizer(_read_only_authorizer)
                for check in checks:
                    values = copy.execute(check["sql"]).fetchmany(2)
                    if len(values) != 1 or len(values[0]) != 1:
                        raise MigrationError("Invariant query must return exactly one row and one column")
                    actual = values[0][0]
                    passed = type(actual) is type(check["expected"]) and actual == check["expected"]
                    report["checks"].append({"sql_sha256": hashlib.sha256(check["sql"].encode()).hexdigest(), "passed": passed})
                    if not passed:
                        raise MigrationError("A declared scalar data invariant failed")
                copy.set_authorizer(None)
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
        _publish_report(output, report)
        return report
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError("Rehearsal setup or output failed; inspect the new output directory") from exc
