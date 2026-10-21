"""Raw SVI smiles and variance-curve checks, with no I/O."""

from dataclasses import asdict, dataclass, replace
import math
from typing import Sequence

import numpy as np
from scipy.optimize import least_squares


@dataclass(frozen=True)
class SVIParams:
    a: float
    b: float
    rho: float
    m: float
    s: float


@dataclass(frozen=True)
class SVISettings:
    n_starts: int = 10
    seed: int = 1729
    spread_floor_iv: float = 0.005
    maximum_weight_share: float = 0.2
    minimum_variance: float = 1e-10
    maximum_minimum_variance: float = 10.0
    minimum_b_fraction: float = 1e-8
    rho_limit: float = 0.999
    minimum_s: float = 1e-4
    maximum_s: float = 5.0
    maximum_abs_m: float = 5.0
    lee_margin: float = 1e-6
    max_nfev: int = 2000
    optimiser_tolerance: float = 1e-12
    butterfly_tolerance: float = 1e-8
    calendar_tolerance: float = 1e-8
    grid_standard_deviations: float = 6.0
    grid_points: int = 1201
    local_grid_points: int = 401
    local_grid_scales: float = 8.0
    minimum_strikes: int = 6
    minimum_strikes_each_side: int = 2


@dataclass(frozen=True)
class FitResult:
    status: str
    params: SVIParams | None = None
    rmse_vol_pts: float | None = None
    frac_strikes_within_bid_ask: float | None = None
    n_strikes: int = 0
    min_g: float | None = None
    calendar_flag: bool | None = None
    message: str = ''
    weighted_objective: float | None = None
    starts_attempted: int = 0
    starts_converged: int = 0
    max_weight_fraction: float | None = None
    check_k_min: float | None = None
    check_k_max: float | None = None
    check_grid_points: int = 0
    calendar_pairs_checked: int = 0
    min_calendar_spread: float | None = None


@dataclass(frozen=True)
class SmileSlice:
    snapshot_id: str
    expiry_ms: int
    t_years: float
    observed_k: tuple[float, ...]
    result: FitResult


def total_variance(k: Sequence[float] | float, params: SVIParams) -> np.ndarray:
    x = np.asarray(k, dtype=float) - params.m
    return params.a + params.b * (params.rho * x + np.hypot(x, params.s))


def variance_derivatives(k: Sequence[float] | float, params: SVIParams) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(k, dtype=float) - params.m
    radius = np.hypot(x, params.s)
    return params.b * (params.rho + x / radius), params.b * params.s ** 2 / radius ** 3


def butterfly_g(k: Sequence[float] | float, params: SVIParams) -> np.ndarray:
    k_array = np.asarray(k, dtype=float)
    w = total_variance(k_array, params)
    first, second = variance_derivatives(k_array, params)
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        g = (1 - k_array * first / (2 * w)) ** 2 - first ** 2 / 4 * (1 / w + 0.25) + second / 2
    return np.where(w > 0, g, np.nan)


def parameter_violations(params: SVIParams, settings: SVISettings = SVISettings()) -> tuple[str, ...]:
    try:
        if not all(math.isfinite(float(v)) for v in (params.a, params.b, params.rho, params.m, params.s)):
            return ('nonfinite_parameters',)
    except (ValueError, TypeError, OverflowError):
        return ('invalid_parameters',)
    reasons = []
    if params.b <= 0:
        reasons.append('nonpositive_b')
    if abs(params.rho) >= settings.rho_limit:
        reasons.append('rho_bound')
    if params.s <= settings.minimum_s:
        reasons.append('s_bound')
    if abs(params.rho) < 1 and params.a + params.b * params.s * math.sqrt(1 - params.rho ** 2) < 0:
        reasons.append('negative_minimum_variance')
    if max(params.b * (1 - params.rho), params.b * (1 + params.rho)) >= 2:
        reasons.append('lee_wing_bound')
    return tuple(reasons)


def check_grid(params: SVIParams, observed_k: Sequence[float], settings: SVISettings) -> np.ndarray:
    observed = np.asarray(observed_k, dtype=float)
    atm = float(total_variance(0.0, params))
    minimum_k = params.m - params.rho * params.s / math.sqrt(1 - params.rho ** 2)
    radius = max(settings.grid_standard_deviations * math.sqrt(max(atm, settings.minimum_variance)),
                 float(np.max(np.abs(observed), initial=0.0)), abs(params.m) + settings.local_grid_scales * params.s)
    local = np.linspace(-settings.local_grid_scales, settings.local_grid_scales, settings.local_grid_points)
    return np.unique(np.concatenate((np.linspace(-radius, radius, settings.grid_points), observed,
                                     [0.0, minimum_k], params.m + params.s * local,
                                     minimum_k + params.s * local)))


