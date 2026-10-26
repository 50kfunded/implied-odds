"""Historical variance forecasts use past-only inputs and later outcomes."""

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math

import numpy as np

from .inference import effective_sample_size, newey_west


HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class VarianceForecast:
    origin_ms: int
    dvol_known_at_ms: int
    trailing_known_at_ms: int
    dvol_variance: float
    trailing_variance: float


@dataclass(frozen=True)
class VarianceObservation:
    forecast: VarianceForecast
    target_start_ms: int
    target_end_ms: int
    realised_variance: float
    correction: dict | None


def hourly_variance(prices: dict, start_ms: int, end_ms: int, annual_hours: int) -> tuple[float | None, str]:
    if (type(start_ms) is not int or type(end_ms) is not int or start_ms % HOUR_MS or end_ms % HOUR_MS
            or not 0 <= start_ms < end_ms or type(annual_hours) is not int or annual_hours <= 0):
        raise ValueError('Invalid realised-variance interval or annualisation')
    values = [prices.get(closed - HOUR_MS) for closed in range(start_ms, end_ms + HOUR_MS, HOUR_MS)]
    if any(value is None for value in values):
        return None, 'missing_prices'
    closes = np.asarray([value[3] for value in values if value is not None], dtype=float)
    if not np.isfinite(closes).all() or np.any(closes <= 0):
        return None, 'invalid_prices'
    returns = np.diff(np.log(closes))
    variance = float(returns @ returns * annual_hours / len(returns))
    return (variance, 'ok') if variance > 0 else (None, 'zero_variance')


def historical_forecast(prices: dict, dvol: dict, origin_ms: int, config: dict,
                        *, dvol_lag_days: int = 0) -> tuple[VarianceForecast | None, list[str]]:
    if type(origin_ms) is not int or origin_ms % DAY_MS or type(dvol_lag_days) is not int or dvol_lag_days < 0:
        raise ValueError('Expected a daily UTC origin and nonnegative DVOL lag')
    reasons = []
    opened = origin_ms - (dvol_lag_days + 1) * DAY_MS
    candle = dvol.get(opened)
    if candle is None:
        reasons.append('missing_dvol')
    elif not math.isfinite(candle[3]) or candle[3] <= 0:
        reasons.append('invalid_dvol')
    trailing, reason = hourly_variance(prices, origin_ms - config['horizon_hours'] * HOUR_MS,
                                       origin_ms, config['annual_hours'])
    if reason != 'ok':
        reasons.append('trailing_' + reason)
    if reasons:
        return None, reasons
    assert candle is not None and trailing is not None
    known = opened + DAY_MS
    assert known <= origin_ms
    return VarianceForecast(origin_ms, known, origin_ms, (candle[3] / 100) ** 2, trailing), []


def qlike(actual: float, forecast: float) -> float:
    if not all(math.isfinite(value) and value > 0 for value in (actual, forecast)):
        raise ValueError('QLIKE requires positive finite realised and forecast variance')
    relative = (actual - forecast) / forecast
    return float(relative - math.log1p(relative)) if relative > -1 else float(actual / forecast - math.log(actual / forecast) - 1)


def fit_correction(past: list[VarianceObservation], origin_ms: int, window: int) -> dict | None:
    if type(window) is not int or window < 1:
        raise ValueError('Invalid completed-origin correction window')
    eligible = sorted((row for row in past if row.target_end_ms <= origin_ms), key=lambda row: row.forecast.origin_ms)
    if len(eligible) < window:
        return None
    training = eligible[-window:]
    assert all(row.target_end_ms <= origin_ms and row.forecast.origin_ms < origin_ms for row in training)
    ratios = [row.realised_variance / row.forecast.dvol_variance for row in training]
    factor = math.fsum(ratios) / len(ratios)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('Invalid historical correction factor')
    return {'factor': factor, 'n_completed_origins': len(training),
            'first_training_origin_ms': training[0].forecast.origin_ms,
            'last_training_origin_ms': training[-1].forecast.origin_ms,
            'latest_training_outcome_ms': max(row.target_end_ms for row in training)}


