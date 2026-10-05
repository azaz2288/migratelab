import argparse
import json
from pathlib import Path
import sys
from .core import MigrationError, load_checks, load_migrations, preview, preview_chain


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rehearse SQLite migration on a new isolated backup")
    parser.add_argument("source", type=Path)
    parser.add_argument("migration", type=Path)
    parser.add_argument("output", type=Path, help="A NEW output directory; never overwritten")
    parser.add_argument("--preserve-table", action="append", default=[])
    parser.add_argument("--preserve-data-table", action="append", default=[], help="Require identical column layout and typed rows")
    parser.add_argument("--checks", type=Path, help="JSON list of read-only SQL checks with expected scalar values")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--from-version", type=int, help="Expected source snapshot PRAGMA user_version")
    parser.add_argument("--to-version", type=int, help="Target user_version, changed only in the copy")
    parser.add_argument("--chain", action="store_true", help="Read migration as a JSON list of versioned SQL edges")
    args = parser.parse_args(argv)
    try:
        if args.chain and (args.from_version is not None or args.to_version is not None):
            raise MigrationError("--chain cannot be combined with single-edge version flags")
        if args.migration.stat().st_size > 256 * 1024:
            raise MigrationError("Migration exceeds 256 KiB limit")
        checks = []
        if args.checks:
            checks = load_checks(args.checks)
        options = dict(preserve_tables=args.preserve_table, preserve_data_tables=args.preserve_data_table,
                       checks=checks, timeout=args.timeout)
        if args.chain:
            report = preview_chain(args.source, args.output, load_migrations(args.migration), **options)
        else:
            report = preview(args.source, args.output, args.migration.read_text(encoding="utf-8-sig"),
                             from_version=args.from_version, to_version=args.to_version, **options)
    except (MigrationError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"complete": False, "error": str(exc)}))
        return 2
    print(json.dumps({"complete": True, "passed": report["passed"],
                      "rolled_back": report["rolled_back"], "report": str(args.output / "report.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
