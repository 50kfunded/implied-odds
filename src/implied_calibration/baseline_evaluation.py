"""I freeze and score baseline records once, with the original forecast's cutoff."""

from collections import Counter, defaultdict
from dataclasses import asdict
import math
from pathlib import Path
import sqlite3
from typing import Callable

from .baseline_lock import LockedBaselines
from .baselines import BaselineCDF, atm_baseline, garch_baseline, realised_baseline
from .history import trailing_returns
from .inference import berkowitz_hac, paired_inference
from .locking import LockedProtocol
from .predictions import checked_prediction
from .scoring import ScoreSettings, score_distribution
from .store import canonical, digest, evaluation_store, retain_score_availability, score_available


def register_baselines(connection: sqlite3.Connection, extension: LockedBaselines, parent: LockedProtocol) -> None:
    extension.validate(parent)
    record = {'content': extension.content, 'lock': extension.lock, 'witness_commit': extension.witness_commit,
              'witness_at_ms': extension.witness_at_ms}
    old = connection.execute('SELECT payload_json FROM baseline_protocols WHERE extension_hash=?',
                             (extension.content_hash,)).fetchone()
    if old is not None:
        if old[0] != canonical(record):
            raise ValueError('Stored baseline protocol differs from its committed lock')
    else:
        connection.execute('INSERT INTO baseline_protocols VALUES (?, ?)', (extension.content_hash, canonical(record)))


def freeze_baselines(connection: sqlite3.Connection, prediction: dict, extension: LockedBaselines,
                     parent: LockedProtocol, created_at_ms: int, git_commit: str,
                     now_ms: Callable[[], int] | None = None) -> list[dict]:
    register_baselines(connection, extension, parent)
    checked_prediction(canonical(prediction))
    if prediction['protocol_hash'] != parent.protocol.content_hash:
        raise ValueError('Baseline forecast uses a different parent protocol')
    if prediction['expiry_ms'] < extension.content['first_expiry_ms']:
        return []
    settings = extension.settings
    output = []
    for name in parent.protocol.content['evaluation']['baseline_names']:
        identity = digest([prediction['prediction_id'], name, extension.content_hash])
        old = connection.execute('SELECT payload_json FROM baseline_predictions WHERE baseline_id=?', (identity,)).fetchone()
        if old is not None:
            record = checked_prediction(old[0])
            if record['created_at_ms'] <= prediction['as_of_ms']:
                output.append(record)
            continue
        start = created_at_ms if now_ms is None else now_ms()
        if type(start) is not int or start < max(created_at_ms, prediction['created_at_ms']):
            raise ValueError('Clock moved backward before baseline freezing')
        deadline = prediction['target_time_ms'] + parent.protocol.content['evaluation']['freeze_delay_tolerance_ms']
        record = {'baseline_id': identity, 'prediction_id': prediction['prediction_id'], 'baseline_name': name,
                  'prediction_content_hash': prediction['content_hash'], 'extension_hash': extension.content_hash,
                  'protocol_hash': parent.protocol.content_hash, 'lock_witness_commit': extension.witness_commit,
                  'git_commit': git_commit, 'expiry_ms': prediction['expiry_ms'], 'lead_ms': prediction['lead_ms'],
                  'snapshot_id': prediction['snapshot_id'], 'as_of_ms': prediction['as_of_ms'],
                  'history_cutoff_ms': prediction['fetched_at_ms'], 'created_at_ms': start,
                  'drift_assumption': settings.garch_drift if name == 'garch11_student_t' else settings.gaussian_drift,
                  'distribution': None, 'status': 'forecast_unavailable', 'history': None}
        if start > deadline:
            record['status'] = 'late_freeze'
        elif prediction['status'] == 'ok':
            try:
                if name == 'same_snapshot_atm_lognormal':
                    distribution = atm_baseline(prediction)
                else:
                    hours = settings.realised_window_hours if name == 'trailing_realised_lognormal' else settings.garch_window_hours
                    history = trailing_returns(connection, prediction['fetched_at_ms'], hours, settings.history_max_age_ms)
                    returns = history.pop('returns', None)
                    record['history'] = history
                    if history['status'] != 'ok':
                        record['status'] = history['status']
                        distribution = None
                    else:
                        distribution = realised_baseline(prediction, returns) if name == 'trailing_realised_lognormal' else garch_baseline(
                            prediction, returns, settings, gap_hours=(prediction['fetched_at_ms']-history['last_close_ms'])/3600000)
                if distribution is not None:
                    record.update(status='ok', distribution=distribution.to_record(),
                                  scoring_support=distribution.support_record())
            except (ValueError, FloatingPointError, ArithmeticError) as exc:
                record.update(status='baseline_failed', reason=str(exc))
        finished = start if now_ms is None else now_ms()
        if type(finished) is not int or finished < start:
            raise ValueError('Clock moved backward during baseline freezing')
        record['created_at_ms'] = finished
        if finished > deadline:
            record.update(status='late_freeze', distribution=None, scoring_support=None)
        record['content_hash'] = digest(record)
        connection.execute('INSERT INTO baseline_predictions VALUES (?, ?, ?, ?, ?)',
                           (identity, prediction['prediction_id'], name, extension.content_hash, canonical(record)))
        output.append(record)
    return output


