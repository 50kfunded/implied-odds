"""I calculate report panels from frozen scores without reading files."""

from collections import defaultdict
from dataclasses import dataclass
import math

import numpy as np
from scipy.stats import binom

from .inference import berkowitz_hac, effective_sample_size, overlap_count, paired_inference, stationary_bootstrap


@dataclass(frozen=True)
class ReportObservation:
    prediction_id: str
    expiry_ms: int
    lead_ms: int
    expiry_class: str
    model: str
    metrics: dict
    diagnostics: dict
    tail_events: dict[str, float]


def tail_events(distribution, delivery_usd: float) -> dict[str, float]:
    return {f'{side}_{p:g}': float(delivery_usd <= distribution.quantile(p) if side == 'left'
            else delivery_usd >= distribution.quantile(1-p))
            for side in ('left', 'right') for p in (.01, .05, .1, .25)}


def histogram(pits: list[float], choices: dict, epsilon: float, *, times: list[int] | None = None, lead: int | None = None) -> dict:
    u = np.asarray(pits, dtype=float)
    if u.ndim != 1 or not np.isfinite(u).all() or np.any((u < 0) | (u > 1)):
        raise ValueError('Invalid raw PIT values for the report')
    overlap = overlap_count(np.asarray(times,dtype=np.int64),lead) if times is not None else 1
    diagnostic = berkowitz_hac(u, lags=max(choices['expected_block_length']-1,overlap-1),
        minimum_ess=choices['minimum_effective_sample_size'], epsilon=epsilon)
    diagnostic['p_value'] = None
    counts, edges = np.histogram(u, bins=np.linspace(0, 1, 11))
    reference = binom.ppf([.025, .975], len(u), .1).tolist() if len(u) else None
    return {'status': diagnostic['status'], 'n': len(u), 'counts': counts.tolist(), 'edges': edges.tolist(),
            'reference_count_range_95': reference, 'diagnostic': diagnostic,
            'reference_label': '95% binomial iid reference range; pointwise, not dependence-aware confidence bands'}


def binary_summary(values: list[float], times: list[int], lead: int, choices: dict) -> dict:
    x = np.asarray(values, dtype=float)
    if len(x) != len(times) or not np.isfinite(x).all() or np.any((x != 0) & (x != 1)):
        raise ValueError('I need paired binary events and expiry times')
    overlap = overlap_count(np.asarray(times, dtype=np.int64), lead)
    block = max(choices['expected_block_length'], overlap)
    lags = max(choices['expected_block_length']-1, overlap-1)
    ess = effective_sample_size(x, lags)
    enough = ess >= choices['minimum_effective_sample_size'] and len(x) > lags
    interval = stationary_bootstrap(x, resamples=choices['bootstrap_resamples'], block_length=block,
        seed=choices['bootstrap_seed'])['confidence_interval_95'] if enough else None
    return {'status': 'descriptive' if enough else 'insufficient data', 'n': len(x),
            'events': int(x.sum()), 'frequency': float(x.mean()) if len(x) else None,
            'effective_sample_size': ess, 'confidence_interval_95': interval,
            'interval_label': 'pointwise stationary-bootstrap interval; descriptive only'}


