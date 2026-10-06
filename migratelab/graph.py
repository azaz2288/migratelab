"""Bounded migration catalog review, never automatic upgrade routing."""
import hashlib
import json
import re

from .core import MigrationError, _preview, _validate_chain


def _catalog(migrations):
    if not isinstance(migrations, (list, tuple)) or not 1 <= len(migrations) <= 100:
        raise MigrationError('A graph must contain 1 to 100 declared edges')
    edges, seen, total = [], set(), 0
    for migration in migrations:
        # Reuse strict single-edge SQL/version validation, not chain ordering.
        step = _validate_chain([migration])[0]
        identity = (step['from_version'], step['to_version'])
        if identity in seen:
            raise MigrationError('Duplicate graph edge; select one explicit SQL per version pair')
        seen.add(identity)
        total += len(step['sql'].encode('utf-8'))
        if total > 256 * 1024:
            raise MigrationError('Combined graph SQL exceeds 256 KiB limit')
        edges.append(step)
    return sorted(edges, key=lambda item: (item['from_version'], item['to_version']))


def _route(edges, route):
    if not isinstance(route, (list, tuple)) or not 2 <= len(route) <= 101:
        raise MigrationError('An explicit route needs 2 to 101 version integers')
    if any(type(version) is not int or not 0 <= version <= 2147483647 for version in route):
        raise MigrationError('Route versions must be integers in [0, 2147483647]')
    mapping = {(edge['from_version'], edge['to_version']): edge for edge in edges}
    selected = []
    for start, end in zip(route, route[1:]):
        if start >= end or (start, end) not in mapping:
            raise MigrationError('Route contains a missing or non-increasing declared edge')
        selected.append(mapping[start, end])
    return selected


def _review(edges, route):
    plain = [{key: edge[key] for key in ('from_version', 'to_version', 'sql')} for edge in edges]
    digest = hashlib.sha256(json.dumps(plain, ensure_ascii=True, separators=(',', ':'),
                                       sort_keys=True).encode()).hexdigest()
    versions = sorted({edge[key] for edge in edges for key in ('from_version', 'to_version')})
    outgoing = {version: [] for version in versions}
    incoming = set()
    for edge in edges:
        outgoing[edge['from_version']].append(edge['to_version'])
        incoming.add(edge['to_version'])
    report = {'version': 1, 'complete': True, 'scope': 'declared-migration-graph-no-database-read',
              'graph_sha256': digest, 'versions': versions,
              'roots': [version for version in versions if version not in incoming],
              'sinks': [version for version in versions if not outgoing[version]],
              'branches': [{'version': version, 'to_versions': targets}
                           for version, targets in outgoing.items() if len(targets) > 1],
              'edges': [{'from_version': edge['from_version'], 'to_version': edge['to_version'],
                         'sql_sha256': hashlib.sha256(edge['sql'].encode()).hexdigest(),
                         'statements': len(edge['commands'])} for edge in edges]}
    if route is not None:
        _route(edges, route)
        report['selected_route'] = list(route)
    return report


def review_graph(migrations, *, route=None):
    """Return metadata only; no SQL execution, DB access or path enumeration."""
    return _review(_catalog(migrations), route)


def preview_graph(source, output, migrations, *, route, preserve_tables=(),
                  preserve_data_tables=(), checks=(), timeout=10.0, expected_graph_sha256=None):
    """Execute only an explicitly selected route on an isolated SQLite backup."""
    edges = _catalog(migrations)
    selected = _route(edges, route)
    review = _review(edges, route)
    if expected_graph_sha256 is not None:
        if not isinstance(expected_graph_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_graph_sha256):
            raise MigrationError('Expected graph SHA256 must be 64 lowercase hex characters')
        if expected_graph_sha256 != review['graph_sha256']:
            raise MigrationError('Migration graph differs from the explicitly reviewed SHA256')
    return _preview(source, output, selected, preserve_tables=preserve_tables,
                    preserve_data_tables=preserve_data_tables, checks=checks, timeout=timeout,
                    graph_review=review)
