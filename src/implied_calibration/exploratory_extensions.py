"""Post-inspection scaling and fixed-weight comparisons on the matched HAR cohort."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np

from .incremental import VarianceCase
from .store import digest
from .variance_study import _metric_summary, qlike


MODELS = ('dvol', 'dvol_rescaled', 'har_dvol_equal', 'har_dvol', 'har')


def load_extension_spec(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding='utf-8'))
    if (spec['kind'] != 'exploratory_dvol_explanations_v1' or spec['schema_version'] != 1
            or type(spec['training_completed_origins']) is not int
            or spec['training_completed_origins'] != 365
            or spec['table_models'] != list(MODELS) or spec['paired_baseline'] != 'har_dvol'
            or spec['har_dvol_equal']['har_weight'] != .5
            or spec['har_dvol_equal']['dvol_weight'] != .5
            or spec['rescaled_dvol']['clipping'] != 'none'
            or not spec['rescaled_dvol']['floor'].startswith('none;')):
        raise ValueError('Expected the declared matched-cohort exploratory specification')
    return spec


def rolling_extensions(cases: list[VarianceCase], record: dict, window: int) -> dict:
    cutoff = record['feature_cutoff_ms']
    eligible = sorted((r for r in cases if r.actual is not None and r.target_end_ms <= cutoff
                       and r.feature_cutoff_ms < cutoff), key=lambda r: r.feature_cutoff_ms)
    if len(eligible) < window:
        raise ValueError('Expected the full completed-label extension window')
    train = eligible[-window:]
    metadata = {'n': len(train), 'first_feature_cutoff_ms': train[0].feature_cutoff_ms,
                'last_feature_cutoff_ms': train[-1].feature_cutoff_ms,
                'latest_target_end_ms': max(r.target_end_ms for r in train)}
    if any(record['training'][key] != value for key, value in metadata.items()):
        raise ValueError('Extension training rows differ from the matched HAR window')
    ratios = []
    for row in train:
        if (row.actual is None or not math.isfinite(row.actual) or row.actual <= 0
                or not math.isfinite(row.features[3]) or row.features[3] <= 0):
            raise ValueError('Expected positive finite realised and raw DVOL training variances')
        ratios.append(row.actual / row.features[3])
    factor = math.fsum(ratios) / window
    raw, har = record['forecasts']['dvol'], record['forecasts']['har']
    forecasts = {'dvol_rescaled': factor * raw, 'har_dvol_equal': .5 * har + .5 * raw}
    if not all(math.isfinite(v) and v > 0 for v in (factor, raw, har, *forecasts.values())):
        raise ValueError('Expected positive finite exploratory forecasts and scale')
    return {'forecasts': forecasts, 'scale_factor': factor, 'training': metadata}


def analyse_extensions(cases: list[VarianceCase], records: list[dict], plan: dict, spec: dict) -> dict:
    if spec['training_completed_origins'] != plan['training_completed_origins']:
        raise ValueError('Extension and HAR completed-label windows must agree')
    rows = []
    for record in records:
        extra = rolling_extensions(cases, record, spec['training_completed_origins'])
        forecasts = {**record['forecasts'], **extra['forecasts']}
        forecasts = {name: forecasts[name] for name in MODELS}
        losses = {name: qlike(record['actual'], forecasts[name]) for name in MODELS}
        rows.append({'origin_ms': record['origin_ms'], 'actual': record['actual'],
                     **extra, 'forecasts': forecasts, 'losses': losses})
    if not rows:
        raise ValueError('Expected matched exploratory observations')
    names = ['qlike_'+name for name in MODELS]
    names += ['qlike_'+name+'_minus_har_dvol' for name in MODELS]
    names += ['qlike_dvol_rescaled_minus_dvol', 'qlike_har_dvol_equal_minus_dvol', 'scale_factor']
    values = np.asarray([[*[r['losses'][name] for name in MODELS],
                          *[r['losses'][name]-r['losses']['har_dvol'] for name in MODELS],
                          r['losses']['dvol_rescaled']-r['losses']['dvol'],
                          r['losses']['har_dvol_equal']-r['losses']['dvol'],r['scale_factor']]
                         for r in rows])
    times = np.asarray([r['origin_ms'] for r in rows])
    years = np.asarray([datetime.fromtimestamp(t/1000, timezone.utc).year for t in times])
    annual: dict[str, dict] = {}
    for year in sorted(set(years)):
        selected = years == year
        annual[str(year)] = {'n': int(selected.sum()),
            'mean_qlike': {name: float(values[selected,i].mean()) for i,name in enumerate(MODELS)},
            'paired': {name: {'mean': float(np.mean([r['losses'][name]-r['losses']['har_dvol']
                                                   for r,y in zip(rows,years) if y == year]))}
                       for name in MODELS},
            'har_dvol_minus_har': float(np.mean([r['losses']['har_dvol']-r['losses']['har']
                                                for r,y in zip(rows,years) if y == year])),
            'raw_minus_augmented_sum': float(np.sum(values[selected,5]))}
    total = float(np.sum(values[:,5]))
    for entry in annual.values():
        entry['raw_signed_contribution_share'] = entry['raw_minus_augmented_sum']/total if total else None
    in_sample = {}
    for name in ('dvol','har_dvol'):
        ratio = math.fsum(r['actual']/r['forecasts'][name] for r in rows)/len(rows)
        in_sample[name] = {'factor':ratio,
            'mean_qlike':float(np.mean([qlike(r['actual'],ratio*r['forecasts'][name]) for r in rows])),
            'analytic_loss_reduction':ratio-1-math.log(ratio),
            'role':'in_sample_algebraic_scale_check_not_a_forecast'}
    return {'role':'exploratory_additions_after_inspection', 'formal_claim_eligible':False,
        'specification':spec, 'specification_sha256':digest(spec), 'n':len(rows),
        'summary':_metric_summary(values,times,names,plan),
        'block_sensitivities':{str(days):_metric_summary(values,times,names,plan,block_days=days)
                               for days in plan['sensitivity_block_days']},
        'annual':annual, 'in_sample_scale_check':in_sample, 'records':rows}
