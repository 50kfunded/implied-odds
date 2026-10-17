"""I extract risk-neutral CDFs from total-variance slices without doing I/O."""

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Sequence

import numpy as np
from scipy.special import log_ndtr, logsumexp

from .blackscholes import normalised_price
from .svi import SVIParams, total_variance, variance_derivatives


@dataclass(frozen=True)
class CDFSettings:
    initial_log_extent: float = 8.0
    maximum_log_extent: float = 64.0
    grid_points: int = 8193
    local_grid_points: int = 1001
    local_grid_scales: float = 8.0
    tail_probability_tolerance: float = 1e-10
    tail_mean_relative_tolerance: float = 1e-8
    forward_relative_tolerance: float = 2e-6

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    or name.endswith('points') and (type(value) is not int or value < 513)):
                raise ValueError(f'Invalid CDF setting: {name}')
        if not self.initial_log_extent <= self.maximum_log_extent <= 600:
            raise ValueError('I need initial_log_extent <= maximum_log_extent <= 600')
        if max(self.tail_probability_tolerance, self.tail_mean_relative_tolerance,
               self.forward_relative_tolerance) >= 1:
            raise ValueError('CDF tolerances must be below one')


@dataclass(frozen=True)
class PAVAResult:
    values: tuple[float, ...]
    maximum_correction: float
    maximum_isotonic_correction: float
    maximum_bounds_correction: float
    pooled_blocks: int
    changed_points: int


