"""I classify every stored quote and inspect IV conventions using captured data."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np

from .blackscholes import IVResult, implied_vol
from .instruments import finite_number
from .protocol import Protocol, validate_protocol


@dataclass(frozen=True)
class CleanQuote:
    snapshot_id: str
    instrument_name: str
    expiry_ms: int
    strike: float
    option_type: str
    fetched_at_ms: int
    metadata_fetched_at_ms: int | None
    forward_usd: float | None
    quote_forward_usd: float | None
    forward_relative_deviation: float | None
    t_years: float
    k: float | None
    bid_btc: float | None
    ask_btc: float | None
    mid_btc: float | None
    tick_btc: float | None
    spread_to_mid: float | None
    iv_bid: float | None
    iv_ask: float | None
    iv_mid: float | None
    iv_bid_reason: str
    iv_ask_reason: str
    iv_mid_reason: str
    iv_mark_price: float | None
    iv_mark_reason: str
    mark_iv_pct: float | None
    mid_minus_mark_vol_points: float | None
    mark_price_minus_mark_vol_points: float | None
    interest_rate: float | None
    underlying_index: str | None
    quote_status: str
    status: str
    reason: str
    reasons: tuple[str, ...]
    flags: tuple[str, ...]


def effective_tick(mid_btc: float, tick_size: float, steps: list[dict[str, float]] | None) -> float:
    if not finite_number(tick_size) or tick_size <= 0:
        raise ValueError('Invalid base tick')
    tick = tick_size
    previous = -math.inf
    for step in steps or []:
        if (not isinstance(step, dict) or set(step) != {'above_price', 'tick_size'}
                or not finite_number(step['above_price']) or not finite_number(step['tick_size'])
                or step['above_price'] < 0 or step['above_price'] <= previous or step['tick_size'] <= 0):
            raise ValueError('Invalid tick step')
        previous = step['above_price']
        if mid_btc > step['above_price']:
            tick = step['tick_size']
    return float(tick)


def distribution(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {'n': 0, 'min': None, 'p05': None, 'median': None, 'mean': None, 'p95': None, 'max': None}
    array = np.asarray(values, dtype=float)
    return {'n': len(values), 'min': float(array.min()), 'p05': float(np.quantile(array, 0.05)),
            'median': float(np.median(array)), 'mean': float(array.mean()),
            'p95': float(np.quantile(array, 0.95)), 'max': float(array.max())}


def _diagnostic(rows: list[CleanQuote], protocol: Protocol) -> dict[str, Any]:
    mid = distribution([row.mid_minus_mark_vol_points for row in rows
                        if row.mid_minus_mark_vol_points is not None])
    mark = distribution([row.mark_price_minus_mark_vol_points for row in rows
                         if row.mark_price_minus_mark_vol_points is not None])
    settings = protocol.content['diagnostics']
    if min(mark['n'], mid['n']) < settings['minimum_mark_comparisons']:
        status = 'insufficient_comparisons'
    elif abs(mark['median']) > settings['systematic_difference_vol_points']:
        status = 'investigate_convention'
    elif abs(mid['median']) > settings['systematic_difference_vol_points']:
        status = 'investigate_mid_mark_gap'
    else:
        status = 'ok'
    return {'status': status, 'mid_minus_mark_vol_points': mid,
            'mark_price_minus_mark_vol_points': mark,
            'missing_mark_comparisons': len(rows) - mark['n']}


def _clean_quote(row: dict[str, Any], forward: float | None, protocol: Protocol) -> CleanQuote:
    choices = protocol.content['cleaning']
    reasons, flags = [], []
    t_years = (row['expiry_ms'] - row['fetched_at_ms']) / (365 * 24 * 3600 * 1000)
    metadata_ok = (row['identity_status'] == 'verified' and row['metadata_time'] is not None)
    if not metadata_ok:
        reasons.append('unverified_metadata')
    elif any(row[field] != row['metadata_' + field] for field in ('expiry_ms', 'strike', 'option_type')):
        reasons.append('metadata_identity_mismatch')
        metadata_ok = False
    if metadata_ok and not row['is_active']:
        reasons.append('inactive_instrument')
    own_forward = row['underlying_price']
    if not finite_number(own_forward) or own_forward <= 0 or forward is None:
        reasons.append('invalid_forward')
        deviation = None
    else:
        deviation = abs(own_forward / forward - 1)
        if deviation > choices['maximum_forward_relative_deviation']:
            reasons.append('inconsistent_forward')
    if t_years <= 0:
        reasons.append('expired')
    elif t_years < choices['minimum_maturity_hours'] / (365 * 24):
        reasons.append('maturity_too_short')
    bid, ask = row['bid_btc'], row['ask_btc']
    bid_ok = finite_number(bid) and bid > 0
    ask_ok = finite_number(ask) and ask > 0
    if not bid_ok:
        reasons.append('no_bid' if bid is None or bid == 0 else 'invalid_bid')
    if not ask_ok:
        reasons.append('no_ask' if ask is None or ask == 0 else 'invalid_ask')
    mid = bid / 2 + ask / 2 if bid_ok and ask_ok else None
    spread = (ask - bid) / mid if mid is not None else None
    if bid_ok and ask_ok and bid > ask:
        reasons.append('crossed')
    tick = None
    if metadata_ok:
        try:
            steps = json.loads(row['tick_size_steps']) if row['tick_size_steps'] is not None else None
            tick = effective_tick(mid or 0, row['tick_size'], steps)
        except (ValueError, TypeError, KeyError):
            reasons.append('invalid_tick_metadata')
    if mid is not None and tick is not None and mid < choices['minimum_mid_ticks'] * tick:
        reasons.append('mid_below_ticks')
    if spread is not None and spread > choices['maximum_spread_to_mid']:
        reasons.append('spread_too_wide')
    if forward is not None and ((row['strike'] < forward and row['option_type'] != 'put')
                                or (row['strike'] >= forward and row['option_type'] != 'call')):
        reasons.append('not_otm')
    ready = metadata_ok and forward is not None and t_years > 0 and deviation is not None

    def invert(price_btc: float | None, fwd: float | None) -> IVResult:
        if not ready or fwd is None or not finite_number(price_btc) or price_btc <= 0:
            return IVResult(math.nan, 'not_attempted')
        return implied_vol(price_btc * fwd, fwd, row['strike'], t_years,
                           row['option_type'], **protocol.content['inversion'])

    bid_iv, ask_iv, mid_iv = invert(bid, forward), invert(ask, forward), invert(mid, forward)
    for side, result in (('bid', bid_iv), ('ask', ask_iv), ('mid', mid_iv)):
        if result.reason not in ('ok', 'not_attempted'):
            reasons.append(f'iv_{side}_{result.reason}')
    mark_iv = invert(row['mark_btc'], own_forward if finite_number(own_forward) and own_forward > 0 else None)
    reported = row['mark_iv_pct']
    reported_ok = finite_number(reported) and reported > 0
    if not reported_ok:
        flags.append('missing_or_invalid_mark_iv')
    if mark_iv.reason != 'ok':
        flags.append('mark_price_iv_' + mark_iv.reason)
    if row['interest_rate'] is not None and row['interest_rate'] != 0:
        flags.append('nonzero_interest_rate_ignored')

    def value(result: IVResult) -> float | None:
        return result.iv if result.reason == 'ok' else None

    return CleanQuote(row['snapshot_id'], row['instrument_name'], row['expiry_ms'], row['strike'],
                      row['option_type'], row['fetched_at_ms'], row['metadata_fetched_at_ms'], forward,
                      own_forward, deviation, t_years,
                      math.log(row['strike'] / forward) if forward is not None else None,
                      bid, ask, mid, tick, spread, value(bid_iv), value(ask_iv), value(mid_iv),
                      bid_iv.reason, ask_iv.reason, mid_iv.reason, value(mark_iv), mark_iv.reason, reported,
                      100 * (mid_iv.iv - reported / 100) if reported_ok and mid_iv.reason == 'ok' else None,
                      100 * (mark_iv.iv - reported / 100) if reported_ok and mark_iv.reason == 'ok' else None,
                      row['interest_rate'], row['underlying_index'], 'rejected' if reasons else 'candidate',
                      'rejected' if reasons else 'usable',
                      reasons[0] if reasons else 'ok', tuple(reasons), tuple(flags))


def clean_expiry(rows: list[dict[str, Any]], protocol: Protocol) -> dict[str, Any]:
    """I use one median forward before applying price or OTM filters."""
    if validate_protocol(protocol.content).content_hash != protocol.content_hash:
        raise ValueError('Protocol content changed after hashing')
    if not rows:
        raise ValueError('An expiry group must contain quotes')
    identities = {(r['snapshot_id'], r['expiry_ms'], r['fetched_at_ms']) for r in rows}
    if len(identities) != 1:
        raise ValueError('I clean one snapshot and expiry at a time')
    rows = sorted(rows, key=lambda r: r['instrument_name'])
    forwards = [r['underlying_price'] for r in rows if r['identity_status'] == 'verified'
                and r['metadata_time'] is not None and r['is_active']
                and finite_number(r['underlying_price']) and r['underlying_price'] > 0]
    forward = float(np.median(forwards)) if forwards else None
    cleaned = [_clean_quote(row, forward, protocol) for row in rows]
    candidates = [row for row in cleaned if row.status == 'usable']
    counts = Counter(row.strike for row in candidates)
    if any(n > 1 for n in counts.values()):
        cleaned = [replace(row, quote_status='rejected', status='rejected', reason='duplicate_otm_strike',
                           reasons=(*row.reasons, 'duplicate_otm_strike'))
                   if row.status == 'usable' and counts[row.strike] > 1 else row for row in cleaned]
        candidates = [row for row in cleaned if row.status == 'usable']
    left = {row.strike for row in candidates if row.strike < forward}
    right = {row.strike for row in candidates if row.strike > forward}
    strikes = {row.strike for row in candidates}
    settings = protocol.content['cleaning']
    group_reasons = []
    if len(strikes) < settings['minimum_unique_strikes']:
        group_reasons.append('insufficient_strikes')
    if len(left) < settings['minimum_strikes_each_side'] or len(right) < settings['minimum_strikes_each_side']:
        group_reasons.append('insufficient_sides')
    diagnostic = _diagnostic(candidates, protocol)
    if diagnostic['status'] == 'investigate_convention':
        group_reasons.append('iv_convention_investigation')
    elif diagnostic['status'] == 'investigate_mid_mark_gap':
        group_reasons.append('iv_mid_mark_investigation')
    elif diagnostic['status'] == 'insufficient_comparisons':
        group_reasons.append('iv_diagnostic_unavailable')
    if group_reasons:
        cleaned = [replace(row, status='rejected', reason=row.reason if row.reasons else group_reasons[0],
                           reasons=(*row.reasons, *group_reasons)) for row in cleaned]
    status = 'unusable' if group_reasons else 'ok'
    if diagnostic['status'].startswith('investigate'):
        status = 'investigate'
    identity, expiry, stamp = next(iter(identities))
    return {'snapshot_id': identity, 'expiry_ms': expiry, 'fetched_at_ms': stamp, 'status': status,
            'reasons': group_reasons, 'forward_usd': forward,
            'quote_forwards': distribution(forwards), 'candidate_strikes': len(strikes),
            'strikes_below_forward': len(left), 'strikes_above_forward': len(right),
            'iv_diagnostic': diagnostic, 'quotes': [asdict(row) for row in cleaned]}


def clean_database(path: Path, protocol: Protocol, as_of_ms: int | None = None) -> dict[str, Any]:
    if validate_protocol(protocol.content).content_hash != protocol.content_hash:
        raise ValueError('Protocol content changed after hashing')
    if as_of_ms is not None and (type(as_of_ms) is not int or not 0 <= as_of_ms <= 9223372036854775807):
        raise ValueError('--as-of-ms must be nonnegative integer UTC milliseconds')
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute('PRAGMA user_version').fetchone()[0] not in (1, 2):
            raise ValueError('Unsupported database schema')
        if as_of_ms is None:
            as_of_ms = connection.execute('SELECT COALESCE(MAX(fetched_at_ms), 0) FROM snapshots').fetchone()[0]
        rows = connection.execute(
            'SELECT q.*, s.fetched_at_ms, i.fetched_at_ms AS metadata_time, i.tick_size, '
            'i.tick_size_steps, i.is_active, i.expiry_ms AS metadata_expiry_ms, '
            'i.strike AS metadata_strike, i.option_type AS metadata_option_type '
            'FROM quotes q JOIN snapshots s USING(snapshot_id) '
            'LEFT JOIN instruments i ON i.instrument_name=q.instrument_name '
            'AND i.fetched_at_ms=q.metadata_fetched_at_ms AND i.fetched_at_ms<=s.fetched_at_ms '
            'WHERE s.fetched_at_ms<=? ORDER BY s.fetched_at_ms, q.snapshot_id, q.expiry_ms, q.instrument_name',
            (as_of_ms,)).fetchall()
        assert all(r['fetched_at_ms'] <= as_of_ms for r in rows)
        assert all(r['metadata_time'] is None or r['metadata_time'] <= r['fetched_at_ms'] for r in rows)
        groups = defaultdict(list)
        for row in rows:
            groups[(row['snapshot_id'], row['expiry_ms'])].append(dict(row))
        output = [clean_expiry(group, protocol) for group in groups.values()]
    finally:
        connection.close()
    quotes = [row for group in output for row in group['quotes']]
    eligible = [row for row in quotes if row['status'] == 'usable']
    candidates = [CleanQuote(**row) for row in quotes if row['quote_status'] == 'candidate']
    diagnostic = _diagnostic(candidates, protocol)
    investigations = sum(group['status'] == 'investigate' for group in output)
    ready = bool(eligible) and diagnostic['status'] == 'ok' and investigations == 0
    return {'as_of_ms': as_of_ms, 'protocol_hash': protocol.content_hash,
            'protocol_status': protocol.content['status'], 'protocol': protocol.content,
            'n_quotes': len(quotes), 'n_candidate_quotes': len(candidates),
            'n_usable_quotes': len(eligible), 'n_groups': len(output),
            'group_status_counts': dict(sorted(Counter(g['status'] for g in output).items())),
            'quote_status_counts': dict(sorted(Counter(q['status'] for q in quotes).items())),
            'reason_counts': dict(sorted(Counter(reason for q in quotes for reason in q['reasons']).items())),
            'flag_counts': dict(sorted(Counter(flag for q in quotes for flag in q['flags']).items())),
            'iv_diagnostic': diagnostic, 'phase4_ready': ready, 'groups': output}