def spread_weights(iv_bid: np.ndarray, iv_ask: np.ndarray, settings: SVISettings) -> np.ndarray:
    weights = 1 / np.maximum(iv_ask - iv_bid, settings.spread_floor_iv) ** 2
    low, high = float(weights.min()), float(weights.max())
    for _ in range(80):
        cap = (low + high) / 2
        capped = np.minimum(weights, cap)
        if capped.max() / capped.sum() > settings.maximum_weight_share:
            high = cap
        else:
            low = cap
    weights = np.minimum(weights, low)
    return weights / weights.mean()


def _decode(vector: np.ndarray, settings: SVISettings) -> SVIParams:
    minimum, fraction, rho, m, s = map(float, vector)
    b = fraction * (2 - settings.lee_margin) / (1 + abs(rho))
    a = minimum - b * s * math.sqrt(1 - rho ** 2)
    return SVIParams(a, b, rho, m, s)


def _settings_error(settings: SVISettings) -> str | None:
    if not isinstance(settings, SVISettings):
        return 'Expected SVISettings'
    integer_fields = {'n_starts', 'seed', 'max_nfev', 'grid_points', 'local_grid_points',
                      'minimum_strikes', 'minimum_strikes_each_side'}
    for field, value in asdict(settings).items():
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if (not finite or value < 0 or value == 0 and field != 'seed'
                or field in integer_fields and type(value) is not int):
            return f'Invalid finite numeric SVI setting: {field}'
    if (not 1 / settings.minimum_strikes <= settings.maximum_weight_share <= 1
            or not 0 < settings.rho_limit <= 0.999 or not 0 < settings.lee_margin < 2
            or not 0 < settings.minimum_b_fraction < 1
            or settings.minimum_s < 1e-4 or settings.maximum_s <= settings.minimum_s
            or settings.maximum_minimum_variance <= settings.minimum_variance
            or settings.optimiser_tolerance <= np.finfo(float).eps
            or min(settings.grid_points, settings.local_grid_points) < 3
            or settings.minimum_strikes < 2 * settings.minimum_strikes_each_side):
        return 'Invalid coupled SVI settings'
    return None


