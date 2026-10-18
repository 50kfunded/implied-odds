"""I score frozen risk-neutral CDFs without reading files or fetching prices."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Callable, Any

import numpy as np
from scipy.optimize import brentq

from .density import ImpliedCDF
from .svi import butterfly_g, total_variance


@dataclass(frozen=True)
class ScoreSettings:
    pit_transform_epsilon: float = 1e-6
    crps_initial_points: int = 4097
    crps_max_refinements: int = 4
    crps_relative_tolerance: float = 1e-7
    crps_absolute_tolerance_usd: float = 1e-6

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'Invalid scoring choice: {name}')
        if (not 0 < self.pit_transform_epsilon < 0.01 or type(self.crps_initial_points) is not int
                or self.crps_initial_points < 513 or type(self.crps_max_refinements) is not int
                or not 1 <= self.crps_max_refinements <= 8):
            raise ValueError('Invalid scoring grid or PIT transform choice')


@dataclass(frozen=True)
class CRPSResult:
    value: float
    estimated_error: float
    converged: bool
    grid_points: int
    outside_support_distance: float


def crps_integral(cdf: Callable[[np.ndarray], np.ndarray], survival: Callable[[np.ndarray], np.ndarray],
                  observed: float, lower: float, upper: float, *, log_price: bool = False,
                  settings: ScoreSettings = ScoreSettings(), extra_price_nodes: tuple = ()) -> CRPSResult:
    if (not all(math.isfinite(v) for v in (observed, lower, upper)) or lower >= upper
            or log_price and lower <= 0):
        raise ValueError('Invalid CRPS outcome or integration support')
    left, right = (math.log(lower), math.log(upper)) if log_price else (lower, upper)
    pivot_price = min(max(observed, lower), upper)
    pivot = math.log(pivot_price) if log_price else pivot_price
    outside = max(lower - observed, 0) + max(observed - upper, 0)

    def price_at(node: float) -> float:
        return math.exp(node) if log_price else node

    def quantile_node(probability: float) -> float:
        if float(np.asarray(cdf(np.array([lower])))[0]) >= probability:
            return left
        if float(np.asarray(cdf(np.array([upper])))[0]) <= probability:
            return right
        return brentq(lambda node: float(np.asarray(cdf(np.array([price_at(node)])))[0]) - probability,
                      left, right, xtol=1e-12)

    local_left, centre_left, median, centre_right, local_right = [
        quantile_node(p) for p in (1e-12, .001, .5, .999, 1 - 1e-12)]
    previous = None
    extra = np.asarray(extra_price_nodes, dtype=float)
    if not np.isfinite(extra).all() or np.any((extra <= lower) | (extra >= upper)):
        raise ValueError('Extra integration nodes must lie strictly inside the scoring support')
    extra = np.log(extra) if log_price else extra
    count = settings.crps_initial_points
    for refinement in range(settings.crps_max_refinements + 1):
        grid = np.unique(np.concatenate((np.linspace(left, right, count),
                                         np.linspace(local_left, local_right, count),
                                         np.linspace(centre_left, centre_right, count), [pivot, median], extra)))
        pieces = []
        intervals = [(left, min(pivot, median), cdf, False),
                     (median, pivot, survival, True), (pivot, median, cdf, True),
                     (max(pivot, median), right, survival, False)]
        for start, stop, function, complement in intervals:
            if stop <= start:
                continue
            nodes = grid[(grid >= start) & (grid <= stop)]
            price = np.exp(nodes) if log_price else nodes
            probability = np.asarray(function(price), dtype=float)
            if probability.shape != price.shape or not np.isfinite(probability).all() or np.any(
                    (probability < 0) | (probability > 1)):
                raise ValueError('Invalid probabilities in CRPS integration')
            jacobian = price if log_price else np.ones_like(price)
            integrand = probability * (probability - 2) if complement else probability ** 2
            integral = float(np.trapezoid(integrand * jacobian, nodes))
            pieces.append(integral + price_at(stop) - price_at(start) if complement else integral)
        value = math.fsum(pieces) + outside
        error = abs(value - previous) if previous is not None else math.inf
        if previous is not None and error <= max(settings.crps_absolute_tolerance_usd,
                                                 settings.crps_relative_tolerance * abs(value)):
            return CRPSResult(value, error, True, len(grid), outside)
        previous = value
        count = 2 * count - 1
    return CRPSResult(value, error, False, len(grid), outside)


def expiry_class(expiry_ms: int) -> str:
    if type(expiry_ms) is not int:
        raise ValueError('Expiry must be integer UTC milliseconds')
    expiry = datetime.fromtimestamp(expiry_ms / 1000, timezone.utc)
    if (expiry.hour, expiry.minute, expiry.second, expiry.microsecond) != (8, 0, 0, 0):
        raise ValueError('I only classify 08:00 UTC expiries')
    if expiry.weekday() != 4:
        return 'daily'
    if (expiry + timedelta(days=7)).month == expiry.month:
        return 'weekly'
    return 'quarterly' if expiry.month in (3, 6, 9, 12) else 'monthly'


@dataclass(frozen=True)
class FrozenCDF:
    model: ImpliedCDF
    lower_usd: float
    upper_usd: float

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> 'FrozenCDF':
        model = ImpliedCDF.from_record(record['distribution'])
        support = record['scoring_support']
        diagnostics = model.to_record()['diagnostics']
        if diagnostics['status'] != 'ok' or diagnostics['repaired']:
            raise ValueError('I only score unrepaired, resolved CDFs')
        expected = cls.for_model(model)
        if support != expected.support_record():
            raise ValueError('Frozen scoring support differs from its declared CDF')
        return expected

    @classmethod
    def for_model(cls, model: ImpliedCDF) -> 'FrozenCDF':
        report = model.to_record()['diagnostics']
        if report['status'] != 'ok' or report['repaired']:
            raise ValueError('CDF failed the primary distribution checks')
        return cls(model, model.forward_usd * math.exp(report['grid_k_min']),
                   model.forward_usd * math.exp(report['grid_k_max']))

    def support_record(self) -> dict[str, Any]:
        report = self.model.to_record()['diagnostics']
        return {'policy': 'collapse_tails_to_boundary_atoms', 'lower_usd': self.lower_usd,
                'upper_usd': self.upper_usd, 'left_atom_mass': report['raw_left_tail_mass'],
                'right_atom_mass': report['raw_right_tail_mass'],
                'omitted_raw_probability_mass': report['raw_truncated_tail_mass'],
                'raw_tail_mean_bound_usd': report['raw_left_omitted_mean_usd']
                                         + report['raw_right_omitted_mean_usd']}

    def probabilities(self, price: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        x = np.asarray(price, dtype=float)
        safe = np.maximum(x, self.lower_usd)
        cdf, sf = self.model.probabilities(np.log(safe / self.model.forward_usd))
        return (np.where(x < self.lower_usd, 0.0, np.where(x >= self.upper_usd, 1.0, cdf)),
                np.where(x < self.lower_usd, 1.0, np.where(x >= self.upper_usd, 0.0, sf)))

    def interior_probabilities(self, price: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.model.probabilities(np.log(price / self.model.forward_usd))

    def is_extrapolated(self, price: float) -> bool:
        return self.model.cdf_at(price).extrapolated

    def quantile(self, probability: float) -> float:
        if not 0 < probability < 1:
            raise ValueError('Quantile probability must be strictly between zero and one')
        report = self.model.to_record()['diagnostics']
        if probability <= report['raw_left_tail_mass']:
            return self.lower_usd
        if 1 - probability <= report['raw_right_tail_mass']:
            return self.upper_usd
        root = brentq(lambda k: float(self.model.probabilities(k)[0]) - probability,
                      report['grid_k_min'], report['grid_k_max'], xtol=1e-12)
        return self.model.forward_usd * math.exp(root)

    def log_return_density(self, price: float) -> tuple[float | None, str]:
        if price < self.lower_usd or price > self.upper_usd:
            return None, 'zero_density'
        if price in (self.lower_usd, self.upper_usd):
            return None, 'support_atom'
        k = math.log(price / self.model.forward_usd)
        w = float(total_variance(k, self.model.params))
        g = float(butterfly_g(k, self.model.params))
        if not math.isfinite(g) or g <= 0:
            return None, 'nonpositive_density'
        d2 = -k / math.sqrt(w) - math.sqrt(w) / 2
        return math.log(g) - 0.5 * math.log(2 * math.pi * w) - 0.5 * d2 ** 2, 'ok'


@dataclass(frozen=True)
class ScoreRow:
    status: str
    expiry_class: str
    metrics: dict[str, float | None]
    diagnostics: dict[str, Any]


def score_distribution(distribution: FrozenCDF, observed_usd: float, expiry_ms: int,
                       settings: ScoreSettings = ScoreSettings()) -> ScoreRow:
    if type(observed_usd) not in (int, float) or not math.isfinite(observed_usd) or observed_usd <= 0:
        raise ValueError('I need a finite positive official delivery price')
    cdf, _ = distribution.probabilities(observed_usd)
    pit = float(cdf)
    transformed = min(max(pit, settings.pit_transform_epsilon), 1 - settings.pit_transform_epsilon)
    crps = crps_integral(lambda p: distribution.interior_probabilities(p)[0],
                         lambda p: distribution.interior_probabilities(p)[1],
                         observed_usd, distribution.lower_usd, distribution.upper_usd,
                         log_price=True, settings=settings,
                         extra_price_nodes=tuple(distribution.integration_nodes()) if hasattr(distribution, 'integration_nodes') else ())
    log_density, log_status = distribution.log_return_density(observed_usd)
    metrics = {'pit': pit, 'pit_transformed': transformed, 'crps_usd': crps.value,
               'negative_log_score_log_return': -log_density if log_density is not None else None}
    intervals = {}
    for level in (0.5, 0.8, 0.9):
        lower, upper = distribution.quantile((1 - level) / 2), distribution.quantile((1 + level) / 2)
        metrics[f'coverage_{int(level * 100)}'] = float(lower <= observed_usd <= upper)
        intervals[str(int(level * 100))] = {'lower_usd': lower, 'upper_usd': upper,
                                          'lower_extrapolated': distribution.is_extrapolated(lower),
                                          'upper_extrapolated': distribution.is_extrapolated(upper)}
    status = 'invalid_density' if log_status == 'nonpositive_density' else 'ok' if crps.converged else 'crps_unresolved'
    return ScoreRow(status, expiry_class(expiry_ms), metrics,
                    {'tail_exceedance': 'left' if observed_usd < distribution.lower_usd else
                                        'right' if observed_usd > distribution.upper_usd else None,
                     'pit_transform_clipped': transformed != pit, 'pit_transform_epsilon': settings.pit_transform_epsilon,
                     'log_score_axis': 'ln_delivery_over_frozen_forward', 'log_score_status': log_status,
                     'negative_log_score_is_infinite': log_status == 'zero_density',
                     'delivery_extrapolated': distribution.is_extrapolated(observed_usd),
                     'crps': asdict(crps), 'scoring_support': distribution.support_record(), 'intervals': intervals})
