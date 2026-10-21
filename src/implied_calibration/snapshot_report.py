"""Exploratory quote-fit diagnostics remain separate from retrospective forecasts."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import matplotlib as mpl

from .cleaning import clean_database
from .distributions import cdf_database
from .history import HOUR_MS, trailing_returns
from .plotting import smile_figure
from .protocol import Protocol
from .svi import FitResult, SVIParams


def snapshot_report(db: Path, protocol: Protocol, output: Path, git_commit: str | None,
                    as_of_ms: int | None = None, *, working_tree_dirty: bool | None = None) -> dict:
    connection = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cutoff = as_of_ms if as_of_ms is not None else connection.execute(
            'SELECT MAX(fetched_at_ms) FROM snapshots').fetchone()[0]
        if type(cutoff) is not int or cutoff < 0:
            raise ValueError('Expected a retained snapshot and an integer UTC cutoff')
        snapshot = connection.execute('SELECT snapshot_id, fetched_at_ms FROM snapshots WHERE fetched_at_ms<=? '
                                      'ORDER BY fetched_at_ms DESC, snapshot_id LIMIT 1', (cutoff,)).fetchone()
        if snapshot is None:
            raise ValueError('No snapshot exists at this cutoff')
        assert snapshot['fetched_at_ms'] <= cutoff
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        history = trailing_returns(connection, snapshot['fetched_at_ms'], 2160, 2 * HOUR_MS) if 'hourly_observations' in tables else {'status': 'short_history'}
        closes = connection.execute('SELECT COUNT(DISTINCT close_ms) FROM hourly_observations '
                                     'WHERE close_ms<=? AND fetched_at_ms<=?', (snapshot['fetched_at_ms'],) * 2).fetchone()[0] if 'hourly_observations' in tables else 0
    finally:
        connection.close()
    cleaned = clean_database(db, protocol, cutoff, snapshot_id=snapshot['snapshot_id'])
    fitted = cdf_database(db, protocol, cutoff, snapshot_id=snapshot['snapshot_id'])
    groups = fitted['groups']
    eligible = sorted((g for g in groups if g['cdf_status'] == 'ok'), key=lambda g: g['expiry_ms'])
    chosen = eligible[0] if eligible else None
    result = {'kind': 'exploratory_market_snapshot', 'synthetic': False, 'as_of_ms': cutoff,
              'snapshot_id': snapshot['snapshot_id'], 'fetched_at_ms': snapshot['fetched_at_ms'],
              'git_commit': git_commit, 'protocol_hash': protocol.content_hash,
              'working_tree_dirty': working_tree_dirty,
              'n_quotes': cleaned['n_quotes'], 'n_usable_quotes': cleaned['n_usable_quotes'],
              'n_expiry_groups': len(groups), 'n_accepted_cdfs': len(eligible),
              'fit_status_counts': fitted['fit_status_counts'], 'cdf_status_counts': fitted['cdf_status_counts'],
              'group_status_counts': cleaned['group_status_counts'], 'reason_counts': cleaned['reason_counts'],
              'iv_diagnostic': cleaned['iv_diagnostic'], 'n_completed_hourly_closes_available': closes,
              'garch_history_status': history['status'],
              'note': "Exploratory fits use real quotes; these are neither frozen research forecasts nor measured calibration outcomes.",
              'selection_rule': 'earliest expiry with accepted CDF in the latest retained snapshot',
              'selected_expiry_ms': chosen['expiry_ms'] if chosen else None,
              'groups': groups}
    output.mkdir(parents=True, exist_ok=True)
    if chosen is None:
        (output / 'market-smile.svg').unlink(missing_ok=True)
    if chosen is not None:
        clean = next(g for g in cleaned['groups'] if g['expiry_ms'] == chosen['expiry_ms'])
        rows = [q for q in clean['quotes'] if q['status'] == 'usable']
        fit_fields = {**chosen['fit'], 'params': SVIParams(**chosen['fit']['params'])}
        expiry = datetime.fromtimestamp(chosen['expiry_ms'] / 1000, timezone.utc).strftime('%d %b %Y %H:%M UTC')
        captured = datetime.fromtimestamp(snapshot['fetched_at_ms'] / 1000, timezone.utc).strftime('%d %b %Y %H:%M UTC')
        with mpl.rc_context():
            mpl.rcdefaults()
            mpl.rcParams['svg.hashsalt'] = 'implied-odds-market-snapshot'
            mpl.rcParams['svg.fonttype'] = 'none'
            figure = smile_figure([q['k'] for q in rows], [q['iv_bid'] for q in rows], [q['iv_ask'] for q in rows],
                                  chosen['t_years'], FitResult(**fit_fields), synthetic=False,
                                  label='Real Deribit quotes: exploratory SVI fit\nExpiry ' + expiry)
            figure.axes[0].text(0.02, 0.96, 'Captured ' + captured, transform=figure.axes[0].transAxes,
                                fontsize=8, va='top')
            figure.savefig(output / 'market-smile.svg', metadata={'Date': None, 'Creator': 'implied-odds'})
            figure.clear()
        result['figure_sha256'] = hashlib.sha256((output / 'market-smile.svg').read_bytes()).hexdigest()
    (output / 'snapshot.json').write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n',
                                         encoding='utf-8', newline='\n')
    return result