def score_baselines(db: Path, extension: LockedBaselines, parent: LockedProtocol, as_of_ms: int,
                    created_at_ms: int, git_commit: str, *, parent_preview: list[dict] | None = None,
                    now_ms: Callable[[], int] | None = None) -> dict:
    if type(as_of_ms) is not int or type(created_at_ms) is not int or not 0 <= as_of_ms <= created_at_ms:
        raise ValueError('Invalid baseline score cutoff')
    store = evaluation_store(db)
    output = []
    added = 0
    persist = created_at_ms == as_of_ms
    preview = {r['prediction_id']: r for r in (parent_preview or []) if r['status'] == 'ok'}
    if preview and created_at_ms == as_of_ms:
        raise ValueError('I only accept temporary parent scores for historical previews')
    try:
        with store.connection:
            register_baselines(store.connection, extension, parent)
            rows = store.connection.execute('SELECT * FROM baseline_predictions WHERE extension_hash=? '
                "AND json_extract(payload_json, '$.created_at_ms')<=? AND json_extract(payload_json, '$.expiry_ms')<=? "
                "ORDER BY json_extract(payload_json, '$.expiry_ms'), baseline_name, baseline_id",
                (extension.content_hash, as_of_ms, as_of_ms)).fetchall()
            for row in rows:
                prediction = checked_prediction(row['payload_json'])
                identity = row['baseline_id']
                old = store.connection.execute('SELECT payload_json FROM baseline_scores WHERE baseline_id=?',
                                               (identity,)).fetchone()
                if old is not None:
                    record = checked_prediction(old[0])
                    if record['created_at_ms'] <= as_of_ms:
                        output.append(record)
                        continue
                    if persist:
                        output.append({'baseline_id': identity, 'status': 'score_unavailable_at_cutoff'})
                        continue
                if prediction['status'] != 'ok':
                    output.append({'baseline_id': identity, 'status': prediction['status']})
                    continue
                svi = store.connection.execute('SELECT payload_json FROM score_records WHERE prediction_id=? '
                    "AND json_extract(payload_json, '$.created_at_ms')<=?",
                    (prediction['prediction_id'], as_of_ms)).fetchone()
                if svi is None and prediction['prediction_id'] not in preview:
                    output.append({'baseline_id': identity, 'status': 'no_paired_svi_score'})
                    continue
                svi = checked_prediction(svi[0]) if svi is not None else checked_prediction(canonical(preview[prediction['prediction_id']]))
                assert svi['as_of_ms'] <= as_of_ms and svi['delivery']['fetched_at_ms'] <= as_of_ms
                original = store.connection.execute('SELECT payload_json FROM predictions WHERE prediction_id=? '
                    "AND json_extract(payload_json, '$.created_at_ms')<=?", (prediction['prediction_id'], as_of_ms)).fetchone()
                if original is None:
                    raise ValueError('I need the frozen original prediction for baseline scoring')
                original = checked_prediction(original[0])
                distribution = BaselineCDF(**prediction['distribution'])
                if (prediction['prediction_content_hash'] != original['content_hash']
                        or svi['prediction_content_hash'] != original['content_hash']
                        or distribution.lower_usd != original['scoring_support']['lower_usd']
                        or distribution.upper_usd != original['scoring_support']['upper_usd']
                        or distribution.forward_usd != original['forward_usd']
                        or prediction['scoring_support'] != distribution.support_record()):
                    raise ValueError('Baseline and SVI forecast provenance or scoring bounds differ')
                score = asdict(score_distribution(distribution, svi['delivery']['delivery_price'],
                                                  prediction['expiry_ms'], ScoreSettings(**parent.protocol.content['scoring'])))
                if score['status'] != 'ok':
                    output.append({'baseline_id': identity, 'status': score['status'], 'score': score})
                    continue
                if distribution.kind == 'garch_quantile_surrogate':
                    score['diagnostics']['simulation_approximation'] = distribution.parameters['log_loss_warning']
                    score['diagnostics']['simulation_tail_resolution'] = distribution.parameters['simulation_tail_resolution']
                    k = math.log(svi['delivery']['delivery_price']/distribution.forward_usd)
                    score['diagnostics']['simulation_support_exceedance'] = ('left' if k < distribution.parameters['knots'][0]
                        else 'right' if k > distribution.parameters['knots'][-1] else None)
                record = {k: prediction[k] for k in ('baseline_id', 'prediction_id', 'baseline_name', 'extension_hash',
                                                       'protocol_hash', 'expiry_ms', 'lead_ms')}
                finished = created_at_ms if now_ms is None or not persist else now_ms()
                if type(finished) is not int or not created_at_ms <= finished <= 9223372036854775807:
                    raise ValueError('Clock moved backward during baseline score calculation')
                record.update(status='ok', created_at_ms=finished, as_of_ms=as_of_ms, git_commit=git_commit,
                              baseline_content_hash=prediction['content_hash'], svi_score_content_hash=svi['content_hash'],
                              delivery=svi['delivery'], score=score)
                record['content_hash'] = digest(record)
                if persist:
                    store.connection.execute('INSERT INTO baseline_scores VALUES (?, ?)', (identity, canonical(record)))
                    added += 1
                output.append(record)
        if persist:
            retain_score_availability(store.connection, 'baseline_scores',
                                      [record for record in output if record['status'] == 'ok'],
                                      now_ms if now_ms is not None else lambda: created_at_ms)
    finally:
        store.close()
    return {'status': 'ok' if any(r['status'] == 'ok' for r in output) else 'no_scorable_baselines',
            'mode': 'persist' if persist else 'historical_preview',
            'added': added, 'status_counts': dict(sorted(Counter(r['status'] for r in output).items())), 'scores': output}


