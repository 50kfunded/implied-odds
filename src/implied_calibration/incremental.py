"""Incremental DVOL comparisons use identical completed training labels."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math

import numpy as np
from scipy.stats import chi2

from .inference import newey_west
from .variance_study import DAY_MS, HOUR_MS, _metric_summary, hourly_variance, qlike


@dataclass(frozen=True)
class VarianceCase:
    feature_cutoff_ms: int
    target_start_ms: int
    target_end_ms: int
    features: tuple[float, float, float, float]
    actual: float | None


def features_at(prices: dict, dvol: dict, cutoff: int) -> tuple[float, float, float, float] | None:
    if type(cutoff) is not int or cutoff % DAY_MS:
        raise ValueError('Expected midnight UTC completed features')
    values = [hourly_variance(prices, cutoff - days * DAY_MS, cutoff, 8760)[0] for days in (1, 7, 30)]
    candle = dvol.get(cutoff - DAY_MS)
    if any(v is None for v in values) or candle is None or not math.isfinite(candle[3]) or candle[3] <= 0:
        return None
    first, weekly, monthly = values
    if first is None or weekly is None or monthly is None:
        return None
    return float(first), float(weekly), float(monthly), float((candle[3] / 100) ** 2)


def build_cases(prices: dict, dvol: dict, start: int, end: int, *, target_shift_hours: int = 0) -> list[VarianceCase]:
    if target_shift_hours not in (0, 1):
        raise ValueError('Expected a declared target alignment')
    output = []
    for cutoff in range(start, end + DAY_MS, DAY_MS):
        features = features_at(prices, dvol, cutoff)
        if features is None:
            continue
        target_start = cutoff + target_shift_hours * HOUR_MS
        actual = hourly_variance(prices, target_start, target_start + 30 * DAY_MS, 8760)[0]
        output.append(VarianceCase(cutoff, target_start, target_start + 30 * DAY_MS, features, actual))
    return output


def fit_forecasts(cases: list[VarianceCase], cutoff: int, features: tuple, plan: dict) -> dict | None:
    window = plan['training_completed_origins']
    eligible = sorted((r for r in cases if r.actual is not None and r.target_end_ms <= cutoff
                       and r.feature_cutoff_ms < cutoff), key=lambda r: r.feature_cutoff_ms)
    if len(eligible) < window:
        return None
    train = eligible[-window:]
    x = np.asarray([r.features for r in train], dtype=float)
    y = np.asarray([r.actual for r in train], dtype=float)
    current = np.asarray(features, dtype=float)
    if x.shape != (window, 4) or current.shape != (4,) or not np.isfinite(x).all() or not np.isfinite(y).all() or not np.isfinite(current).all():
        raise ValueError('Expected finite matched features and completed targets')
    forecasts = {'trailing': float(current[2]), 'dvol': float(current[3])}
    fits = {}
    for name, count in (('har', 3), ('har_dvol', 4)):
        mean, scale = x[:, :count].mean(axis=0), x[:, :count].std(axis=0)
        scale = np.where(scale > np.finfo(float).eps, scale, 1.)
        design = np.column_stack((np.ones(window), (x[:, :count] - mean) / scale))
        beta, _, rank, singular = np.linalg.lstsq(design, y, rcond=None)
        raw = float(np.r_[1., (current[:count] - mean) / scale] @ beta)
        if not math.isfinite(raw):
            raise ValueError('Cannot freeze a nonfinite regression prediction')
        forecasts[name] = max(plan['forecast_floor_variance'], raw)
        fits[name] = {'raw_prediction': raw, 'floor_hit': raw < plan['forecast_floor_variance'],
                      'coefficients_scaled': beta.tolist(), 'feature_mean': mean.tolist(), 'feature_scale': scale.tolist(),
                      'rank': int(rank), 'condition_number': float(singular[0] / singular[-1]) if singular[-1] > 0 else None}
    return {'forecasts': forecasts, 'fits': fits, 'training': {'n': window,
            'first_feature_cutoff_ms': train[0].feature_cutoff_ms, 'last_feature_cutoff_ms': train[-1].feature_cutoff_ms,
            'latest_target_end_ms': max(r.target_end_ms for r in train), 'same_rows_for_both_models': True}}


def regression_diagnostic(y: np.ndarray, x: np.ndarray, null: np.ndarray, lags: int = 59) -> dict:
    """Report level-regression HAC diagnostics as descriptive checks."""
    y, x = np.asarray(y, dtype=float), np.asarray(x, dtype=float)
    if x.ndim != 2 or len(x) != len(y) or len(x) <= max(lags, x.shape[1]):
        return {'status': 'insufficient data'}
    scale = np.std(x, axis=0)
    scale = np.where(scale > 1e-12, scale, 1.)
    z = x / scale
    beta_scaled, _, rank, _ = np.linalg.lstsq(z, y, rcond=None)
    beta = beta_scaled / scale
    residual = y - z @ beta_scaled
    bread = np.linalg.pinv(z.T @ z / len(z))
    covariance_z = bread @ newey_west(z * residual[:, None], min(lags, len(z)-1)) @ bread / len(z)
    covariance = covariance_z / np.outer(scale, scale)
    se = np.sqrt(np.maximum(0., np.diag(covariance)))
    delta = beta - null
    valid = rank == x.shape[1] and np.linalg.matrix_rank(covariance) == x.shape[1]
    statistic = float(delta @ np.linalg.solve(covariance, delta)) if valid else None
    return {'status': 'descriptive' if valid else 'singular', 'n': len(y), 'coefficients': beta.tolist(),
            'hac_standard_errors': se.tolist(), 'joint_null': null.tolist(), 'joint_wald': statistic,
            'asymptotic_p_value': float(chi2.sf(statistic, len(beta))) if valid else None,
            'hac_lags': lags, 'role': 'exploratory_no_multiple_testing_claim',
            'limit': 'Diagnostics use contemporaneous level residuals; overlapping targets and generated rolling forecasts affect inference.'}


def risk_budget(actual: float, forecast: float, plan: dict) -> dict:
    budget = plan['decision']['annual_volatility_budget']
    weight = min(plan['decision']['maximum_weight'], budget / math.sqrt(forecast))
    variance = weight * weight * actual
    return {'weight': weight, 'realised_exposure_variance': variance,
            'squared_variance_budget_error': (variance - budget * budget) ** 2,
            'budget_exceeded': variance > budget * budget,
            'entry_exit_fee_fraction': {str(bps): 2 * bps / 10000 * weight for bps in plan['decision']['entry_exit_fee_bps']}}


def loss_concentration(records: list[dict], difference: np.ndarray) -> dict:
    absolute = np.abs(difference)
    denominator = float(absolute.sum())
    descending = np.argsort(-absolute, kind='stable')
    contributions = {}
    for percent in (1, 5):
        count = max(1, math.ceil(len(difference) * percent / 100))
        selected = descending[:count]
        contributions[str(percent)] = {'n': count, 'share_absolute_loss_difference': float(absolute[selected].sum()/denominator) if denominator else 0.,
            'signed_sum': float(difference[selected].sum())}
    quarters: dict[str, list[float]] = {}
    years: dict[str, list[float]] = {}
    for record, value in zip(records, difference):
        date = datetime.fromtimestamp(record['origin_ms']/1000, timezone.utc)
        quarters.setdefault(f'{date.year}-Q{(date.month-1)//3+1}', []).append(float(value))
        years.setdefault(str(date.year), []).append(float(value))
    total = float(difference.sum())
    return {'role': 'exploratory_fixed_calendar_episodes', 'top_absolute_contributions': contributions,
        'quarters': {key: {'n': len(x), 'mean': float(np.mean(x)), 'sum': float(np.sum(x))} for key,x in quarters.items()},
        'leave_one_year_out': {key: {'n': len(difference)-len(x), 'mean': (total-math.fsum(x))/(len(difference)-len(x))}
                              for key,x in years.items() if len(x) < len(difference)},
        'worst_absolute_origins': [{'origin_ms': records[i]['origin_ms'], 'difference': float(difference[i])} for i in descending[:10]],
        'interpretation': "Large contributions are identified after inspection of losses and do not define an ex-ante trading regime."}


def analyse_incremental(prices: dict, dvol: dict, historical: dict, plan: dict) -> dict:
    cases = build_cases(prices, dvol, historical['origin_start_ms'], historical['origin_end_ms'])
    records, exclusions = [], []
    by_cutoff = {r.feature_cutoff_ms: r for r in cases}
    for origin in range(plan['historical_origin_start_ms'], plan['historical_origin_end_ms'] + DAY_MS, DAY_MS):
        row = by_cutoff.get(origin)
        fit = fit_forecasts(cases, origin, row.features, plan) if row is not None else None
        if row is None or row.actual is None or fit is None:
            exclusions.append({'origin_ms': origin, 'reason': 'missing_features_target_or_completed_training'})
            continue
        forecasts = fit['forecasts']
        losses = {name: qlike(row.actual, value) for name,value in forecasts.items()}
        decisions = {name: risk_budget(row.actual, value, plan) for name,value in forecasts.items()}
        records.append({'origin_ms': origin, 'target_start_ms': row.target_start_ms, 'target_end_ms': row.target_end_ms,
                        'feature_cutoff_ms': row.feature_cutoff_ms, 'features': list(row.features), 'actual': row.actual,
                        **fit, 'losses': losses, 'decision': decisions})
    if not records:
        raise ValueError('No matched incremental-information observations are available')
    names = ['qlike_'+name for name in plan['models']] + ['qlike_har_dvol_minus_har', 'qlike_dvol_minus_har',
             'risk_budget_har_dvol_minus_har', 'qlike_dvol_minus_har_dvol']
    values = np.asarray([[*[r['losses'][name] for name in plan['models']],
         r['losses']['har_dvol']-r['losses']['har'], r['losses']['dvol']-r['losses']['har'],
         r['decision']['har_dvol']['squared_variance_budget_error']-r['decision']['har']['squared_variance_budget_error'],
         r['losses']['dvol']-r['losses']['har_dvol']]
         for r in records])
    times = np.asarray([r['origin_ms'] for r in records])
    summary = _metric_summary(values, times, names, plan)
    y = np.asarray([r['actual'] for r in records])
    diagnostics = {name: regression_diagnostic(y, np.column_stack((np.ones(len(y)), [r['forecasts'][name] for r in records])), np.array([0.,1.]))
                   for name in plan['models']}
    encompassing = regression_diagnostic(y, np.column_stack((np.ones(len(y)), [r['forecasts']['har'] for r in records],
              [r['forecasts']['har_dvol']-r['forecasts']['har'] for r in records])), np.array([0.,1.,0.]))
    encompassing['joint_null_scope'] = 'HAR calibration plus zero increment coefficient'
    coefficient, se = encompassing['coefficients'][2], encompassing['hac_standard_errors'][2]
    encompassing['increment_only_unrestricted_HAR'] = {
        'coefficient': coefficient, 'hac_standard_error': se,
        'null': 'increment coefficient zero; intercept and HAR slope unrestricted',
        'asymptotic_p_value': float(chi2.sf((coefficient/se)**2,1)) if se > 0 else None,
        'role': 'exploratory_secondary_generated_forecasts'}
    decision = {name: {key: float(np.mean([r['decision'][name][key] for r in records]))
                  for key in ('weight','realised_exposure_variance','squared_variance_budget_error','budget_exceeded')}
                for name in plan['models']}
    phases = []
    for phase in range(30):
        selected = np.array([(t-times[0])//DAY_MS % 30 == phase for t in times])
        phases.append({'phase_days': phase, 'n': int(selected.sum()),
                       'mean_har_dvol_minus_har': float(values[selected,4].mean()) if selected.any() else None})
    return {'kind': 'exploratory_incremental_variance', 'synthetic': False, 'formal_claim_eligible': False,
        'historical_role': plan['historical_role'], 'summary': summary, 'n': len(records), 'excluded': exclusions,
        'floor_hits': {name: sum(r['fits'][name]['floor_hit'] for r in records) for name in ('har','har_dvol')},
        'raw_prediction_minimum': {name: min(r['fits'][name]['raw_prediction'] for r in records) for name in ('har','har_dvol')},
        'block_sensitivities': {str(days): _metric_summary(values, times, names, plan, block_days=days)
                                for days in plan['sensitivity_block_days']},
        'mincer_zarnowitz': diagnostics, 'encompassing': encompassing,
        'concentration': {name: loss_concentration(records, np.array([r['losses'][name]-r['losses'][baseline] for r in records]))
                          for name,baseline in (('har_dvol','har'),('dvol','trailing'))},
        'nonoverlap_phases': phases, 'decision': decision,
        'decision_scope': plan['decision']['scope'],
        'fee_summary': {name: {str(bps): float(np.mean([r['decision'][name]['entry_exit_fee_fraction'][str(bps)] for r in records]))
                          for bps in plan['decision']['entry_exit_fee_bps']} for name in plan['models']},
        'records': records}
