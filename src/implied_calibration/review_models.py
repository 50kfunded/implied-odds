"""Declared exploratory benchmarks use completed labels and recorded failures."""

from datetime import datetime
import json
import math
from pathlib import Path
import subprocess
import warnings

import numpy as np
from scipy.optimize import minimize

from .incremental import VarianceCase, fit_forecasts
from .store import digest
from .variance_study import DAY_MS, HOUR_MS, qlike

MODEL_NAMES = ('dvol', 'har_365', 'har_dvol_365', 'har_730', 'har_dvol_730', 'har_expanding',
               'har_dvol_expanding', 'log_har_smear', 'log_har_dvol_smear', 'qlike_har',
               'qlike_har_dvol', 'ridge_qlike_har_dvol', 'ewma_094', 'garch_11', 'egarch_111',
               'bates_granger', 'mz_dvol')

def utc_ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()*1000)


def load_review_spec(root: Path) -> tuple[dict, dict]:
    folder = Path(root)/'protocol'
    path = folder/'exploratory-review-v2.json'
    spec = json.loads(path.read_text(encoding='utf-8'))
    lock = json.loads(path.with_suffix('.lock.json').read_text(encoding='utf-8'))
    amendment = json.loads((folder/'exploratory-review-v2a.json').read_text(encoding='utf-8'))
    if (spec['kind'] != 'exploratory_review_v2' or lock['sha256'] != digest(spec)
            or amendment['parent_spec_sha256'] != digest(spec)
            or spec['horizon_days'] != 30 or spec['training_labels'] != 730
            or spec['models'] != list(MODEL_NAMES)):
        raise ValueError('Expected the unchanged v2 declaration and its pre-evaluation amendment')
    witnesses = {}
    for name in ('exploratory-review-v2.json', 'exploratory-review-v2a.json'):
        relative = 'protocol/'+name
        commits = subprocess.check_output(['git', 'log', '--reverse', '--format=%H', '--', relative],
                                          cwd=root, text=True).splitlines()
        current = json.loads((folder/name).read_text(encoding='utf-8'))
        for commit in commits:
            saved = json.loads(subprocess.check_output(['git', 'show', commit+':'+relative], cwd=root))
            if saved == current:
                witnesses[name] = commit
                break
        if name not in witnesses:
            raise ValueError('Commit the declared specification before evaluating it')
    result = {**spec, 'exploratory_focus': amendment['exploratory_focus']}
    return result, {'parent_spec_hash': digest(spec), 'amendment_hash': digest(amendment),
                    'witness_commits': witnesses, 'role': 'publicly_declared_after_v1_inspection'}


def completed_rows(cases: list[VarianceCase], cutoff: int, count: int | None) -> list[VarianceCase]:
    rows = sorted((r for r in cases if r.actual is not None and r.target_end_ms <= cutoff
                   and r.feature_cutoff_ms < cutoff), key=lambda r: r.feature_cutoff_ms)
    if count is not None:
        if len(rows) < count:
            raise ValueError('Insufficient fully completed labels for the declared window')
        rows = rows[-count:]
    return rows