def build_observations(prices: dict, dvol: dict, config: dict, *, dvol_lag_days: int = 0,
                        future_start_shift_hours: int = 0) -> tuple[list[VarianceObservation], list[dict]]:
    if future_start_shift_hours not in (0, 1):
        raise ValueError('Undeclared historical outcome alignment sensitivity')
    rows: list[VarianceObservation] = []
    excluded: list[dict] = []
    for origin in range(config['origin_start_ms'], config['origin_end_ms'] + DAY_MS, DAY_MS):
        forecast, reasons = historical_forecast(prices, dvol, origin, config, dvol_lag_days=dvol_lag_days)
        start = origin + future_start_shift_hours * HOUR_MS
        end = start + config['horizon_hours'] * HOUR_MS
        actual, reason = hourly_variance(prices, start, end, config['annual_hours'])
        if reason != 'ok':
            reasons.append('future_' + reason)
        if reasons:
            excluded.append({'origin_ms': origin, 'reasons': reasons})
            continue
        assert forecast is not None and actual is not None
        correction = fit_correction(rows, origin, config['recalibration_completed_origins']) if origin >= config['recalibration_start_ms'] else None
        rows.append(VarianceObservation(forecast, start, end, actual, correction))
    return rows, excluded


def bootstrap_means(values: np.ndarray, times: np.ndarray, *, spacing_ms: int, block_rows: int,
                     resamples: int, seed: int) -> np.ndarray:
    x = np.asarray(values, dtype=float)
    times = np.asarray(times, dtype=np.int64)
    if (x.ndim != 2 or not len(x) or len(x) != len(times) or not np.isfinite(x).all()
            or np.any(np.diff(times) <= 0) or resamples < 2000 or block_rows < 1 or spacing_ms <= 0):
        raise ValueError('Invalid paired historical bootstrap inputs')
    if np.any(np.diff(times) != spacing_ms):
        raise ValueError('Bootstrap intervals are withheld across internal calendar gaps')
    rng = np.random.default_rng(seed)
    index = rng.integers(0, len(x), size=resamples)
    totals = x[index].copy()
    for _ in range(1, len(x)):
        following = (index + 1) % len(x)
        restart = rng.random(resamples) < 1 / block_rows
        index = np.where(restart, rng.integers(0, len(x), size=resamples), following)
        totals += x[index]
    return totals / len(x)


def _metric_summary(values: np.ndarray, times: np.ndarray, names: list[str], config: dict,
                    *, spacing_days: int = 1, block_days: int | None = None) -> dict:
    if not len(values):
        return {'n': 0, 'status': 'insufficient data', 'metrics': {}, 'calendar_gaps': 0}
    block_days = config['expected_block_days'] if block_days is None else block_days
    block_rows = max(1, math.ceil(block_days / spacing_days))
    lags = min(len(values) - 1, math.ceil(config['hac_lags_days'] / spacing_days))
    gaps = int(np.sum(np.diff(times) != spacing_days * DAY_MS))
    samples = None if gaps else bootstrap_means(values, times, spacing_ms=spacing_days * DAY_MS, block_rows=block_rows,
        resamples=config['bootstrap_resamples'], seed=config['bootstrap_seed'])
    metrics = {}
    for column, name in enumerate(names):
        x = values[:, column]
        interval = None if samples is None else [float(v) for v in np.quantile(samples[:, column], [.025, .975])]
        ess = effective_sample_size(x, lags)
        long_run = float(newey_west(x, lags)[0, 0])
        metrics[name] = {'mean': float(np.mean(x)), 'median': float(np.median(x)),
            'confidence_interval_95': interval, 'effective_sample_size': ess,
            'ess_gate_passed': ess >= config['minimum_effective_sample_size'] and gaps == 0 and len(values) > lags,
            'hac_standard_error': None if gaps else math.sqrt(max(0., long_run) / len(x)),
            'interval_role': 'withheld_calendar_gaps' if gaps else 'descriptive_stationary_bootstrap'}
    return {'n': len(values), 'status': 'descriptive', 'metrics': metrics, 'calendar_gaps': gaps,
            'expected_block_days': block_days, 'expected_block_rows': block_rows,
            'hac_lags_rows': lags, 'resamples': config['bootstrap_resamples'], 'seed': config['bootstrap_seed'],
            'spacing_days': spacing_days, 'minimum_effective_sample_size': config['minimum_effective_sample_size']}


def summarise_rows(rows: list[VarianceObservation], config: dict, *, spacing_days: int = 1,
                    block_days: int | None = None) -> dict:
    names = ['dvol_volatility_pct', 'realised_volatility_pct', 'volatility_gap_points', 'variance_gap',
             'qlike_dvol', 'qlike_trailing', 'qlike_dvol_minus_trailing', 'dvol_above_realised']
    values = np.asarray([[math.sqrt(row.forecast.dvol_variance) * 100, math.sqrt(row.realised_variance) * 100,
        100 * (math.sqrt(row.forecast.dvol_variance) - math.sqrt(row.realised_variance)),
        row.forecast.dvol_variance - row.realised_variance,
        qlike(row.realised_variance, row.forecast.dvol_variance), qlike(row.realised_variance, row.forecast.trailing_variance),
        qlike(row.realised_variance, row.forecast.dvol_variance) - qlike(row.realised_variance, row.forecast.trailing_variance),
        float(row.forecast.dvol_variance > row.realised_variance)] for row in rows], dtype=float)
    return _metric_summary(values, np.asarray([row.forecast.origin_ms for row in rows]), names, config,
                            spacing_days=spacing_days, block_days=block_days)


