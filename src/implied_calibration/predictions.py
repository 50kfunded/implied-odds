"""I freeze each due forecast once, including missing or rejected forecasts."""

from collections import Counter
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

from .distributions import cdf_database
from .locking import LockedProtocol
from .scoring import FrozenCDF, expiry_class
from .density import ImpliedCDF
from .store import canonical, digest, evaluation_store


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    expiry_ms: int
    lead_ms: int
    target_time_ms: int
    as_of_ms: int
    created_at_ms: int
    protocol_hash: str
    git_commit: str
    lock_witness_commit: str
    status: str
    snapshot_id: str | None = None
    fetched_at_ms: int | None = None
    gap_ms: int | None = None
    forward_usd: float | None = None
    t_years: float | None = None
    params: dict[str, float] | None = None
    fit_diagnostics: dict[str, Any] | None = None
    distribution: dict[str, Any] | None = None
    scoring_support: dict[str, Any] | None = None
    message: str = ''
    content_hash: str = ''


def register_protocol(connection: sqlite3.Connection, locked: LockedProtocol) -> None:
    locked.validate()
    values = (locked.protocol.content_hash, canonical(locked.protocol.content), canonical(locked.lock),
              locked.witness_commit, locked.witness_at_ms)
    existing = connection.execute('SELECT * FROM evaluation_protocols WHERE protocol_hash=?', (values[0],)).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise ValueError('Stored evaluation protocol differs from this lock witness')
        return
    connection.execute('INSERT INTO evaluation_protocols VALUES (?, ?, ?, ?, ?)', values)


def checked_prediction(payload: str) -> dict[str, Any]:
    record = json.loads(payload)
    claimed = record.pop('content_hash')
    if digest(record) != claimed:
        raise ValueError('Stored prediction hash differs')
    record['content_hash'] = claimed
    return record


def select_snapshot(connection: sqlite3.Connection, target_time_ms: int, as_of_ms: int) -> sqlite3.Row | None:
    cutoff = min(target_time_ms, as_of_ms)
    row = connection.execute('SELECT snapshot_id, fetched_at_ms FROM snapshots WHERE fetched_at_ms<=? '
                             'ORDER BY fetched_at_ms DESC, snapshot_id LIMIT 1', (cutoff,)).fetchone()
    assert row is None or row['fetched_at_ms'] <= cutoff <= target_time_ms
    return row


