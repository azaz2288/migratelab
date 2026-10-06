import argparse
import json
from pathlib import Path
import sys
from .core import MigrationError, load_checks, load_migrations, preview, preview_chain
from .graph import preview_graph


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
    parser.add_argument('--graph', action='store_true', help='Read a declared edge catalog; requires an explicit --route')
    parser.add_argument('--route', type=int, nargs='+', help='Complete version sequence for --graph, never inferred')
    parser.add_argument('--expect-graph-sha256', help='With --graph, require the exact previously reviewed catalog digest')
    args = parser.parse_args(argv)
    try:
        if args.graph and (args.chain or args.from_version is not None or args.to_version is not None):
            raise MigrationError('--graph cannot combine with --chain or single-edge version flags')
        if (args.graph and args.route is None) or (not args.graph and args.route is not None):
            raise MigrationError('--graph requires an explicit --route; --route is only valid with --graph')
        if args.expect_graph_sha256 is not None and not args.graph:
            raise MigrationError('--expect-graph-sha256 is only valid with --graph')
        if args.chain and (args.from_version is not None or args.to_version is not None):
            raise MigrationError("--chain cannot be combined with single-edge version flags")
        if args.migration.stat().st_size > 256 * 1024:
            raise MigrationError("Migration exceeds 256 KiB limit")
        checks = []
        if args.checks:
            checks = load_checks(args.checks)
        options = dict(preserve_tables=args.preserve_table, preserve_data_tables=args.preserve_data_table,
                       checks=checks, timeout=args.timeout)
        if args.graph:
            report = preview_graph(args.source, args.output, load_migrations(args.migration), route=args.route,
                                   expected_graph_sha256=args.expect_graph_sha256, **options)
        elif args.chain:
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