def summarise_correction(rows: list[VarianceObservation], config: dict) -> dict:
    paired = [row for row in rows if row.forecast.origin_ms >= config['recalibration_start_ms'] and row.correction is not None]
    names = ['qlike_corrected', 'qlike_dvol', 'qlike_trailing', 'corrected_minus_dvol', 'corrected_minus_trailing', 'factor']
    values = []
    for row in paired:
        assert row.correction is not None
        corrected = qlike(row.realised_variance, row.forecast.dvol_variance * row.correction['factor'])
        raw = qlike(row.realised_variance, row.forecast.dvol_variance)
        trailing = qlike(row.realised_variance, row.forecast.trailing_variance)
        values.append([corrected, raw, trailing, corrected - raw, corrected - trailing, row.correction['factor']])
    summary = _metric_summary(np.asarray(values, dtype=float), np.asarray([row.forecast.origin_ms for row in paired]), names, config)
    summary['role'] = 'descriptive_secondary_same_origin_comparison'
    summary['dependence_limit'] = 'The rolling 365-completed-origin scale can create longer persistence than the 60-day primary block.'
    summary['n_excluded_without_correction'] = sum(row.forecast.origin_ms >= config['recalibration_start_ms'] and row.correction is None for row in rows)
    return summary


def analyse_variance(prices: dict, dvol: dict, config: dict) -> dict:
    rows, excluded = build_observations(prices, dvol, config)
    primary = summarise_rows(rows, config)
    metric = primary['metrics'].get('qlike_dvol_minus_trailing')
    primary['status'] = 'ok' if metric and metric['ess_gate_passed'] else 'insufficient data'
    primary['conclusion'] = 'insufficient data'
    if primary['status'] == 'ok':
        interval = metric['confidence_interval_95']
        primary['conclusion'] = ('DVOL had lower mean QLIKE than trailing realised variance' if interval[1] < 0 else
                                 'DVOL had higher mean QLIKE than trailing realised variance' if interval[0] > 0 else
                                 'The primary interval includes zero')
    years = sorted({datetime.fromtimestamp(row.forecast.origin_ms / 1000, timezone.utc).year for row in rows})
    annual = {str(year): summarise_rows([row for row in rows if datetime.fromtimestamp(
        row.forecast.origin_ms / 1000, timezone.utc).year == year], config) for year in years}
    sensitivities = {}
    for name in config['alignment_sensitivities']:
        alternate, missing = build_observations(prices, dvol, config, dvol_lag_days=int(name == 'dvol_lag_one_day'),
            future_start_shift_hours=int(name == 'future_realised_window_plus_one_hour'))
        common = set(row.forecast.origin_ms for row in rows) & set(row.forecast.origin_ms for row in alternate)
        sensitivities[name] = {'primary_on_same_origins': summarise_rows([row for row in rows if row.forecast.origin_ms in common], config),
            'alternate': summarise_rows([row for row in alternate if row.forecast.origin_ms in common], config),
            'excluded_origins': missing, 'n_paired_origins': len(common)}
    nonoverlap = [row for row in rows if (row.forecast.origin_ms - config['nonoverlap_anchor_ms']) % (config['nonoverlap_spacing_days'] * DAY_MS) == 0]
    return {'kind': 'retrospective_dvol_variance_results', 'synthetic': False,
        'n_requested_origins': (config['origin_end_ms'] - config['origin_start_ms']) // DAY_MS + 1,
        'origin_range_rule': 'daily 00:00 UTC, start and end both included', 'primary': primary,
        'annual': annual, 'correction': summarise_correction(rows, config),
        'block_sensitivities': {str(days): summarise_rows(rows, config, block_days=days) for days in config['sensitivity_block_days']},
        'nonoverlap': summarise_rows(nonoverlap, config, spacing_days=config['nonoverlap_spacing_days']),
        'nonoverlap_label': 'Non-overlapping outcome windows; not assumed independent',
        'alignment_sensitivities': sensitivities, 'excluded_origins': excluded,
        'exclusion_reason_counts': dict(sorted(Counter(reason for row in excluded for reason in row['reasons']).items())),
        'observations': [asdict(row) for row in rows],
        'vintage': config['vintage'], 'claim_scope': config['claim_scope']}
