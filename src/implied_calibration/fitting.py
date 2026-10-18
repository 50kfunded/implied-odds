"""I fit cleaned snapshots only after their recorded IV investigation gate clears."""

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .cleaning import clean_database
from .protocol import Protocol
from .svi import FitResult, SVISettings, SmileSlice, check_calendars, fit_svi


def svi_settings(protocol: Protocol) -> SVISettings:
    choices = {name: value for name, value in protocol.content['svi'].items()
               if name not in ('candidate_selection', 'primary_acceptance')}
    return SVISettings(**choices,
                       minimum_strikes=protocol.content['cleaning']['minimum_unique_strikes'],
                       minimum_strikes_each_side=protocol.content['cleaning']['minimum_strikes_each_side'])


def fit_database(path: Path, protocol: Protocol, as_of_ms: int | None = None,
                 *, snapshot_id: str | None = None) -> dict[str, Any]:
    cleaned = clean_database(path, protocol, as_of_ms, snapshot_id=snapshot_id)
    settings = svi_settings(protocol)
    slices = []
    groups = [group for group in cleaned['groups'] if snapshot_id is None or group['snapshot_id'] == snapshot_id]
    for group in groups:
        rows = [row for row in group['quotes'] if row['status'] == 'usable']
        assert all(row['fetched_at_ms'] <= cleaned['as_of_ms'] for row in group['quotes'])
        k = tuple(row['k'] for row in rows)
        if group['status'] == 'investigate' or not cleaned['phase4_ready']:
            result = FitResult('blocked_upstream', n_strikes=len(rows),
                               message='The captured-data IV gate is closed; I have not attempted an SVI fit')
        elif group['status'] != 'ok':
            result = FitResult('cleaning_rejected', n_strikes=len(rows), message='; '.join(group['reasons']))
        else:
            result = fit_svi(k, [row['iv_bid'] for row in rows], [row['iv_ask'] for row in rows],
                             [row['iv_mid'] for row in rows], rows[0]['t_years'], settings)
        t_years = (group['expiry_ms'] - group['fetched_at_ms']) / (365 * 24 * 3600 * 1000)
        slices.append(SmileSlice(group['snapshot_id'], group['expiry_ms'], t_years, k, result))
    slices = check_calendars(slices, settings)
    output = []
    for group, entry in zip(groups, slices):
        accepted = entry.result.status == 'ok' and entry.result.calendar_flag is False
        output.append({'snapshot_id': entry.snapshot_id, 'expiry_ms': entry.expiry_ms,
                       'fetched_at_ms': group['fetched_at_ms'], 't_years': entry.t_years,
                       'forward_usd': group['forward_usd'], 'observed_k': entry.observed_k,
                       'cleaning_status': group['status'], 'cleaning_reasons': group['reasons'],
                       'primary_accepted': accepted, 'fit': asdict(entry.result)})
    count_accepted = sum(row['primary_accepted'] for row in output)
    return {'as_of_ms': cleaned['as_of_ms'], 'protocol_hash': protocol.content_hash,
            'protocol_status': protocol.content['status'], 'protocol': protocol.content,
            'phase4_ready': cleaned['phase4_ready'],
            'status': 'blocked_upstream' if not cleaned['phase4_ready'] else
                      ('ok' if count_accepted else 'no_accepted_fits'),
            'n_groups': len(output), 'n_primary_accepted': count_accepted,
            'n_fit_attempted': sum(row['fit']['starts_attempted'] > 0 for row in output),
            'fit_status_counts': dict(sorted(Counter(row['fit']['status'] for row in output).items())),
            'cleaning_summary': {name: value for name, value in cleaned.items()
                                 if name not in ('groups', 'protocol', 'protocol_hash', 'protocol_status')},
            'groups': output}