def bounded_pava(values: Sequence[float]) -> PAVAResult:
    """I project equally weighted nodes onto increasing probabilities and report every change."""
    raw = np.asarray(values, dtype=float)
    if raw.ndim != 1 or raw.size == 0 or not np.isfinite(raw).all():
        raise ValueError('PAVA needs a nonempty finite one-dimensional sequence')
    blocks: list[list[float | int]] = []
    for value in raw:
        blocks.append([float(value), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            right, left = blocks.pop(), blocks.pop()
            count = int(left[1]) + int(right[1])
            mean = float(left[0]) * (int(left[1]) / count) + float(right[0]) * (int(right[1]) / count)
            blocks.append([mean, count])
    isotonic = np.concatenate([np.full(int(count), mean) for mean, count in blocks])
    bounded = np.clip(isotonic, 0.0, 1.0)
    return PAVAResult(tuple(map(float, bounded)), float(np.max(np.abs(bounded - raw))),
                      float(np.max(np.abs(isotonic - raw))),
                      float(np.max(np.abs(bounded - isotonic))),
                      sum(int(count) > 1 for _, count in blocks), int(np.count_nonzero(bounded != raw)))


def _validate_params(params: SVIParams) -> None:
    if not all(type(v) in (float, int) and math.isfinite(v) for v in asdict(params).values()):
        raise ValueError('I need finite SVI parameters')
    if (params.b < 0 or params.s <= 0 or abs(params.rho) >= 1
            or params.a + params.b * params.s * math.sqrt(1 - params.rho ** 2) <= 0
            or max(params.b * (1 - params.rho), params.b * (1 + params.rho)) >= 2):
        raise ValueError('I need positive variance, b >= 0, s > 0, |rho| < 1 and Lee slopes < 2')


def _linear_survival_integral(k: np.ndarray, survival: np.ndarray) -> float:
    """I integrate the repaired linear-in-k survival exactly, including the price Jacobian."""
    h = np.diff(k)
    second = np.empty_like(h)
    small = h < 1e-3
    z = h[small]
    second[small] = z / 2 + z ** 2 / 3 + z ** 3 / 8 + z ** 4 / 30 + z ** 5 / 144
    second[~small] = np.exp(h[~small]) - np.expm1(h[~small]) / h[~small]
    segments = np.exp(k[:-1]) * (survival[:-1] * np.expm1(h) + np.diff(survival) * second)
    return math.fsum(map(float, segments))


def analytic_probabilities(k: Sequence[float] | float, params: SVIParams) -> tuple[np.ndarray, np.ndarray]:
    """I return the raw CDF and survival separately, including any invalid probabilities."""
    _validate_params(params)
    x = np.asarray(k, dtype=float)
    if not np.isfinite(x).all():
        raise ValueError('I need finite log-moneyness')
    w = total_variance(x, params)
    first, _ = variance_derivatives(x, params)
    if not np.isfinite(w).all() or np.any(w <= 0) or not np.isfinite(first).all():
        raise ValueError('Nonfinite or nonpositive variance on the CDF grid')
    root = np.sqrt(w)
    d2 = -x / root - root / 2
    with np.errstate(divide='ignore'):
        adjustment_log = -0.5 * d2 ** 2 - 0.5 * math.log(2 * math.pi) + np.log(np.abs(first)) - np.log(2 * root)
    cdf_log, cdf_sign = logsumexp(np.stack((log_ndtr(-d2), adjustment_log)), axis=0,
                                 b=np.stack((np.ones_like(x), np.sign(first))), return_sign=True)
    sf_log, sf_sign = logsumexp(np.stack((log_ndtr(d2), adjustment_log)), axis=0,
                               b=np.stack((np.ones_like(x), -np.sign(first))), return_sign=True)
    cdf, sf = cdf_sign * np.exp(cdf_log), sf_sign * np.exp(sf_log)
    # I keep the smaller tail directly and use its complement for the large probability.
    return np.where(cdf <= sf, cdf, 1 - sf), np.where(cdf <= sf, 1 - cdf, sf)


@dataclass(frozen=True)
class CDFValue:
    cdf: float
    survival: float
    extrapolated: bool
    region: str
    repaired: bool
    outside_numerical_grid: bool


@dataclass(frozen=True)
class ImpliedCDF:
    params: SVIParams
    forward_usd: float
    t_years: float
    observed_k_min: float
    observed_k_max: float
    settings: CDFSettings = CDFSettings()
    _k: np.ndarray = field(init=False, repr=False, compare=False)
    _cdf: np.ndarray = field(init=False, repr=False, compare=False)
    _survival: np.ndarray = field(init=False, repr=False, compare=False)
    _diagnostics: dict[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_params(self.params)
        for name in ('forward_usd', 't_years', 'observed_k_min', 'observed_k_max'):
            value = getattr(self, name)
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError(f'I need finite {name}')
        if self.forward_usd <= 0 or self.t_years <= 0 or self.observed_k_min >= self.observed_k_max:
            raise ValueError('I need positive F and T and an ordered observed strike range')
        if not isinstance(self.settings, CDFSettings):
            raise ValueError('I need CDFSettings')
        radius = max(self.settings.initial_log_extent, abs(self.observed_k_min), abs(self.observed_k_max))
        if radius > self.settings.maximum_log_extent:
            raise ValueError('Observed strikes exceed the numerical log-extent limit')
        while True:
            left, right = -radius, radius
            boundary_cdf, boundary_sf = analytic_probabilities([left, right], self.params)
            left_mean, right_mean = self._raw_tail_means(left, right)
            probability_resolved = (0 <= boundary_cdf[0] <= self.settings.tail_probability_tolerance
                                    and 0 <= boundary_sf[1] <= self.settings.tail_probability_tolerance)
            mean_resolved = (0 <= left_mean <= self.settings.tail_mean_relative_tolerance
                             and 0 <= right_mean <= self.settings.tail_mean_relative_tolerance)
            if probability_resolved and mean_resolved or radius >= self.settings.maximum_log_extent:
                break
            radius = min(2 * radius, self.settings.maximum_log_extent)
        minimum_k = self.params.m - self.params.rho * self.params.s / math.sqrt(1 - self.params.rho ** 2)
        local = self.params.s * np.linspace(-self.settings.local_grid_scales, self.settings.local_grid_scales,
                                            self.settings.local_grid_points)
        k = np.unique(np.concatenate((np.linspace(left, right, self.settings.grid_points),
                                      [0.0, self.observed_k_min, self.observed_k_max],
                                      self.params.m + local, minimum_k + local)))
        k = k[(k >= left) & (k <= right)]
        spacing = 64 * np.finfo(float).eps * max(1.0, radius)
        k = k[np.concatenate(([True], np.diff(k) > spacing))]
        k[0], k[-1] = left, right
        raw_cdf, raw_sf = analytic_probabilities(k, self.params)
        if not np.isfinite(raw_cdf).all() or not np.isfinite(raw_sf).all():
            raise ValueError('Nonfinite analytic probabilities')
        repair = bool(np.any(np.diff(raw_cdf) < 0) or np.any(np.diff(raw_sf) > 0)
                      or np.any(raw_cdf < 0) or np.any(raw_cdf > 1)
                      or np.any(raw_sf < 0) or np.any(raw_sf > 1))
        projected = bounded_pava(raw_cdf) if repair else None
        cdf, sf = raw_cdf.copy(), raw_sf.copy()
        if projected is not None:
            cdf = np.asarray(projected.values)
            cdf[0], cdf[-1] = 0.0, 1.0
            sf = 1 - cdf
        for name, array in (('_k', k), ('_cdf', cdf), ('_survival', sf)):
            array.setflags(write=False)
            object.__setattr__(self, name, array)
        # I integrate survival on the log-price axis with dS = F exp(k) dk.
        truncated = (_linear_survival_integral(k, sf) if repair else
                     float(np.trapezoid(sf * np.exp(k), k)))
        if repair:
            full_mean = truncated + math.exp(left)
            omitted_left, omitted_right = math.exp(left), 0.0
            call_identity_error = None
        else:
            full_mean = truncated + left_mean + right_mean
            omitted_left, omitted_right = left_mean, right_mean
            call_identity_error = abs(truncated - (1 - left_mean - right_mean))
        obs_cdf, obs_sf = self._evaluate([self.observed_k_min, self.observed_k_max], repair)
        max_correction = float(np.max(np.maximum(np.abs(cdf - raw_cdf), np.abs(sf - raw_sf)))) if repair else 0.0
        reasons = []
        if repair:
            reasons.append('cdf_repaired')
        if not probability_resolved:
            reasons.append('tail_probability_unresolved')
        if not mean_resolved:
            reasons.append('tail_mean_unresolved')
        if abs(full_mean - 1) > self.settings.forward_relative_tolerance:
            reasons.append('forward_mismatch')
        status = ('tail_not_resolved' if not probability_resolved or not mean_resolved else
                  'forward_mismatch' if 'forward_mismatch' in reasons else 'repaired' if repair else 'ok')
        object.__setattr__(self, '_diagnostics', {
            'status': status, 'reasons': reasons, 'repaired': repair,
            'maximum_correction': max_correction,
            'maximum_isotonic_correction': projected.maximum_isotonic_correction if projected else 0.0,
            'maximum_bounds_correction': projected.maximum_bounds_correction if projected else 0.0,
            'maximum_endpoint_correction': float(max(abs(cdf[0] - raw_cdf[0]), abs(cdf[-1] - raw_cdf[-1]))) if repair else 0.0,
            'pooled_blocks': projected.pooled_blocks if projected else 0,
            'changed_points': int(np.count_nonzero((cdf != raw_cdf) | (sf != raw_sf))) if repair else 0,
            'grid_k_min': left, 'grid_k_max': right, 'grid_points': len(k),
            'query_method': 'linear_in_log_moneyness' if repair else 'analytic',
            'integration_method': 'exact_linear_in_log_moneyness' if repair else 'trapezoid_log_price',
            'boundary_policy': 'finite_support_0_1' if repair else 'analytic_svi_wings',
            'raw_left_tail_mass': float(boundary_cdf[0]), 'raw_right_tail_mass': float(boundary_sf[1]),
            'raw_truncated_tail_mass': float(boundary_cdf[0] + boundary_sf[1]),
            'evaluated_left_tail_mass': float(cdf[0]), 'evaluated_right_tail_mass': float(sf[-1]),
            'raw_left_omitted_mean_usd': left_mean * self.forward_usd,
            'raw_right_omitted_mean_usd': right_mean * self.forward_usd,
            'truncated_survival_integral_usd': truncated * self.forward_usd,
            'omitted_left_integral_usd': omitted_left * self.forward_usd,
            'omitted_right_integral_usd': omitted_right * self.forward_usd,
            'mean_usd': full_mean * self.forward_usd,
            'relative_forward_error': abs(full_mean - 1),
            'raw_call_identity_relative_error': call_identity_error,
            'extrapolated_left_mass': float(obs_cdf[0]), 'extrapolated_right_mass': float(obs_sf[1]),
            'extrapolated_probability_mass': float(obs_cdf[0] + obs_sf[1]),
        })

    def _raw_tail_means(self, left: float, right: float) -> tuple[float, float]:
        w = total_variance([left, right], self.params)
        return (math.exp(left) - normalised_price(left, float(w[0]), 'put'),
                normalised_price(right, float(w[1]), 'call'))

    def _evaluate(self, k: Sequence[float] | float, repaired: bool) -> tuple[np.ndarray, np.ndarray]:
        x = np.asarray(k, dtype=float)
        if not np.isfinite(x).all():
            raise ValueError('I need finite log-moneyness')
        if repaired:
            cdf = np.interp(x, self._k, self._cdf, left=0.0, right=1.0)
            sf = np.interp(x, self._k, self._survival, left=1.0, right=0.0)
            return cdf, sf
        return analytic_probabilities(x, self.params)

    def probabilities(self, k: Sequence[float] | float) -> tuple[np.ndarray, np.ndarray]:
        return self._evaluate(k, self._diagnostics['repaired'])

    def cdf_at(self, price: float) -> CDFValue:
        if type(price) not in (int, float) or math.isnan(price) or price < 0:
            raise ValueError('I need a nonnegative price, optionally positive infinity')
        if price == 0:
            k, cdf, sf = -math.inf, 0.0, 1.0
        elif price == math.inf:
            k, cdf, sf = math.inf, 1.0, 0.0
        else:
            relative = (price - self.forward_usd) / self.forward_usd
            k = math.log1p(relative) if abs(relative) < 0.5 else math.log(price) - math.log(self.forward_usd)
            cdf, sf = map(float, self.probabilities(k))
        lower_price = self.forward_usd * math.exp(self.observed_k_min)
        upper_price = self.forward_usd * math.exp(self.observed_k_max)
        region = ('extrapolated_left' if price == 0 or price < lower_price else
                  'extrapolated_right' if price == math.inf or price > upper_price else 'observed_strike_range')
        return CDFValue(cdf, sf, region != 'observed_strike_range', region, self._diagnostics['repaired'],
                        bool(k < self._k[0] or k > self._k[-1]))

    def to_record(self) -> dict[str, Any]:
        return {'params': asdict(self.params), 'forward_usd': self.forward_usd, 't_years': self.t_years,
                'observed_k_min': self.observed_k_min, 'observed_k_max': self.observed_k_max,
                'settings': asdict(self.settings),
                'diagnostics': {**self._diagnostics, 'reasons': list(self._diagnostics['reasons'])}}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> 'ImpliedCDF':
        return cls(SVIParams(**record['params']), record['forward_usd'], record['t_years'],
                   record['observed_k_min'], record['observed_k_max'], CDFSettings(**record['settings']))
