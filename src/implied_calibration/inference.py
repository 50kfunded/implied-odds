"""I compare paired scores with dependence-aware uncertainty and an ESS gate."""

import math

import numpy as np
from scipy.special import ndtri
from scipy.stats import chi2, norm


def finite_series(values) -> np.ndarray:
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or not np.isfinite(x).all():
        raise ValueError('I need a finite one-dimensional paired series')
    return x


def effective_sample_size(values, lags: int = 13) -> float:
    x = finite_series(values)
    n = len(x)
    if n < 3:
        return 0.
    y = x - x.mean()
    variance = float(y @ y)
    if variance <= np.finfo(float).tiny:
        return 0.
    # I stop at the first nonpositive autocorrelation pair and keep pairs decreasing.
    correlations = np.correlate(y, y, mode='full')[n - 1:] / variance
    pair_sum, previous = 0., math.inf
    for lag in range(1, n - 1, 2):
        pair = float(correlations[lag] + correlations[lag + 1])
        if pair <= 0:
            break
        pair = min(pair, previous)
        pair_sum += pair
        previous = pair
    initial_positive = n / (1 + 2 * pair_sum)
    long_run = float(newey_west(x, min(lags, n - 1))[0, 0])
    hac_ess = n * (variance / n) / long_run if long_run > 0 else float(n)
    return max(0., min(float(n), initial_positive, hac_ess))


def overlap_count(expiry_ms, lead_ms: int) -> int:
    times = np.asarray(expiry_ms)
    if (times.ndim != 1 or not np.issubdtype(times.dtype, np.integer) or np.any(times < 0)
            or type(lead_ms) is not int or lead_ms <= 0 or np.any(np.diff(times) <= 0)):
        raise ValueError('I need unique increasing integer expiries and a positive lead')
    return max((i - int(np.searchsorted(times, time - lead_ms, side='right')) + 1
                for i, time in enumerate(times)), default=1)


