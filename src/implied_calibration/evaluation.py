"""I score immutable forecasts only after their official delivery observation exists."""

from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
import re
from statistics import mean
from typing import Any, Callable

from .locking import LockedProtocol
from .predictions import checked_prediction, register_protocol
from .scoring import FrozenCDF, ScoreSettings, score_distribution
from .store import canonical, digest, evaluation_store, retain_score_availability


def score_database(db: Path, locked: LockedProtocol, as_of_ms: int, created_at_ms: int,
                   git_commit: str, *, now_ms: Callable[[], int] | None = None) -> dict[str, Any]:
    locked.validate()
    if (type(as_of_ms) is not int or type(created_at_ms) is not int or not 0 <= as_of_ms <= created_at_ms
            or not re.fullmatch('[0-9a-f]{40}', git_commit)):
        raise ValueError('I need bounded integer timestamps and a Git commit for scoring')
    store = evaluation_store(db)
    output, completed = [], []
    added = existing_count = 0
    persist = created_at_ms == as_of_ms
    try:
        with store.connection:
            register_protocol(store.connection, locked)
            rows = store.connection.execute(
                "SELECT * FROM predictions WHERE expiry_ms<=? "
                "AND json_extract(payload_json, '$.created_at_ms')<=? "
                "AND json_extract(payload_json, '$.protocol_hash')=? ORDER BY expiry_ms, lead_ms, prediction_id",
                (as_of_ms, as_of_ms, locked.protocol.content_hash)).fetchall()
            for row in rows:
                prediction = checked_prediction(row['payload_json'])
                assert prediction['created_at_ms'] <= as_of_ms and prediction['expiry_ms'] <= as_of_ms
                identity = row['prediction_id']
                old = store.connection.execute('SELECT payload_json FROM score_records WHERE prediction_id=?',
                                               (identity,)).fetchone()
                if old is not None:
                    record = checked_prediction(old['payload_json'])
                    if record['created_at_ms'] <= as_of_ms:
                        output.append(record)
                        completed.append(record)
                        existing_count += 1
                        continue
                    if persist:
                        output.append({'prediction_id': identity, 'status': 'score_unavailable_at_cutoff'})
                        continue
                if prediction['status'] != 'ok':
                    output.append({'prediction_id': identity, 'status': 'forecast_unavailable',
                                   'forecast_status': prediction['status']})
                    continue
                delivery = store.connection.execute(
                    'SELECT d.*, o.source_id, o.content_hash FROM delivery_prices d '
                    'JOIN delivery_observations o USING(expiry_ms) WHERE d.expiry_ms=? AND d.fetched_at_ms<=?',
                    (prediction['expiry_ms'], as_of_ms)).fetchone()
                if delivery is None:
                    output.append({'prediction_id': identity, 'status': 'no_delivery_price'})
                    continue
                assert delivery['fetched_at_ms'] <= as_of_ms and delivery['expiry_ms'] <= delivery['fetched_at_ms']
                score = score_distribution(FrozenCDF.from_record(prediction), float(delivery['delivery_price']),
                                           prediction['expiry_ms'], ScoreSettings(**locked.protocol.content['scoring']))
                if score.status != 'ok':
                    output.append({'prediction_id': identity, 'status': score.status, 'score': asdict(score)})
                    continue
                finished = created_at_ms if now_ms is None or not persist else now_ms()
                if type(finished) is not int or not created_at_ms <= finished <= 9223372036854775807:
                    raise ValueError('Clock moved backward during score calculation')
                record = {'prediction_id': identity, 'prediction_content_hash': prediction['content_hash'],
                          'protocol_hash': locked.protocol.content_hash, 'git_commit': git_commit,
                          'created_at_ms': finished, 'as_of_ms': as_of_ms, 'expiry_ms': prediction['expiry_ms'],
                          'lead_ms': prediction['lead_ms'], 'status': 'ok', 'delivery': dict(delivery), 'score': asdict(score)}
                record['content_hash'] = digest(record)
                if persist:
                    store.connection.execute('INSERT INTO score_records VALUES (?, ?)', (identity, canonical(record)))
                    for metric, value in score.metrics.items():
                        store.connection.execute('INSERT INTO scores VALUES (?, ?, ?)', (identity, metric, value))
                    added += 1
                output.append(record)
                completed.append(record)
        if persist:
            retain_score_availability(store.connection, 'score_records', completed,
                                      now_ms if now_ms is not None else lambda: created_at_ms)
    finally:
        store.close()
    classified: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in completed:
        classified[(record['score']['expiry_class'], record['lead_ms'])].append(record)
    summaries = []
    for (classification, lead), records in sorted(classified.items()):
        infinite = sum(record['score']['diagnostics']['negative_log_score_is_infinite'] for record in records)
        unavailable = sum(record['score']['diagnostics']['log_score_status'] == 'support_atom' for record in records)
        summaries.append({'expiry_class': classification, 'lead_ms': lead, 'n_scored': len(records),
                          'mean_crps_usd': mean(record['score']['metrics']['crps_usd'] for record in records),
                          'coverage': {str(level): {'n': len(records), 'frequency':
                              mean(record['score']['metrics'][f'coverage_{level}'] for record in records)}
                              for level in (50, 80, 90)},
                          'infinite_log_losses': infinite, 'undefined_continuous_log_losses': unavailable,
                          'mean_negative_log_score_log_return': None if infinite or unavailable else
                          mean(record['score']['metrics']['negative_log_score_log_return'] for record in records),
                          'log_mean_status': 'infinite' if infinite else 'undefined' if unavailable else 'finite'})
    return {'status': 'ok' if completed else 'no_scorable_forecasts', 'as_of_ms': as_of_ms,
            'mode': 'persist' if persist else 'historical_preview',
            'created_at_ms': created_at_ms, 'protocol_hash': locked.protocol.content_hash,
            'added': added, 'existing': existing_count,
            'status_counts': dict(sorted(Counter(row['status'] for row in output).items())),
            'inference_status': 'insufficient data; I run infer for dependence-aware comparisons',
            'class_and_lead_summary': summaries, 'scores': output}