def log_design(x: np.ndarray, current: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not np.isfinite(x).all() or not np.isfinite(current).all() or np.any(x <= 0) or np.any(current <= 0):
        raise ValueError('Log features must be positive and finite')
    logged = np.log(x)
    mean, scale = logged.mean(axis=0), logged.std(axis=0)
    scale = np.where(scale > np.finfo(float).eps, scale, 1.)
    return np.column_stack((np.ones(len(x)), (logged-mean)/scale)), np.r_[1., (np.log(current)-mean)/scale]


def positive(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Nonpositive or nonfinite declared-model forecast')
    return value


def log_smearing(x, y, current) -> dict:
    x, y, current = np.asarray(x, float), np.asarray(y, float), np.asarray(current, float)
    if y.ndim != 1 or len(y) != len(x) or not np.isfinite(y).all() or np.any(y <= 0):
        raise ValueError('Log targets must be positive and finite')
    design, now = log_design(x, current)
    beta = np.linalg.lstsq(design, np.log(y), rcond=None)[0]
    residual = np.log(y)-design@beta
    factor = float(np.mean(np.exp(residual)))
    forecast = positive(float(np.exp(now@beta))*factor)
    return {'forecast': forecast, 'smearing_factor': factor, 'coefficients': beta.tolist(),
            'training_n': len(y), 'floor_hit': False}


def qlike_objective(beta: np.ndarray, design: np.ndarray, y: np.ndarray, penalty: float) -> tuple[float, np.ndarray]:
    eta = design@beta
    if not np.isfinite(eta).all() or np.max(np.abs(eta)) > 700:
        return math.inf, np.zeros_like(beta)
    ratio = y*np.exp(-eta)
    objective = float(np.mean(ratio+eta)+penalty*(beta[1:]@beta[1:])/2)
    gradient = design.T@(1-ratio)/len(y)
    gradient[1:] += penalty*beta[1:]
    return objective, gradient


def qlike_fit(x, y, current, settings: dict, penalty: float = 0.) -> dict:
    x, y, current = np.asarray(x, float), np.asarray(y, float), np.asarray(current, float)
    if y.ndim != 1 or len(y) != len(x) or not np.isfinite(y).all() or np.any(y <= 0) or penalty < 0:
        raise ValueError('Expected positive finite QLIKE targets and a nonnegative penalty')
    design, now = log_design(x, current)
    start = np.r_[math.log(float(y.mean())), np.zeros(design.shape[1]-1)]
    result = minimize(qlike_objective, start, args=(design, y, penalty), method='L-BFGS-B', jac=True,
                      options={k: settings[k] for k in ('maxiter', 'ftol', 'gtol')})
    if not result.success or not np.isfinite(result.x).all():
        raise ValueError('QLIKE optimizer failed: '+str(result.message))
    forecast = positive(float(np.exp(now@result.x)))
    return {'forecast': forecast, 'coefficients': result.x.tolist(), 'penalty': penalty,
            'objective': float(result.fun), 'iterations': int(result.nit), 'training_n': len(y), 'floor_hit': False}


def daily_returns(prices: dict, cutoff: int, days: int = 730) -> np.ndarray:
    times = range(cutoff-days*DAY_MS, cutoff+DAY_MS, DAY_MS)
    candles = [prices.get(stamp-HOUR_MS) for stamp in times]
    values = []
    for candle in candles:
        if candle is None:
            raise ValueError('Missing completed daily close in return training window')
        values.append(float(candle[3]))
    closes = np.asarray(values, dtype=float)
    if not np.isfinite(closes).all() or np.any(closes <= 0):
        raise ValueError('Invalid completed daily close')
    return np.diff(np.log(closes))


def ewma_forecast(returns, decay: float = .94, initial: int = 30) -> float:
    returns = np.asarray(returns, float)
    if returns.ndim != 1 or len(returns) < initial or not np.isfinite(returns).all() or not 0 < decay < 1:
        raise ValueError('Invalid declared EWMA training returns')
    variance = float(np.mean(returns[:initial]**2))
    for value in returns[initial:]:
        variance = decay*variance+(1-decay)*value*value
    return positive(float(365*variance))


def garch_forecast(returns, cutoff: int, settings: dict, *, egarch: bool = False) -> dict:
    from arch import arch_model
    returns = np.asarray(returns, float)
    model = arch_model(settings['return_scale']*returns, mean='Zero', vol='EGARCH' if egarch else 'GARCH',
                       p=settings['p'], o=settings['o_egarch'] if egarch else 0, q=settings['q'],
                       dist='normal', rescale=False)
    with warnings.catch_warnings(record=True) as caught:
        fitted = model.fit(disp='off', show_warning=False, options={'maxiter': settings['optimizer_maxiter']})
    if fitted.convergence_flag != 0:
        raise ValueError('GARCH optimizer failed with flag '+str(fitted.convergence_flag))
    diagnostics: dict = {'parameters': {str(k): float(v) for k, v in fitted.params.items()},
                   'warnings': [str(item.message) for item in caught]}
    if egarch:
        seed = settings['seed']+cutoff//DAY_MS
        batches = []
        for batch in (0, 1):
            rng = np.random.default_rng(seed+batch*10_000_000)
            forecast = fitted.forecast(horizon=settings['forecast_days'], method='simulation',
                                       simulations=settings['simulation_paths'], rng=rng.standard_normal,
                                       reindex=False)
            paths = np.asarray(forecast.simulations.variances)[-1].mean(axis=1)*365/settings['return_scale']**2
            if not np.isfinite(paths).all():
                raise ValueError('Nonfinite EGARCH simulation path')
            batches.append(paths)
        value = float(batches[0].mean())
        diagnostics.update(simulation_seed=seed, simulation_paths=settings['simulation_paths'],
                           monte_carlo_standard_error=float(batches[0].std(ddof=1)/math.sqrt(len(batches[0]))),
                           independent_batch_relative_gap=float((batches[1].mean()-value)/value) if value > 0 else None)
    else:
        forecast = fitted.forecast(horizon=settings['forecast_days'], method='analytic', reindex=False)
        value = float(np.asarray(forecast.variance)[-1].mean()*365/settings['return_scale']**2)
    return {'forecast': positive(value), **diagnostics, 'training_n': len(returns), 'floor_hit': False}


def bates_granger(vintages: list[dict], cutoff: int, har: float, dvol: float, count: int = 365) -> dict:
    rows = sorted((r for r in vintages if r['target_end_ms'] <= cutoff and r['origin_ms'] < cutoff),
                  key=lambda r: r['origin_ms'])[-count:]
    if len(rows) != count:
        raise ValueError('Insufficient completed genuine forecast vintages for combination weights')
    if any(r.get('har') is None for r in rows):
        raise ValueError('A required reconstructed HAR forecast vintage failed')
    ehar = np.asarray([r['actual']-r['har'] for r in rows])
    edvol = np.asarray([r['actual']-r['dvol'] for r in rows])
    denominator = float(np.mean((ehar-edvol)**2))
    tolerance = np.finfo(float).eps*max(float(np.mean(ehar**2)), float(np.mean(edvol**2)), np.finfo(float).tiny)
    raw = float((np.mean(edvol**2)-np.mean(ehar*edvol))/denominator) if denominator > tolerance else .5
    weight = float(np.clip(raw, 0, 1))
    return {'forecast': positive(weight*har+(1-weight)*dvol), 'har_weight': weight, 'unconstrained_weight': raw,
            'training_n': count, 'latest_weight_target_end_ms': max(r['target_end_ms'] for r in rows), 'floor_hit': False}


def mz_forecast(x, y, current: float, floor: float) -> dict:
    x, y = np.asarray(x, float), np.asarray(y, float)
    design = np.column_stack((np.ones(len(x)), x))
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    raw = float(beta[0]+beta[1]*current)
    if not math.isfinite(raw):
        raise ValueError('Nonfinite MZ prediction')
    return {'forecast': max(floor, raw), 'raw_prediction': raw, 'floor_hit': raw < floor,
            'coefficients': beta.tolist(), 'training_n': len(y)}


def window_forecasts(cases: list[VarianceCase], row: VarianceCase, plan: dict, *, strict: bool = True) -> dict:
    output = {'dvol': row.features[3]}
    diagnostics = {}
    failures: list[dict] = []
    available = len(completed_rows(cases, row.feature_cutoff_ms, None))
    for name, count in (('365', 365), ('730', 730), ('expanding', available)):
        try:
            fitted = fit_forecasts(cases, row.feature_cutoff_ms, row.features,
                                   {**plan, 'training_completed_origins': count}) if count else None
            if fitted is None:
                raise ValueError('Insufficient completed window labels')
        except (ValueError, np.linalg.LinAlgError) as exc:
            if strict:
                raise
            failures.extend({'model': model+'_'+name, 'reason': str(exc)} for model in ('har', 'har_dvol'))
            continue
        for model in ('har', 'har_dvol'):
            key = model+'_'+name
            output[key] = fitted['forecasts'][model]
            diagnostics[key] = {**fitted['fits'][model], 'training': fitted['training']}
    return {'forecasts': output, 'diagnostics': diagnostics, 'failures': failures}


def vintage_forecasts(cases: list[VarianceCase], plan: dict) -> list[dict]:
    output = []
    for row in cases:
        if row.actual is None:
            continue
        try:
            fitted = fit_forecasts(cases, row.feature_cutoff_ms, row.features, plan)
        except np.linalg.LinAlgError as exc:
            output.append({'origin_ms': row.feature_cutoff_ms, 'target_end_ms': row.target_end_ms,
                           'actual': row.actual, 'har': None, 'dvol': row.features[3], 'failure': str(exc)})
            continue
        if fitted is not None:
            output.append({'origin_ms': row.feature_cutoff_ms, 'target_end_ms': row.target_end_ms,
                           'actual': row.actual, 'har': fitted['forecasts']['har'], 'dvol': row.features[3],
                           'latest_training_target_end_ms': fitted['training']['latest_target_end_ms']})
    return output


def benchmark_records(cases: list[VarianceCase], prices: dict, plan: dict, spec: dict,
                      *, progress=None) -> dict:
    start, end = utc_ms(spec['common_origin_start']), utc_ms(spec['common_origin_end'])
    by_cutoff = {r.feature_cutoff_ms: r for r in cases}
    vintages = vintage_forecasts(cases, plan)
    records, failures = [], []
    for origin in range(start, end+DAY_MS, DAY_MS):
        row = by_cutoff.get(origin)
        if row is None or row.actual is None:
            failures.append({'origin_ms': origin, 'model': 'all', 'reason': 'missing features or target'})
            continue
        fitted = window_forecasts(cases, row, plan, strict=False)
        forecasts, diagnostics = fitted['forecasts'], fitted['diagnostics']
        failures.extend({'origin_ms': origin, **failure} for failure in fitted['failures'])
        available = completed_rows(cases, origin, None)
        train = available[-spec['training_labels']:]
        x = np.asarray([r.features for r in train], dtype=float).reshape((-1, 4))
        y = np.asarray([r.actual for r in train], dtype=float)
        current = np.asarray(row.features)
        for name in spec['models']:
            if name in forecasts:
                continue
            if name in MODEL_NAMES[1:7]:
                continue
            try:
                if (name.startswith('log_') or 'qlike_' in name or name == 'mz_dvol') and len(train) < spec['training_labels']:
                    raise ValueError('Insufficient fully completed labels for the declared fitted model')
                if name.startswith('log_'):
                    count = 4 if 'dvol' in name else 3
                    fit = log_smearing(x[:, :count], y, current[:count])
                elif 'qlike_' in name:
                    count = 4 if 'dvol' in name else 3
                    penalty = spec['qlike']['ridge_penalty'] if name.startswith('ridge_') else 0.
                    fit = qlike_fit(x[:, :count], y, current[:count], spec['qlike'], penalty)
                elif name == 'mz_dvol':
                    fit = mz_forecast(x[:, 3], y, current[3], spec['positive_floor'])
                elif name == 'bates_granger':
                    if 'har_365' not in forecasts:
                        raise ValueError('The declared HAR365 combination component is unavailable')
                    fit = bates_granger(vintages, origin, forecasts['har_365'], forecasts['dvol'])
                else:
                    returns = daily_returns(prices, origin, spec['garch']['return_days'])
                    fit = ({'forecast': ewma_forecast(returns), 'floor_hit': False} if name == 'ewma_094'
                           else garch_forecast(returns, origin, spec['garch'], egarch=name == 'egarch_111'))
                forecasts[name], diagnostics[name] = positive(fit['forecast']), fit
            except (ValueError, FloatingPointError, OverflowError, np.linalg.LinAlgError) as exc:
                failures.append({'origin_ms': origin, 'model': name, 'reason': str(exc)})
        records.append({'origin_ms': origin, 'target_end_ms': row.target_end_ms, 'actual': row.actual,
                        'latest_training_target_end_ms': max((r.target_end_ms for r in train), default=None),
                        'forecasts': forecasts, 'diagnostics': diagnostics,
                        'losses': {name: qlike(row.actual, value) for name, value in forecasts.items()}})
        if progress is not None and len(records) % 100 == 0:
            progress({'processed': len(records), 'origin_ms': origin, 'failures': len(failures)})
    common = [r for r in records if set(spec['models']).issubset(r['forecasts'])]
    return {'records': records, 'common_records': common, 'failures': failures,
            'n_scheduled': (end-start)//DAY_MS+1,
            'coverage': {name: sum(name in r['forecasts'] for r in records) for name in spec['models']},
            'floor_hits': {name: sum(bool(r['diagnostics'].get(name, {}).get('floor_hit')) for r in records)
                           for name in spec['models']},
            'raw_prediction_minimum': {name: min((r['diagnostics'][name]['raw_prediction'] for r in records
                                               if 'raw_prediction' in r['diagnostics'].get(name, {})), default=None)
                                       for name in spec['models']},
            'egarch_monte_carlo': {
                'maximum_absolute_independent_batch_relative_gap': max((abs(r['diagnostics']['egarch_111']['independent_batch_relative_gap'])
                    for r in records if 'egarch_111' in r['diagnostics']), default=None),
                'maximum_standard_error': max((r['diagnostics']['egarch_111']['monte_carlo_standard_error']
                    for r in records if 'egarch_111' in r['diagnostics']), default=None)}}