def newey_west(values, lags: int) -> np.ndarray:
    x = np.asarray(values, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or not len(x) or not np.isfinite(x).all() or type(lags) is not int or not 0 <= lags < len(x):
        raise ValueError('Invalid HAC series or lag count')
    y = x - x.mean(axis=0)
    covariance = y.T @ y / len(y)
    for lag in range(1, lags + 1):
        cross = y[lag:].T @ y[:-lag] / len(y)
        covariance += (1 - lag / (lags + 1)) * (cross + cross.T)
    return covariance


def dm_test(differences, lags: int) -> dict:
    x = finite_series(differences)
    if not len(x):
        return {'status': 'insufficient data', 'statistic': None, 'p_value': None}
    variance = float(newey_west(x, lags)[0, 0])
    if not math.isfinite(variance) or variance <= np.finfo(float).eps * max(np.finfo(float).tiny, float(np.mean(x ** 2))):
        return {'status': 'insufficient data', 'reason': 'degenerate_hac_variance', 'statistic': None, 'p_value': None}
    statistic = float(x.mean()) / math.sqrt(variance / len(x))
    return {'status': 'ok', 'statistic': statistic, 'p_value': float(2 * norm.sf(abs(statistic))),
            'long_run_variance': variance, 'lags': lags, 'reference': 'asymptotic_normal'}


def stationary_bootstrap(values, *, resamples: int = 2000, block_length: int = 14, seed: int = 1729) -> dict:
    x = finite_series(values)
    if (not len(x) or type(resamples) is not int or resamples < 2000
            or type(block_length) is not int or block_length < 1 or type(seed) is not int or seed < 0):
        raise ValueError('I need at least 2000 seeded stationary bootstrap resamples')
    rng = np.random.default_rng(seed)
    index = rng.integers(0, len(x), size=resamples)
    sums = x[index].copy()
    for _ in range(1, len(x)):
        restart = rng.random(resamples) < 1 / block_length
        index = np.where(restart, rng.integers(0, len(x), size=resamples), (index + 1) % len(x))
        sums += x[index]
    means = sums / len(x)
    lower, upper = np.quantile(means, [.025, .975])
    p = (1 + int(np.sum(np.abs(means - x.mean()) >= abs(x.mean())))) / (resamples + 1)
    return {'confidence_interval_95': [float(lower), float(upper)], 'p_value': p, 'resamples': resamples,
            'expected_block_length': block_length, 'seed': seed, 'method': 'Politis_Romano_stationary_percentile'}


def berkowitz_hac(pits, *, lags: int = 13, minimum_ess: float = 60, epsilon: float = 1e-6) -> dict:
    u = finite_series(pits)
    if (not math.isfinite(minimum_ess) or minimum_ess <= 0 or type(lags) is not int or lags < 0):
        raise ValueError('I need a positive finite ESS threshold and nonnegative integer lags')
    if np.any((u < 0) | (u > 1)) or not 0 < epsilon < .01:
        raise ValueError('PITs must lie in 0..1; transform clipping must be declared')
    z = ndtri(np.clip(u, epsilon, 1 - epsilon))
    moments = np.column_stack((z, z ** 2 - 1))
    ess = min((effective_sample_size(moments[:, j], lags) for j in range(2)), default=0.)
    output = {'status': 'insufficient data', 'method': 'Berkowitz_type_HAC_normal_moments', 'n': len(u),
              'effective_sample_size': ess, 'minimum_ess': minimum_ess, 'statistic': None, 'p_value': None,
              'pit_clipped_count': int(np.sum((u < epsilon) | (u > 1 - epsilon))), 'epsilon': epsilon,
              'serial_correlation': 'allowed_under_the_null'}
    if ess < minimum_ess or len(u) <= lags:
        return output
    covariance = newey_west(moments, lags)
    if np.linalg.eigvalsh(covariance)[0] <= np.finfo(float).eps:
        output['reason'] = 'degenerate_hac_covariance'
        return output
    mu = moments.mean(axis=0)
    statistic = float(len(u) * mu @ np.linalg.solve(covariance, mu))
    output.update(status='ok', statistic=statistic, p_value=float(chi2.sf(statistic, 2)),
                  reference='asymptotic_chi_square_2_for_HAC_moments', lags=lags)
    return output


def paired_inference(expiry_ms, svi_scores, baseline_scores, lead_ms: int, *, minimum_ess: float = 60,
                     block_length: int = 14, resamples: int = 2000, seed: int = 1729,
                     primary: bool = False) -> dict:
    if (not math.isfinite(minimum_ess) or minimum_ess <= 0
            or type(block_length) is not int or block_length < 1
            or type(resamples) is not int or resamples < 2000 or type(seed) is not int or seed < 0):
        raise ValueError('I need valid finite inference settings before applying the ESS gate')
    svi, baseline = finite_series(svi_scores), finite_series(baseline_scores)
    if svi.shape != baseline.shape or len(expiry_ms) != len(svi):
        raise ValueError('Paired scores and expiry timestamps must have the same length')
    timestamps = np.asarray(expiry_ms)
    if timestamps.size == 0:
        timestamps = np.asarray([], dtype=np.int64)
    overlap = overlap_count(timestamps, lead_ms)
    block = max(block_length, overlap)
    lags = max(block_length - 1, overlap - 1)
    differences = svi - baseline
    ess = effective_sample_size(differences, lags)
    output = {'status': 'insufficient data', 'n_pairs': len(svi), 'effective_sample_size': ess,
              'minimum_ess': minimum_ess, 'mean_difference_svi_minus_baseline':
              float(differences.mean()) if len(differences) else None,
              'overlap_count': overlap, 'expected_block_length': block, 'hac_lags': lags,
              'comparison_role': 'single_declared_primary' if primary else 'descriptive_secondary',
              'bootstrap': None, 'dm_cross_check': None, 'conclusion': 'insufficient data'}
    if ess < minimum_ess or len(differences) <= lags:
        return output
    bootstrap = stationary_bootstrap(differences, resamples=resamples, block_length=block, seed=seed)
    dm = dm_test(differences, lags)
    if dm['status'] != 'ok':
        output['reason'] = dm.get('reason')
        return output
    if not primary:
        bootstrap['p_value'] = None
    # I retain DM only as a descriptive cross-check, never a second headline test.
    dm['p_value'] = None
    output.update(status='ok', bootstrap=bootstrap, dm_cross_check=dm,
                  conclusion='descriptive only' if not primary else
                  'lower mean CRPS for SVI' if bootstrap['confidence_interval_95'][1] < 0 else
                  'higher mean CRPS for SVI' if bootstrap['confidence_interval_95'][0] > 0 else
                  'primary interval includes zero')
    return output