def fit_svi(k: Sequence[float], iv_bid: Sequence[float], iv_ask: Sequence[float],
            iv_mid: Sequence[float], t_years: float, settings: SVISettings = SVISettings()) -> FitResult:
    """Select the lowest weighted error among converged starts, then test it."""
    n_strikes = 0
    try:
        arrays = [np.asarray(values, dtype=float) for values in (k, iv_bid, iv_ask, iv_mid)]
        x, bid, ask, mid = arrays
        if any(a.ndim != 1 for a in arrays) or len({a.size for a in arrays}) != 1:
            return FitResult('invalid_input', message='Expected equal one-dimensional quote arrays')
        n_strikes = x.size
        settings_error = _settings_error(settings)
        if settings_error:
            return FitResult('invalid_input', n_strikes=n_strikes, message=settings_error)
        if (not all(np.all(np.isfinite(a)) for a in arrays) or not math.isfinite(t_years)
                or t_years <= 0 or np.any(bid <= 0) or np.any(bid > mid) or np.any(mid > ask)):
            return FitResult('invalid_input', n_strikes=n_strikes, message='Expected finite ordered positive IV quotes and T')
        if (n_strikes < settings.minimum_strikes or np.unique(x).size != n_strikes
                or np.sum(x < 0) < settings.minimum_strikes_each_side
                or np.sum(x > 0) < settings.minimum_strikes_each_side):
            return FitResult('insufficient_strikes', n_strikes=n_strikes,
                             message='Expected distinct strikes and enough strikes strictly on both sides')
        order = np.argsort(x)
        x, bid, ask, mid = [a[order] for a in arrays]
        weights = spread_weights(bid, ask, settings)
        lower = np.array([settings.minimum_variance, settings.minimum_b_fraction,
                          -np.nextafter(settings.rho_limit, 0), -settings.maximum_abs_m,
                          np.nextafter(settings.minimum_s, math.inf)])
        upper = np.array([settings.maximum_minimum_variance, 1.0,
                          np.nextafter(settings.rho_limit, 0), settings.maximum_abs_m, settings.maximum_s])
        rng = np.random.default_rng(settings.seed)
        candidates = []
        failures = []

        def residual(vector: np.ndarray) -> np.ndarray:
            return (np.sqrt(total_variance(x, _decode(vector, settings)) / t_years) - mid) * np.sqrt(weights)

        for start in range(settings.n_starts):
            rho = -0.2 if start == 0 else float(rng.uniform(-0.8, 0.8))
            s = min(0.2, settings.maximum_s / 2) if start == 0 else float(np.exp(
                rng.uniform(math.log(lower[4]), math.log(upper[4]))))
            b = float(np.clip(np.ptp(mid ** 2 * t_years) / max(float(np.ptp(x)), 0.1), 0.005, 0.8))
            minimum = max(float(np.min(mid ** 2 * t_years)) * (0.8 if start == 0 else rng.uniform(0.3, 1.0)),
                          settings.minimum_variance * 10)
            m = 0.0 if start == 0 else float(rng.uniform(float(x.min()), float(x.max())))
            initial = np.array([minimum, b * (1 + abs(rho)) / (2 - settings.lee_margin), rho, m, s])
            initial = np.clip(initial, lower + (upper - lower) * 1e-8, upper - (upper - lower) * 1e-8)
            try:
                solved = least_squares(residual, initial, method='trf', bounds=(lower, upper),
                                       max_nfev=settings.max_nfev, x_scale='jac',
                                       ftol=settings.optimiser_tolerance, xtol=settings.optimiser_tolerance,
                                       gtol=settings.optimiser_tolerance)
                if solved.success and np.all(np.isfinite(solved.x)) and math.isfinite(solved.cost):
                    params = _decode(solved.x, settings)
                    if not parameter_violations(params, settings):
                        candidates.append((float(solved.cost), start, params))
                    else:
                        failures.append(f'{start}: parameter constraints failed')
                else:
                    failures.append(f'{start}: {solved.message}')
            except (ValueError, RuntimeError, FloatingPointError, OverflowError, np.linalg.LinAlgError) as exc:
                failures.append(f'{start}: {type(exc).__name__}: {exc}')
        if not candidates:
            return FitResult('optimiser_failed', n_strikes=n_strikes, starts_attempted=settings.n_starts,
                             message='; '.join(failures))
        objective, _, params = min(candidates, key=lambda value: (value[0], value[1]))
        grid = check_grid(params, x, settings)
        g = butterfly_g(grid, params)
        fitted_iv = np.sqrt(total_variance(x, params) / t_years)
        min_g = float(g.min()) if np.all(np.isfinite(g)) else None
        status = 'ok' if min_g is not None and min_g >= -settings.butterfly_tolerance else 'butterfly_violation'
        return FitResult(status, params, float(100 * np.sqrt(np.mean((fitted_iv - mid) ** 2))),
                         float(np.mean((fitted_iv >= bid) & (fitted_iv <= ask))), n_strikes, min_g,
                         message=f'{len(candidates)}/{settings.n_starts} starts converged; lowest weighted error; '
                                 + ('; '.join(failures) if failures else 'no failed starts'),
                         weighted_objective=objective, starts_attempted=settings.n_starts,
                         starts_converged=len(candidates), max_weight_fraction=float(weights.max() / weights.sum()),
                         check_k_min=float(grid.min()), check_k_max=float(grid.max()), check_grid_points=grid.size)
    except (TypeError, ValueError, OverflowError, FloatingPointError, np.linalg.LinAlgError) as exc:
        return FitResult('invalid_input', n_strikes=n_strikes, message=f'{type(exc).__name__}: {exc}')


def check_calendars(slices: Sequence[SmileSlice], settings: SVISettings = SVISettings()) -> list[SmileSlice]:
    """Compare each expiry pair on a shared snapshot grid and leave parameters alone."""
    output = list(slices)
    settings_error = _settings_error(settings)
    if settings_error:
        return [replace(entry, result=replace(entry.result, status='invalid_input', calendar_flag=None,
                                             message=settings_error)) for entry in slices]
    snapshots = sorted({entry.snapshot_id for entry in slices})
    for snapshot in snapshots:
        indices = [i for i, entry in enumerate(slices) if entry.snapshot_id == snapshot
                   and entry.result.params is not None and entry.result.status == 'ok']
        indices.sort(key=lambda i: (slices[i].expiry_ms, i))
        if not indices:
            continue
        grid = np.unique(np.concatenate([check_grid(slices[i].result.params, slices[i].observed_k, settings)
                                         for i in indices]))
        flags = {i: False for i in indices}
        counts = {i: 0 for i in indices}
        minima = {i: math.inf for i in indices}
        for position, earlier in enumerate(indices):
            for later in indices[position + 1:]:
                difference = total_variance(grid, slices[later].result.params) - total_variance(grid, slices[earlier].result.params)
                minimum = float(difference.min()) if np.all(np.isfinite(difference)) else -math.inf
                for i in (earlier, later):
                    counts[i] += 1
                    minima[i] = min(minima[i], minimum)
                    flags[i] |= minimum < -settings.calendar_tolerance
        for i in indices:
            output[i] = replace(slices[i], result=replace(slices[i].result, calendar_flag=flags[i],
                                calendar_pairs_checked=counts[i],
                                min_calendar_spread=minima[i] if math.isfinite(minima[i]) else None))
    return output