def cumulative_comparison(rows: list[ReportObservation], choices: dict, *, max_points: int = 32) -> dict:
    if type(max_points) is not int or max_points < 2:
        raise ValueError('I need at least two display points')
    lead, baseline = choices['primary_lead_ms'], choices['primary_baseline']
    paired: dict[str, dict[str, ReportObservation]] = defaultdict(dict)
    for row in rows:
        if row.lead_ms == lead and row.model in ('svi', baseline) and row.expiry_ms <= choices.get('expiry_cutoff_ms', 9223372036854775807):
            if row.model in paired[row.prediction_id]:
                raise ValueError('Duplicate model score in a report pair')
            paired[row.prediction_id][row.model] = row
    complete = sorted((p for p in paired.values() if set(p) == {'svi', baseline}), key=lambda p:p['svi'].expiry_ms)
    times = [p['svi'].expiry_ms for p in complete]
    if any(p['svi'].expiry_ms != p[baseline].expiry_ms for p in complete):
        raise ValueError('Paired report expiries differ')
    svi = [p['svi'].metrics['crps_usd'] for p in complete]
    other = [p[baseline].metrics['crps_usd'] for p in complete]
    arguments = {'minimum_ess':choices['minimum_effective_sample_size'], 'block_length':choices['expected_block_length'],
                  'resamples':choices['bootstrap_resamples'], 'seed':choices['bootstrap_seed'],
                  'primary':choices.get('formal_decision_allowed', False)}
    final = paired_inference(times, svi, other, lead, **arguments)
    final['decision_schedule'] = 'terminal' if arguments['primary'] else 'interim_descriptive'
    final['final_analysis_at_ms'] = choices.get('final_analysis_at_ms')
    final['stopping_amendment_hash'] = choices.get('stopping_amendment_hash')
    if not arguments['primary'] and final['status'] == 'ok':
        final['conclusion'] = 'descriptive only; I wait for the fixed terminal analysis'
    points = []
    prefixes = np.unique(np.linspace(1, len(times), min(len(times), max_points), dtype=int)) if times else []
    for n in prefixes:
        result = paired_inference(times[:n], svi[:n], other[:n], lead, **arguments)
        points.append({'expiry_ms':times[n-1], 'n_pairs':int(n), 'status':result['status'],
                       'mean_difference_usd':result['mean_difference_svi_minus_baseline'],
                       'effective_sample_size':result['effective_sample_size'],
                       'confidence_interval_95':result['bootstrap']['confidence_interval_95'] if result['bootstrap'] else None})
    return {'status':final['status'], 'primary':final, 'points':points, 'lead_ms':lead, 'baseline':baseline,
            'interval_label':'pointwise cumulative-mean stationary-bootstrap intervals; not simultaneous',
            'unpaired_forecasts':sum(set(p) != {'svi', baseline} for p in paired.values())}


def report_statistics(rows: list[ReportObservation], choices: dict, epsilon: float) -> dict:
    rows = [row for row in rows if row.expiry_ms <= choices.get('expiry_cutoff_ms',9223372036854775807)]
    seen = set()
    for row in rows:
        identity = (row.prediction_id, row.model)
        if identity in seen:
            raise ValueError('Duplicate frozen report score')
        seen.add(identity)
        if (not math.isfinite(row.metrics['crps_usd']) or row.metrics['crps_usd'] < 0
                or not 0 <= row.metrics['pit'] <= 1):
            raise ValueError('Invalid frozen score in report')
    primary = sorted((r for r in rows if r.model == 'svi' and r.lead_ms == choices['primary_lead_ms']), key=lambda r:r.expiry_ms)
    times = [r.expiry_ms for r in primary]
    reliability = []
    for side in ('left', 'right'):
        for p in (.01,.05,.1,.25):
            values = [r.tail_events[f'{side}_{p:g}'] for r in primary]
            reliability.append({'side':side, 'nominal_probability':p,
                **binary_summary(values,times,choices['primary_lead_ms'],choices)})
    groups = defaultdict(list)
    for row in rows:
        for classification in ('all',row.expiry_class):
            groups[(row.model,row.lead_ms,classification)].append(row)
    coverage = []
    for (model,lead,classification), group in sorted(groups.items()):
        group.sort(key=lambda r:r.expiry_ms)
        infinite = sum(r.diagnostics['negative_log_score_is_infinite'] for r in group)
        undefined = sum(r.diagnostics['log_score_status'] == 'support_atom' for r in group)
        for level in (50,80,90):
            item = binary_summary([r.metrics[f'coverage_{level}'] for r in group], [r.expiry_ms for r in group], lead, choices)
            coverage.append({'model':model,'lead_ms':lead,'expiry_class':classification,'level':level,**item,
                'infinite_log_losses':infinite,'undefined_continuous_log_losses':undefined,
                'mean_log_loss':None if infinite or undefined else float(np.mean([r.metrics['negative_log_score_log_return'] for r in group]))})
    temporal = cumulative_comparison(rows,choices)
    return {'status':temporal['status'], 'pit':histogram([r.metrics['pit'] for r in primary],choices,epsilon,times=times,lead=choices['primary_lead_ms']),
            'reliability':reliability,'coverage':coverage,'crps':temporal,
            'display_selection':'SVI at the declared primary lead; coverage also split by model, lead and expiry class',
            'tail_event_rule':'delivery <= lower quantile or delivery >= upper quantile; inclusive atoms can exceed nominal probability'}
