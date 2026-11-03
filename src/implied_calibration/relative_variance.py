"""Matched ETH/BTC forecasts separate relative risk from common volatility."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess

import numpy as np
from scipy.special import expit

from .incremental import build_cases, loss_concentration
from .review_models import log_smearing, utc_ms
from .store import digest
from .variance_study import DAY_MS, _metric_summary, qlike


RATIO_MODELS = ('completed_mean', 'trailing_ratio', 'raw_dvol_ratio',
                'calibrated_dvol_ratio', 'ratio_har', 'ratio_har_dvol')
MARGINAL_MODELS = ('dvol', 'log_har', 'log_har_dvol')
PAIRS = (('ratio_har_dvol', 'ratio_har'), ('raw_dvol_ratio', 'ratio_har'),
         ('raw_dvol_ratio', 'completed_mean'), ('calibrated_dvol_ratio', 'raw_dvol_ratio'))


def load_relative_spec(root: Path) -> tuple[dict, dict]:
    path = root/'protocol/eth-relative-variance-v1.json'
    spec = json.loads(path.read_text(encoding='utf-8'))
    lock = json.loads(path.with_suffix('.lock.json').read_text(encoding='utf-8'))
    amendment_path = root/'protocol/eth-relative-variance-v1a.json'
    amendment = json.loads(amendment_path.read_text(encoding='utf-8'))
    if (spec.get('kind') != 'exploratory_eth_relative_variance'
            or digest(spec) != lock['sha256'] or spec['assets'] != ['BTC', 'ETH']
            or spec['ratio_models'] != list(RATIO_MODELS)
            or spec['marginal_models'] != list(MARGINAL_MODELS)
            or spec['horizon_days'] != 30 or spec['training_completed_labels'] != 730
            or amendment['parent_spec_sha256'] != digest(spec)):
        raise ValueError('Relative-variance declaration or lock differs')
    for asset, relative in spec['source_configs'].items():
        config = json.loads((root/relative).read_text(encoding='utf-8'))
        if digest(config) != lock['source_config_hashes'][asset]:
            raise ValueError('Relative-variance source configuration differs')
    commits = subprocess.check_output(['git', 'log', '--reverse', '--format=%H', '--',
                                      path.relative_to(root).as_posix()], cwd=root, text=True).splitlines()
    for commit in commits:
        saved = json.loads(subprocess.check_output(
            ['git', 'show', commit+':protocol/eth-relative-variance-v1.json'], cwd=root))
        if saved == spec:
            amendment_commits = subprocess.check_output(['git', 'log', '--reverse', '--format=%H', '--',
                'protocol/eth-relative-variance-v1a.json'], cwd=root, text=True).splitlines()
            witness = next((c for c in amendment_commits if json.loads(subprocess.check_output(
                ['git', 'show', c+':protocol/eth-relative-variance-v1a.json'], cwd=root)) == amendment), None)
            if witness is None:
                raise ValueError('Commit the decomposition diagnostics before evaluation')
            return spec, {'spec_sha256': digest(spec), 'declaration_commit': commit,
                          'amendment_sha256': digest(amendment), 'amendment_commit': witness,
                          'independent_timestamp': False, 'chronology_verified': False,
                          'role': 'retained_specification_content_witness'}
    raise ValueError('Commit the relative-variance declaration before evaluation')


@dataclass(frozen=True)
class PairedCase:
    origin_ms: int
    target_start_ms: int
    target_end_ms: int
    features: tuple[tuple[float, ...], tuple[float, ...]]
    actual: tuple[float | None, float | None]


def paired_cases(inputs: dict, start: int, end: int, *, lag_days: int = 0,
                 target_shift_hours: int = 0) -> list[PairedCase]:
    if lag_days not in (0, 1) or target_shift_hours not in (0, 1):
        raise ValueError('Undeclared relative-variance alignment sensitivity')
    sources = []
    for asset in ('BTC', 'ETH'):
        item = inputs[asset]
        dvol = {time+lag_days*DAY_MS: value for time, value in item.dvol.items()}
        rows = build_cases(item.prices, dvol, start, end, target_shift_hours=target_shift_hours)
        sources.append({row.feature_cutoff_ms: row for row in rows})
    output = []
    for origin in sorted(set(sources[0]) & set(sources[1])):
        btc, eth = (source[origin] for source in sources)
        if (btc.target_start_ms, btc.target_end_ms) != (eth.target_start_ms, eth.target_end_ms):
            raise ValueError('Paired targets must share the same return interval')
        output.append(PairedCase(origin, btc.target_start_ms, btc.target_end_ms,
                                 (btc.features, eth.features), (btc.actual, eth.actual)))
    return output


def finite_positive(array) -> np.ndarray:
    value = np.asarray(array, dtype=float)
    if not np.isfinite(value).all() or np.any(value <= 0):
        raise ValueError('Variance inputs must be positive and finite')
    return value


def linear_ratio_fit(x: np.ndarray, y: np.ndarray, current: np.ndarray) -> dict:
    if (x.ndim != 2 or y.shape != (len(x),) or current.shape != (x.shape[1],)
            or len(x) <= x.shape[1]+1 or not np.isfinite(x).all()
            or not np.isfinite(y).all() or not np.isfinite(current).all()):
        raise ValueError('Invalid direct log-ratio regression inputs')
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > np.finfo(float).eps, scale, 1.)
    design = np.column_stack((np.ones(len(x)), (x-mean)/scale))
    beta, _, rank, singular = np.linalg.lstsq(design, y, rcond=None)
    prediction = float(np.r_[1., (current-mean)/scale] @ beta)
    if not math.isfinite(prediction):
        raise ValueError('Nonfinite direct log-ratio forecast')
    return {'prediction': prediction, 'coefficients': beta.tolist(), 'rank': int(rank),
            'condition_number': float(singular[0]/singular[-1]) if singular[-1] > 0 else None}


def forecast_pair(cases: list[PairedCase], origin: int, window: int) -> dict:
    if type(window) is not int or window < 6:
        raise ValueError('Expected a completed-label window of at least six')
    if len({r.origin_ms for r in cases}) != len(cases):
        raise ValueError('Duplicate paired feature origins')
    current = next((r for r in cases if r.origin_ms == origin), None)
    if current is None:
        raise ValueError('Missing matched current features')
    eligible = sorted((r for r in cases if all(v is not None for v in r.actual)
                       and r.origin_ms < origin and r.target_end_ms <= origin), key=lambda r: r.origin_ms)
    if len(eligible) < window:
        raise ValueError('Insufficient matched completed training labels')
    train = eligible[-window:]
    x = finite_positive([r.features for r in train])
    y = finite_positive([r.actual for r in train])
    now = finite_positive(current.features)
    if x.shape != (window, 2, 4) or y.shape != (window, 2) or now.shape != (2, 4):
        raise ValueError('Expected matched two-asset four-feature observations')
    # Log differences avoid numerical overflow from division of positive variances.
    ratio_x = np.log(x[:, 1, :])-np.log(x[:, 0, :])
    ratio_y = np.log(y[:, 1])-np.log(y[:, 0])
    ratio_now = np.log(now[1])-np.log(now[0])
    fits = {name: linear_ratio_fit(ratio_x[:, :count], ratio_y, ratio_now[:count])
            for name, count in (('ratio_har', 3), ('ratio_har_dvol', 4))}
    offset = float(np.mean(ratio_y-ratio_x[:, 3]))
    forecasts = {'completed_mean': float(ratio_y.mean()), 'trailing_ratio': float(ratio_now[2]),
                 'raw_dvol_ratio': float(ratio_now[3]), 'calibrated_dvol_ratio': float(ratio_now[3]+offset),
                 **{name: fit['prediction'] for name, fit in fits.items()}}
    marginal = {}
    for index, asset in enumerate(('BTC', 'ETH')):
        marginal[asset] = {'dvol': float(now[index, 3]),
                          'log_har': log_smearing(x[:, index, :3], y[:, index], now[index, :3])['forecast'],
                          'log_har_dvol': log_smearing(x[:, index, :], y[:, index], now[index, :])['forecast']}
    return {'ratio_forecasts': forecasts, 'ratio_fits': fits, 'calibration_log_offset': offset,
            'marginal_forecasts': marginal, 'training_n': window,
            'first_training_origin_ms': train[0].origin_ms, 'last_training_origin_ms': train[-1].origin_ms,
            'latest_training_target_end_ms': max(r.target_end_ms for r in train)}


def standalone_weights(log_ratio: float) -> tuple[float, float]:
    if not math.isfinite(log_ratio):
        raise ValueError('Expected a finite log variance ratio')
    return float(expit(log_ratio/2)), float(expit(-log_ratio/2))


def evaluate_pairs(cases: list[PairedCase], spec: dict) -> tuple[list[dict], list[dict]]:
    by_origin = {r.origin_ms: r for r in cases}
    records, excluded = [], []
    for origin in range(utc_ms(spec['evaluation_start']), utc_ms(spec['evaluation_end'])+DAY_MS, DAY_MS):
        case = by_origin.get(origin)
        if case is None or any(v is None for v in case.actual):
            excluded.append({'origin_ms': origin, 'reason': 'missing_matched_features_or_targets'})
            continue
        try:
            fitted = forecast_pair(cases, origin, spec['training_completed_labels'])
        except (ValueError, np.linalg.LinAlgError, OverflowError) as exc:
            excluded.append({'origin_ms': origin, 'reason': str(exc)})
            continue
        actual = finite_positive(case.actual)
        target = float(np.log(actual[1])-np.log(actual[0]))
        losses = {name: (target-value)**2 for name, value in fitted['ratio_forecasts'].items()}
        marginal_losses = {asset: {name: qlike(float(actual[i]), value) for name, value
                                  in fitted['marginal_forecasts'][asset].items()}
                           for i, asset in enumerate(('BTC', 'ETH'))}
        if not all(math.isfinite(v) for v in losses.values()):
            raise ValueError('Nonfinite log-ratio squared error')
        records.append({'origin_ms': origin, 'target_start_ms': case.target_start_ms,
                        'target_end_ms': case.target_end_ms, 'actual': actual.tolist(),
                        'target_log_ratio': target, 'ratio_losses': losses,
                        'marginal_losses': marginal_losses, **fitted})
    if not records:
        raise ValueError('No matched relative-variance evaluation origins')
    return records, excluded


def metric_matrix(records: list[dict]) -> tuple[list[str], np.ndarray]:
    names = ['mse_'+name for name in RATIO_MODELS]
    names += [a+'_minus_'+b for a, b in PAIRS]
    names += [asset+'_qlike_'+name for asset in ('BTC', 'ETH') for name in MARGINAL_MODELS]
    names += [asset+'_log_har_dvol_minus_log_har' for asset in ('BTC', 'ETH')]
    names += ['ETH_minus_BTC_incremental_qlike']
    names += [model+'_'+part+'_squared_log_error' for model in MARGINAL_MODELS for part in ('common', 'relative')]
    names += ['marginal_common_increment', 'marginal_relative_increment']
    values = []
    for record in records:
        losses = record['ratio_losses']
        marginal = record['marginal_losses']
        increments = [marginal[asset]['log_har_dvol']-marginal[asset]['log_har'] for asset in ('BTC', 'ETH')]
        decomposition = marginal_decomposition(record)
        values.append([*[losses[name] for name in RATIO_MODELS],
                       *[losses[a]-losses[b] for a, b in PAIRS],
                       *[marginal[asset][name] for asset in ('BTC', 'ETH') for name in MARGINAL_MODELS],
                       *increments, increments[1]-increments[0],
                       *[decomposition[model][part] for model in MARGINAL_MODELS for part in ('common', 'relative')],
                       *[decomposition['log_har_dvol'][part]-decomposition['log_har'][part] for part in ('common', 'relative')]])
    return names, np.asarray(values, dtype=float)


def marginal_decomposition(record: dict) -> dict:
    output = {}
    for name in MARGINAL_MODELS:
        errors = [math.log(record['actual'][i])-math.log(record['marginal_forecasts'][asset][name])
                  for i, asset in enumerate(('BTC', 'ETH'))]
        common, relative = (errors[1]+errors[0])/2, errors[1]-errors[0]
        output[name] = {'common': common**2, 'relative': relative**2,
                        'average_asset_squared_error': (errors[0]**2+errors[1]**2)/2,
                        'identity_error': abs(common**2+relative**2/4-(errors[0]**2+errors[1]**2)/2)}
    return output


def correlation(a, b) -> float | None:
    a, b = np.asarray(a, float), np.asarray(b, float)
    if (a.ndim != 1 or a.shape != b.shape or not np.isfinite(a).all()
            or not np.isfinite(b).all() or len(a) < 3 or a.std() == 0 or b.std() == 0):
        return None
    return float(np.corrcoef(a, b)[0, 1])


def summarise_pairs(records: list[dict], spec: dict) -> dict:
    names, values = metric_matrix(records)
    times = np.asarray([r['origin_ms'] for r in records], dtype=np.int64)
    config = spec['inference']
    summary = _metric_summary(values, times, names, config)
    annual = {}
    for year in sorted({datetime.fromtimestamp(t/1000, timezone.utc).year for t in times}):
        selected = np.array([datetime.fromtimestamp(t/1000, timezone.utc).year == year for t in times])
        annual[str(year)] = {'n': int(selected.sum()),
                             'means': dict(zip(names, values[selected].mean(axis=0).tolist()))}
    contrasts = {a+'_minus_'+b: loss_concentration(records, values[:, names.index(a+'_minus_'+b)]) for a, b in PAIRS}
    phases = []
    for phase in range(30):
        selected = ((times-utc_ms(spec['evaluation_start']))//DAY_MS) % 30 == phase
        phases.append({'phase_days': phase, 'n': int(selected.sum()),
                       'means': {a+'_minus_'+b: float(values[selected, names.index(a+'_minus_'+b)].mean())
                                 if selected.any() else None for a, b in PAIRS}})
    midpoint = utc_ms(spec['evaluation_start']) + ((utc_ms(spec['evaluation_end'])-utc_ms(spec['evaluation_start']))//DAY_MS+1)//2*DAY_MS
    halves = {label: {'n': int(selected.sum()),
                     'means': dict(zip(names, values[selected].mean(axis=0).tolist())) if selected.any() else {}}
              for label, selected in (('first', times < midpoint), ('second', times >= midpoint))}
    increments = [[r['marginal_losses'][asset]['log_har_dvol']-r['marginal_losses'][asset]['log_har']
                   for r in records] for asset in ('BTC', 'ETH')]
    decomposition = {model: {
        'log_error_correlation': correlation(*[[math.log(r['actual'][i])-math.log(r['marginal_forecasts'][asset][model])
                                               for r in records] for i, asset in enumerate(('BTC', 'ETH'))]),
        'maximum_identity_error': max(marginal_decomposition(r)[model]['identity_error'] for r in records)}
        for model in MARGINAL_MODELS}
    return {'summary': summary, 'annual': annual, 'halves': halves, 'concentration': contrasts,
            'marginal_log_error_decomposition': decomposition,
            'phases': phases, 'marginal_increment_loss_correlation': correlation(*increments),
            'realised_variance_correlation': correlation(*[[r['actual'][i] for r in records] for i in (0, 1)]),
            'block_sensitivities': {str(days): _metric_summary(values, times, names, config, block_days=days)
                                    for days in config['sensitivity_block_days']},
            'formal_claim_eligible': False, 'role': 'exploratory_descriptive_even_if_interval_excludes_zero'}