def freeze_predictions(db: Path, locked: LockedProtocol, as_of_ms: int, created_at_ms: int,
                       git_commit: str, *, expiries: list[int] | None = None,
                       now_ms: Callable[[], int] | None = None, baseline_extension=None) -> dict[str, Any]:
    locked.validate()
    if (type(as_of_ms) is not int or type(created_at_ms) is not int or not 0 <= as_of_ms <= created_at_ms
            or not re.fullmatch('[0-9a-f]{40}', git_commit)):
        raise ValueError('I need bounded integer timestamps and a Git commit for freezing')
    choices = locked.protocol.content['evaluation']
    store = evaluation_store(db)
    added, existing_count, not_due, ineligible = 0, 0, 0, 0
    output, baseline_output = [], []
    if baseline_extension is not None:
        baseline_extension.validate(locked)
    try:
        with store.connection:
            register_protocol(store.connection, locked)
            if expiries is None:
                observations = store.connection.execute('SELECT DISTINCT expiry_ms FROM instruments '
                                                         'WHERE fetched_at_ms<=? ORDER BY expiry_ms', (as_of_ms,)).fetchall()
                expiries = [row['expiry_ms'] for row in observations]
            for expiry in sorted(set(expiries)):
                expiry_class(expiry)
                if expiry < choices['first_expiry_ms']:
                    ineligible += 1
                    continue
                for lead in choices['lead_times_ms']:
                    target = expiry - lead
                    identity = digest([expiry, lead, locked.protocol.content_hash])
                    old = store.connection.execute('SELECT payload_json FROM predictions WHERE prediction_id=?', (identity,)).fetchone()
                    if old is not None:
                        record = checked_prediction(old['payload_json'])
                        if record['created_at_ms'] > as_of_ms:
                            not_due += 1
                            continue
                        output.append(record)
                        if baseline_extension is not None:
                            stored_baselines = store.connection.execute('SELECT payload_json FROM baseline_predictions '
                                'WHERE prediction_id=? AND extension_hash=? '
                                "AND json_extract(payload_json, '$.created_at_ms')<=? ORDER BY baseline_name",
                                (identity, baseline_extension.content_hash, as_of_ms)).fetchall()
                            baseline_output.extend(checked_prediction(row[0]) for row in stored_baselines)
                        existing_count += 1
                        continue
                    if target > as_of_ms:
                        not_due += 1
                        continue
                    prediction = Prediction(identity, expiry, lead, target, as_of_ms, created_at_ms,
                                            locked.protocol.content_hash, git_commit, locked.witness_commit,
                                            'late_freeze' if created_at_ms > target + choices['freeze_delay_tolerance_ms'] else 'no_snapshot')
                    if prediction.status != 'late_freeze':
                        snapshot = select_snapshot(store.connection, target, as_of_ms)
                        if snapshot is not None:
                            gap = target - snapshot['fetched_at_ms']
                            prediction = replace(prediction, snapshot_id=snapshot['snapshot_id'],
                                                 fetched_at_ms=snapshot['fetched_at_ms'], gap_ms=gap)
                            if gap <= choices['snapshot_tolerance_ms']:
                                report = cdf_database(db, locked.protocol, target, snapshot_id=snapshot['snapshot_id'])
                                groups = [group for group in report['groups'] if group['expiry_ms'] == expiry]
                                if not groups:
                                    prediction = replace(prediction, status='expiry_absent', message='The selected snapshot has no rows for this expiry')
                                else:
                                    group = groups[0]
                                    assert group['fetched_at_ms'] <= min(as_of_ms, target)
                                    prediction = replace(prediction, status=group['cdf_status'], forward_usd=group['forward_usd'],
                                                         t_years=group['t_years'], params=group['fit']['params'],
                                                         fit_diagnostics=group['fit'], distribution=group['distribution'],
                                                         message=group['cdf_message'])
                                    if prediction.status == 'ok':
                                        model = ImpliedCDF.from_record(group['distribution'])
                                        support = FrozenCDF.for_model(model).support_record()
                                        prediction = replace(prediction, scoring_support=support)
                            else:
                                prediction = replace(prediction, message='The latest snapshot is older than the locked tolerance')
                    completed_at_ms = created_at_ms if now_ms is None else now_ms()
                    if type(completed_at_ms) is not int or completed_at_ms < created_at_ms:
                        raise ValueError('Clock moved backward while I was freezing a forecast')
                    prediction = replace(prediction, created_at_ms=completed_at_ms)
                    if completed_at_ms > target + choices['freeze_delay_tolerance_ms']:
                        prediction = replace(prediction, status='late_freeze', distribution=None, scoring_support=None,
                                             message='The freeze deadline elapsed before the forecast was ready')
                    record = asdict(prediction)
                    record.pop('content_hash')
                    record['content_hash'] = digest(record)
                    store.connection.execute('INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)',
                                             (identity, expiry, lead, prediction.snapshot_id, prediction.status, canonical(record)))
                    if baseline_extension is not None:
                        from .baseline_evaluation import freeze_baselines
                        baseline_output.extend(freeze_baselines(store.connection, record, baseline_extension, locked,
                                                                 completed_at_ms, git_commit, now_ms))
                    output.append(record)
                    added += 1
    finally:
        store.close()
    return {'status': 'ok' if any(row['status'] == 'ok' for row in output) else 'no_eligible_forecasts',
            'as_of_ms': as_of_ms, 'created_at_ms': max([created_at_ms] + [row['created_at_ms'] for row in output + baseline_output]),
            'protocol_hash': locked.protocol.content_hash,
            'added': added, 'existing': existing_count, 'not_due': not_due, 'ineligible_expiries': ineligible,
            'status_counts': dict(sorted(Counter(row['status'] for row in output).items())), 'predictions': output,
            'baselines': sorted(baseline_output, key=lambda row: (row['expiry_ms'], row['lead_ms'], row['baseline_name'])),
            'baseline_status_counts': dict(sorted(Counter(row['status'] for row in baseline_output).items()))}
