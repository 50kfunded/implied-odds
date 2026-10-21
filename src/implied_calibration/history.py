"""Retained hourly BTC perpetual candles respect snapshot-time availability."""

from collections import Counter
from pathlib import Path
import sqlite3
import time
from typing import Callable

import numpy as np

from .client import PublicClient, validate_response
from .raw import RawWriter, read_raw
from .store import canonical, digest, evaluation_store

HOUR_MS = 3600000
ENDPOINT = 'public/get_tradingview_chart_data'


def fetch_history(client: PublicClient, start_ms: int, end_ms: int, *,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    if (type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms
            or start_ms % HOUR_MS or end_ms % HOUR_MS):
        raise ValueError('Expected an increasing hour-aligned history interval')
    pages = no_data = 0
    for start in range(start_ms, end_ms, 720 * HOUR_MS):
        if pages:
            sleep(10.)
        stop = min(end_ms, start + 720 * HOUR_MS)
        result = client.request(ENDPOINT, {'instrument_name': 'BTC-PERPETUAL', 'resolution': '60',
                                          'start_timestamp': start, 'end_timestamp': stop - 1}).result
        pages += 1
        no_data += result['status'] == 'no_data'
    return {'status': 'ok', 'pages': pages, 'no_data_pages': no_data}


def capture_history(output: Path, start_ms: int, end_ms: int) -> dict:
    writer = RawWriter(Path(output) / 'raw')
    client = PublicClient(on_attempt=lambda attempt: writer.append(attempt.to_dict()))
    try:
        return fetch_history(client, start_ms, end_ms)
    finally:
        client.close()


def ingest_history(directory: Path, db: Path, as_of_ms: int) -> dict:
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError('Expected nonnegative UTC milliseconds')
    paths = sorted(Path(directory).rglob('*.jsonl.gz'))
    if not paths:
        raise ValueError('Expected retained hourly history pages')
    store = evaluation_store(db)
    reasons: Counter = Counter()
    added = pages = unchanged = 0
    try:
        with store.connection:
            for path in paths:
                for row in read_raw(path, on_issue=lambda issue: reasons.update([issue['reason']])):
                    if row.get('endpoint') != ENDPOINT:
                        reasons['other_endpoint'] += 1
                        continue
                    stamp, params = row.get('fetched_at_ms'), row.get('params')
                    if (type(stamp) is not int or stamp < 0 or not isinstance(params, dict)
                            or set(params) != {'instrument_name', 'resolution', 'start_timestamp', 'end_timestamp'}
                            or params['instrument_name'] != 'BTC-PERPETUAL' or params['resolution'] != '60'
                            or any(type(params[k]) is not int for k in ('start_timestamp', 'end_timestamp'))
                            or not 0 <= params['start_timestamp'] < params['end_timestamp']):
                        raise ValueError('Invalid retained hourly request')
                    if stamp > as_of_ms:
                        reasons['future_page'] += 1
                        continue
                    if not row.get('ok'):
                        reasons['failed_attempt'] += 1
                        continue
                    result = validate_response(ENDPOINT, row.get('http_status'), row.get('body'))
                    source = digest(row)
                    if store.connection.execute('SELECT 1 FROM history_sources WHERE source_id=?', (source,)).fetchone() is None:
                        store.connection.execute('INSERT INTO history_sources VALUES (?, ?, ?, ?)',
                                                 (source, stamp, canonical(params), digest(row['body'])))
                    pages += 1
                    if result['status'] == 'no_data':
                        reasons['no_data'] += 1
                        continue
                    for opened, close in zip(result['ticks'], result['close']):
                        if not params['start_timestamp'] <= opened <= params['end_timestamp']:
                            raise ValueError('Hourly candle falls outside its retained request')
                        ended = opened + HOUR_MS
                        if ended > stamp:
                            reasons['unfinished_candle'] += 1
                            continue
                        key = (opened, stamp, source)
                        old = store.connection.execute('SELECT close_usd FROM hourly_observations '
                                                       'WHERE open_ms=? AND fetched_at_ms=? AND source_id=?', key).fetchone()
                        if old is not None:
                            if old[0] != close:
                                raise ValueError('Conflicting immutable hourly observation')
                            unchanged += 1
                            continue
                        store.connection.execute('INSERT INTO hourly_observations VALUES (?, ?, ?, ?, ?)',
                                                 (opened, stamp, ended, close, source))
                        added += 1
    finally:
        store.close()
    return {'status': 'ok', 'as_of_ms': as_of_ms, 'pages': pages, 'added': added,
            'unchanged': unchanged, 'reason_counts': dict(sorted(reasons.items()))}


def trailing_returns(connection: sqlite3.Connection, cutoff_ms: int, hours: int, max_age_ms: int) -> dict:
    rows = connection.execute('SELECT * FROM hourly_observations WHERE close_ms<=? AND fetched_at_ms<=? '
                              'ORDER BY close_ms DESC, fetched_at_ms DESC, source_id', (cutoff_ms, cutoff_ms)).fetchall()
    by_close: dict[int, list] = {}
    for row in rows:
        by_close.setdefault(row['close_ms'], []).append(row)
    times = sorted(by_close, reverse=True)[:hours + 1]
    if len(times) < hours + 1:
        return {'status': 'short_history', 'available_closes': len(times), 'required_closes': hours + 1}
    if cutoff_ms - times[0] > max_age_ms:
        return {'status': 'stale_history', 'latest_close_ms': times[0]}
    times.reverse()
    if any(b - a != HOUR_MS for a, b in zip(times, times[1:])):
        return {'status': 'history_gap'}
    if any(len({r['close_usd'] for r in by_close[t]}) > 1 for t in times):
        return {'status': 'revised_history'}
    selected = [by_close[t][0] for t in times]
    assert all(r['fetched_at_ms'] <= cutoff_ms and r['close_ms'] <= cutoff_ms for r in selected)
    prices = np.array([r['close_usd'] for r in selected], dtype=float)
    if not np.isfinite(prices).all() or np.any(prices <= 0):
        return {'status': 'invalid_history_price'}
    return {'status': 'ok', 'returns': np.diff(np.log(prices)), 'first_close_ms': times[0],
            'last_close_ms': times[-1], 'cutoff_ms': cutoff_ms,
            'history_hash': digest([dict(r) for r in selected]), 'n_returns': hours}
