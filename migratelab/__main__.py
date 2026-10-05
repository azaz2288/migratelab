import argparse
import json
from pathlib import Path
import sys
from .core import MigrationError, preview


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rehearse SQLite migration on a new isolated backup")
    parser.add_argument("source", type=Path)
    parser.add_argument("migration", type=Path)
    parser.add_argument("output", type=Path, help="A NEW output directory; never overwritten")
    parser.add_argument("--preserve-table", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        if args.migration.stat().st_size > 256 * 1024:
            raise MigrationError("Migration exceeds 256 KiB limit")
        report = preview(args.source, args.output, args.migration.read_text(encoding="utf-8-sig"),
                         preserve_tables=args.preserve_table, timeout=args.timeout)
    except (MigrationError, OSError, UnicodeError) as exc:
        print(json.dumps({"complete": False, "error": str(exc)}))
        return 2
    print(json.dumps({"complete": True, "passed": report["passed"],
                      "rolled_back": report["rolled_back"], "report": str(args.output / "report.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
