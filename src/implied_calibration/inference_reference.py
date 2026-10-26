"""Exploratory inference references leave the locked v1 decisions unchanged."""

import math

import numpy as np
from scipy.stats import norm, t

from .inference import effective_sample_size, finite_series, newey_west
from .inference_study import bootstrap_interval, loss_process, wilson
from .variance_study import DAY_MS


def contiguous(values, timestamps=None) -> np.ndarray:
    x = finite_series(values)
    if timestamps is not None:
        times = np.asarray(timestamps)
        if times.ndim != 1 or len(times) != len(x) or np.any(np.diff(times) != DAY_MS):
            raise ValueError('Inference blocks require consecutive daily origins')
    return x


def dm_hln(values, *, lags: int, horizon: int = 30, timestamps=None) -> dict:
    x = contiguous(values, timestamps)
    n = len(x)
    if type(horizon) is not int or horizon < 1 or type(lags) is not int or not 0 <= lags < n:
        raise ValueError('Invalid DM-HLN horizon or HAC lags')
    if n <= horizon:
        return {'status': 'insufficient data', 'p_value': None, 'statistic': None}
    variance = float(newey_west(x, lags)[0, 0])
    if variance <= np.finfo(float).eps * max(float(np.mean(x*x)), np.finfo(float).tiny):
        return {'status': 'degenerate variance', 'p_value': None, 'statistic': None}
    correction = math.sqrt((n + 1 - 2*horizon + horizon*(horizon-1)/n)/n)
    se = math.sqrt(variance/n)
    statistic = float(x.mean()/se*correction)
    return {'status': 'descriptive', 'n': n, 'mean': float(x.mean()), 'hac_standard_error': se,
            'statistic': statistic, 'p_value': float(2*t.sf(abs(statistic), n-1)),
            'horizon': horizon, 'lags': lags, 'hln_factor': correction,
            'reference': 'Student_t_n_minus_1',
            'limit': 'HLN is a reference correction; persistent losses, nested models and generated forecasts can invalidate its nominal size.'}


def subsampling(values, *, timestamps=None, exponent: float = .7, minimum_block: int = 60) -> dict:
    x = contiguous(values, timestamps)
    n = len(x)
    if not 0 < exponent < 1 or type(minimum_block) is not int or minimum_block < 2:
        raise ValueError('Invalid subsampling block rule')
    block = max(minimum_block, math.floor(n**exponent))
    if block >= n:
        return {'status': 'insufficient data', 'confidence_interval_95': None, 'p_value': None, 'block': block}
    prefix = np.r_[0., np.cumsum(x)]
    mean = float(x.mean())
    roots = math.sqrt(block)*((prefix[block:]-prefix[:-block])/block-mean)
    lower, upper = np.quantile(roots, [.025, .975])
    ci = [float(mean-upper/math.sqrt(n)), float(mean-lower/math.sqrt(n))]
    observed = math.sqrt(n)*mean
    below = (1+np.count_nonzero(roots <= observed))/(len(roots)+1)
    above = (1+np.count_nonzero(roots >= observed))/(len(roots)+1)
    return {'status': 'descriptive', 'n': n, 'mean': mean, 'block': block, 'n_blocks': len(roots),
            'confidence_interval_95': ci, 'p_value': float(min(1., 2*min(below, above))),
            'method': 'overlapping_contiguous_blocks_root_n_mean',
            'limit': 'Subsampling is a candidate diagnostic. Its asymptotic assumptions and finite-sample calibration are not guaranteed by choosing a block rule.'}


def reference_table(records: list[dict], pairs: list[tuple[str, str]], spec: dict) -> list[dict]:
    times = [r['origin_ms'] for r in records]
    output: list[dict] = []
    for first, second in pairs:
        x = np.array([r['losses'][first]-r['losses'][second] for r in records])
        output.append({'contrast': first+'_minus_'+second, 'n': len(x), 'mean': float(x.mean()),
                       'ess_lag59': effective_sample_size(x, min(59, len(x)-1)),
                       'dm_hln': {str(lag): dm_hln(x, lags=lag, timestamps=times)
                                  for lag in spec['dm_hln']['lags']},
                       'subsampling': subsampling(x, timestamps=times)})
    return output


