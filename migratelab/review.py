"""Standalone metadata-only graph CLI; no source database argument."""
import argparse
import json
from pathlib import Path

from .core import MigrationError, load_migrations
from .graph import review_graph


def main(argv=None):
    parser = argparse.ArgumentParser(description='Review declared migration edges without executing SQL or opening a database')
    parser.add_argument('catalog', type=Path)
    parser.add_argument('--route', type=int, nargs='+', help='Validate an explicit version sequence, never infer a route')
    args = parser.parse_args(argv)
    try:
        report = review_graph(load_migrations(args.catalog), route=args.route)
    except (MigrationError, OSError, UnicodeError, ValueError):
        # Do not echo malformed catalog content or SQL through diagnostic strings.
        print(json.dumps({'complete': False, 'error': 'Invalid or unreadable migration graph or explicit route'}))
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