def infer_database(db: Path, extension: LockedBaselines, parent: LockedProtocol, as_of_ms: int) -> dict:
    from .research_plan import stopping_choices
    extension.validate(parent)
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError('Invalid inference cutoff')
    choices = stopping_choices(parent.protocol.content['evaluation'], parent.protocol.content_hash,
                               extension.content_hash, as_of_ms)
    if choices['formal_decision_allowed']:
        as_of_ms = min(as_of_ms, choices['final_analysis_at_ms'])
    # I read inference inputs without migrating or changing the research database.
    connection = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    paired = defaultdict(list)
    missing: Counter = Counter()
    try:
        forecasts = connection.execute('SELECT * FROM baseline_predictions WHERE extension_hash=? '
            "AND json_extract(payload_json, '$.created_at_ms')<=? AND json_extract(payload_json, '$.expiry_ms')<=?",
            (extension.content_hash, as_of_ms, as_of_ms)).fetchall()
        for forecast in forecasts:
            prediction = checked_prediction(forecast['payload_json'])
            base = connection.execute('SELECT payload_json FROM baseline_scores WHERE baseline_id=? '
                "AND json_extract(payload_json, '$.created_at_ms')<=?", (forecast['baseline_id'], as_of_ms)).fetchone()
            svi = connection.execute('SELECT payload_json FROM score_records WHERE prediction_id=? '
                "AND json_extract(payload_json, '$.created_at_ms')<=?", (forecast['prediction_id'], as_of_ms)).fetchone()
            if base is None or svi is None:
                missing[prediction['status'] if prediction['status'] != 'ok' else 'unscored_pair'] += 1
                continue
            base, svi = checked_prediction(base[0]), checked_prediction(svi[0])
            if (not score_available(connection, 'baseline_scores', base, as_of_ms)
                    or not score_available(connection, 'score_records', svi, as_of_ms)):
                missing['score_unavailable_at_cutoff'] += 1
                continue
            assert max(base['delivery']['fetched_at_ms'], svi['delivery']['fetched_at_ms']) <= as_of_ms
            if base['svi_score_content_hash'] != svi['content_hash'] or base['delivery'] != svi['delivery']:
                raise ValueError('Paired score provenance differs')
            if (base['baseline_content_hash'] != prediction['content_hash']
                    or prediction['prediction_content_hash'] != svi['prediction_content_hash']):
                raise ValueError('Paired baseline forecast provenance differs')
            key = (base['lead_ms'], base['baseline_name'])
            paired[key].append((base, svi))
    finally:
        connection.close()
    groups = []
    for lead in choices['lead_times_ms']:
        for name in choices['baseline_names']:
            rows = sorted((pair for pair in paired[(lead, name)] if pair[0]['expiry_ms'] <= choices['expiry_cutoff_ms']),
                          key=lambda pair: pair[0]['expiry_ms'])
            classes = ['all', 'daily', 'weekly', 'monthly', 'quarterly']
            for classification in classes:
                selected = [pair for pair in rows if classification == 'all' or pair[0]['score']['expiry_class'] == classification]
                times = [pair[0]['expiry_ms'] for pair in selected]
                declared_primary = (lead == choices['primary_lead_ms'] and name == choices['primary_baseline'] and classification == 'all')
                primary = declared_primary and choices['formal_decision_allowed']
                result = paired_inference(times, [pair[1]['score']['metrics']['crps_usd'] for pair in selected],
                    [pair[0]['score']['metrics']['crps_usd'] for pair in selected], lead,
                    minimum_ess=choices['minimum_effective_sample_size'], block_length=choices['expected_block_length'],
                    resamples=choices['bootstrap_resamples'], seed=choices['bootstrap_seed'], primary=primary)
                pits = {}
                for index, model in ((1, 'svi'), (0, name)):
                    diagnostic = berkowitz_hac([pair[index]['score']['metrics']['pit'] for pair in selected],
                        lags=result['hac_lags'], minimum_ess=choices['minimum_effective_sample_size'],
                        epsilon=parent.protocol.content['scoring']['pit_transform_epsilon'])
                    diagnostic['p_value'] = None
                    diagnostic['comparison_role'] = 'descriptive_secondary'
                    pits[model] = diagnostic
                log_status = {model: {'infinite': sum(pair[index]['score']['diagnostics']['negative_log_score_is_infinite'] for pair in selected),
                    'undefined_at_atom': sum(pair[index]['score']['diagnostics']['log_score_status'] == 'support_atom' for pair in selected)}
                    for index, model in ((1, 'svi'), (0, name))}
                result['declared_primary'] = declared_primary
                result['decision_schedule'] = 'terminal' if choices['formal_decision_allowed'] else 'interim_descriptive'
                result['final_analysis_at_ms'] = choices['final_analysis_at_ms']
                groups.append({'lead_ms': lead, 'baseline': name, 'expiry_class': classification,
                               'crps_comparison': result, 'pit_diagnostics': pits, 'log_loss_counts': log_status})
    primary = next(g['crps_comparison'] for g in groups if g['crps_comparison']['declared_primary'])
    return {'status': primary['status'], 'as_of_ms': as_of_ms, 'protocol_hash': parent.protocol.content_hash,
            'extension_hash': extension.content_hash, 'primary_comparison': primary,
            'multiple_testing': 'one_declared_primary; all_other_outputs_descriptive',
            'missing_pair_counts': dict(sorted(missing.items())), 'groups': groups}