def calibration_report(spec: dict) -> dict:
    config = spec['calibration']
    output: list[dict] = []
    for n in config['sample_sizes']:
        for name in config['dgp']:
            rng = np.random.default_rng(config['seed']+n+len(output)*10000)
            counts = dict.fromkeys(config['tested_methods'], 0)
            for _ in range(config['replications']):
                x = loss_process(n, name, rng)
                ci = bootstrap_interval(x, resamples=config['bootstrap_resamples'],
                                        block=config['bootstrap_block_days'], rng=rng)
                counts['stationary_percentile'] += ci[0] > 0 or ci[1] < 0
                for lag in (29, 59):
                    p = dm_hln(x, lags=lag)['p_value']
                    counts['dm_hln_lag'+str(lag)] += p is not None and p < config['nominal_size']
                sub = subsampling(x)['confidence_interval_95']
                counts['subsampling'] += sub[0] > 0 or sub[1] < 0
            output.append({'n': n, 'dgp': name, 'replications': config['replications'],
                           'methods': {method: {'rejection_rate': count/config['replications'],
                                      'monte_carlo_interval_95': wilson(count, config['replications'])}
                                       for method, count in counts.items()}})
    return {'role': 'synthetic_calibration_not_market_size', 'rows': output, 'settings': config}


def stationary_samples(values, n: int, replications: int, block: int, rng) -> np.ndarray:
    x = finite_series(values)
    if not len(x) or min(n, replications, block) < 1:
        raise ValueError('Expected a nonempty source and positive resampling settings')
    indices = rng.integers(0, len(x), replications)
    samples = np.empty((replications, n))
    samples[:, 0] = x[indices]
    for j in range(1, n):
        indices = np.where(rng.random(replications) < 1/block,
                           rng.integers(0, len(x), replications), (indices+1) % len(x))
        samples[:, j] = x[indices]
    return samples


def empirical_power(values, spec: dict) -> dict:
    x = finite_series(values)
    config = spec['power']
    centred = x-x.mean()
    rows = []
    for block in config['block_days']:
        for n in config['sample_sizes']:
            rng = np.random.default_rng(config['seed']+block*10000+n)
            draws = stationary_samples(centred, n, config['replications'], block, rng)
            means = draws.mean(axis=1)
            deviations = draws-means[:, None]
            lrv = np.sum(deviations*deviations, axis=1)/n
            for lag in range(1, 60):
                lrv += 2*(1-lag/60)*np.sum(deviations[:, lag:]*deviations[:, :-lag], axis=1)/n
            factor = math.sqrt((n+1-60+30*29/n)/n)
            se = np.sqrt(np.maximum(lrv, np.finfo(float).tiny)/n)
            critical = t.ppf(1-config['nominal_size']/2, n-1)
            sub_intervals = np.array([subsampling(draw)['confidence_interval_95'] for draw in draws])
            for shift in config['effect_sizes']:
                valid = lrv > np.finfo(float).eps*np.maximum(np.mean((draws+shift)**2, axis=1), np.finfo(float).tiny)
                dm_count = int(np.count_nonzero(valid & (np.abs((means+shift)/se*factor) > critical)))
                sub_count = int(np.count_nonzero((sub_intervals[:, 0]+shift > 0) | (sub_intervals[:, 1]+shift < 0)))
                rows.append({'n': n, 'expected_block_days': block, 'population_shift': shift,
                             'replications': config['replications'], 'methods': {
                                 method: {'rejection_rate': count/config['replications'],
                                          'monte_carlo_interval_95': wilson(count, config['replications'])}
                                 for method, count in (('dm_hln_lag59', dm_count), ('subsampling', sub_count))}})
    lrv_observed = float(newey_west(x, 59)[0, 0])
    effect = abs(float(x.mean()))
    zsum = norm.ppf(.975)+norm.ppf(.8)
    return {'role': 'conditional_planning_from_inspected_empirical_losses', 'rows': rows, 'settings': config,
            'normal_plugin': {'assumed_long_run_variance': lrv_observed, 'n365_standard_error': math.sqrt(lrv_observed/365),
                              'n365_80pct_detectable_effect': float(zsum*math.sqrt(lrv_observed/365)),
                              'years_for_absolute_z2': 4*lrv_observed/effect**2/365 if effect else None,
                              'years_for_80pct_power': float(zsum*zsum*lrv_observed/effect**2/365) if effect else None},
            'limit': 'The inspected effect and dependence are uncertain. Neither this resampling nor the normal approximation predicts the years needed in a future market.'}
