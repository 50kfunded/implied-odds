"""I build same-snapshot reference distributions without fetching data."""

from dataclasses import asdict, dataclass
import math
from typing import Any

import numpy as np
from scipy.special import ndtr, ndtri
from scipy.stats import beta as beta_distribution

from .baseline_lock import BaselineSettings
from .svi import SVIParams, total_variance


@dataclass(frozen=True)
class BaselineCDF:
    forward_usd: float
    lower_usd: float
    upper_usd: float
    kind: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if (not all(math.isfinite(v) and v > 0 for v in (self.forward_usd, self.lower_usd, self.upper_usd))
                or self.lower_usd >= self.upper_usd):
            raise ValueError('Invalid baseline scoring support')
        if self.kind == 'lognormal':
            if not math.isfinite(self.parameters['variance']) or self.parameters['variance'] <= 0:
                raise ValueError('Baseline total variance must be positive')
        elif self.kind == 'garch_quantile_surrogate':
            k, p = np.asarray(self.parameters['knots']), np.asarray(self.parameters['probabilities'])
            if (k.ndim != 1 or len(k) < 17 or k.shape != p.shape or not np.isfinite(k).all()
                    or not np.isfinite(p).all() or np.any(np.diff(k) <= 0) or np.any(np.diff(p) <= 0)
                    or p[0] != 0 or p[-1] != 1):
                raise ValueError('I need strictly increasing finite simulation knots with 0/1 endpoints')
        else:
            raise ValueError('Unknown baseline distribution')

    def to_record(self) -> dict:
        return asdict(self)

    def integration_nodes(self) -> list[float]:
        if self.kind == 'lognormal':
            return []
        lower, upper = math.log(self.lower_usd/self.forward_usd), math.log(self.upper_usd/self.forward_usd)
        return [self.forward_usd*math.exp(k) for k in self.parameters['knots'] if lower < k < upper]

    def interior_probabilities(self, price: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        k = np.log(np.asarray(price, dtype=float) / self.forward_usd)
        if self.kind == 'lognormal':
            w = self.parameters['variance']
            z = (k + w / 2) / math.sqrt(w)
            return ndtr(z), ndtr(-z)
        p = np.interp(k, self.parameters['knots'], self.parameters['probabilities'])
        sf = np.interp(k, self.parameters['knots'], 1 - np.asarray(self.parameters['probabilities']))
        return p, sf

    def probabilities(self, price: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        x = np.asarray(price, dtype=float)
        p, s = self.interior_probabilities(np.maximum(x, self.lower_usd))
        return (np.where(x < self.lower_usd, 0., np.where(x >= self.upper_usd, 1., p)),
                np.where(x < self.lower_usd, 1., np.where(x >= self.upper_usd, 0., s)))

    def support_record(self) -> dict:
        left = float(self.interior_probabilities(self.lower_usd)[0])
        right = float(self.interior_probabilities(self.upper_usd)[1])
        return {'policy': 'collapse_tails_to_boundary_atoms', 'lower_usd': self.lower_usd,
                'upper_usd': self.upper_usd, 'left_atom_mass': left, 'right_atom_mass': right,
                'raw_price_mean': 'undefined_for_garch_t' if self.kind != 'lognormal' else self.forward_usd,
                'simulation_tail_resolution': self.parameters.get('simulation_tail_resolution'),
                'density_approximation': self.kind}

    def quantile(self, probability: float) -> float:
        if not 0 < probability < 1:
            raise ValueError('Quantile probability must lie strictly inside 0..1')
        if self.kind == 'lognormal':
            w = self.parameters['variance']
            k = -w / 2 + math.sqrt(w) * ndtri(probability)
        else:
            k = np.interp(probability, self.parameters['probabilities'], self.parameters['knots'])
        return self.forward_usd * math.exp(min(max(float(k), math.log(self.lower_usd / self.forward_usd)),
                                              math.log(self.upper_usd / self.forward_usd)))

    def log_return_density(self, price: float) -> tuple[float | None, str]:
        if price < self.lower_usd or price > self.upper_usd:
            return None, 'zero_density'
        if price in (self.lower_usd, self.upper_usd):
            return None, 'support_atom'
        k = math.log(price / self.forward_usd)
        if self.kind == 'lognormal':
            w = self.parameters['variance']
            return -.5 * (math.log(2 * math.pi * w) + (k + w / 2) ** 2 / w), 'ok'
        knots, p = np.asarray(self.parameters['knots']), np.asarray(self.parameters['probabilities'])
        index = int(np.searchsorted(knots, k, side='right')) - 1
        if index < 0 or index >= len(knots) - 1:
            return None, 'zero_density'
        return math.log((p[index + 1] - p[index]) / (knots[index + 1] - knots[index])), 'ok'

    def is_extrapolated(self, price: float) -> bool:
        if self.kind == 'lognormal':
            return False
        k = math.log(price / self.forward_usd)
        return k < self.parameters['knots'][0] or k > self.parameters['knots'][-1]


def lognormal(forward: float, variance: float, support: dict) -> BaselineCDF:
    return BaselineCDF(forward, support['lower_usd'], support['upper_usd'], 'lognormal', {'variance': variance})


def atm_baseline(prediction: dict) -> BaselineCDF:
    variance = float(total_variance(0., SVIParams(**prediction['params'])))
    return lognormal(prediction['forward_usd'], variance, prediction['scoring_support'])


def realised_baseline(prediction: dict, returns: np.ndarray) -> BaselineCDF:
    returns = np.asarray(returns, dtype=float)
    if returns.ndim != 1 or not len(returns) or not np.isfinite(returns).all():
        raise ValueError('Invalid realised-volatility history')
    variance = float(np.mean(returns ** 2)) * prediction['t_years'] * 365 * 24
    return lognormal(prediction['forward_usd'], variance, prediction['scoring_support'])


def hour_overlap(step: int, gap_hours: float, horizon_hours: float) -> float:
    return max(0., min(step+1., gap_hours+horizon_hours)-max(float(step),gap_hours))


def simulate_garch(state: dict, hours: float, settings: BaselineSettings, *, gap_hours: float = 0.) -> tuple[np.ndarray, dict]:
    settings.validate()
    omega, alpha, beta, nu = (state[k] for k in ('omega', 'alpha', 'beta', 'nu'))
    if (not all(math.isfinite(v) for v in (*state.values(), hours, gap_hours)) or hours <= 0 or gap_hours < 0
            or omega <= 0 or min(alpha, beta) < 0 or alpha + beta >= 1 or nu <= 2
            or state['last_variance'] <= 0):
        raise ValueError('Invalid stationary GARCH-t state')
    rng = np.random.default_rng(settings.simulation_seed)
    n = settings.simulation_paths
    variance = np.full(n, omega + alpha * state['last_residual'] ** 2 + beta * state['last_variance'])
    terminal = np.zeros(n)
    whole = math.floor(hours)
    fraction = hours - whole
    for step in range(math.ceil(gap_hours+hours)):
        with np.errstate(over='ignore', invalid='ignore'):
            innovation = np.sqrt(variance) * rng.standard_t(nu, n) * math.sqrt((nu - 2) / nu)
            terminal += innovation * math.sqrt(hour_overlap(step,gap_hours,hours))
            variance = omega + alpha * innovation ** 2 + beta * variance
        if not np.isfinite(terminal).all() or not np.isfinite(variance).all():
            raise ValueError('GARCH simulation produced nonfinite paths; I keep none of this forecast')
    return terminal, {'hours': hours, 'whole_hours': whole, 'fractional_hour': fraction,
                      'path_count': n, 'seed': settings.simulation_seed, 'discarded_gap_hours': gap_hours,
                      'gap_fractional_hour': gap_hours-math.floor(gap_hours)}


def garch_baseline(prediction: dict, returns: np.ndarray, settings: BaselineSettings, *, gap_hours: float = 0.) -> BaselineCDF:
    import arch
    from arch import arch_model

    returns = np.asarray(returns, dtype=float)
    if returns.ndim != 1 or len(returns) != settings.garch_window_hours or not np.isfinite(returns).all():
        raise ValueError('GARCH needs its full finite rolling return window')
    fit = arch_model(100 * returns, mean='Zero', vol='GARCH', p=1, q=1, dist='StudentsT',
                     rescale=False).fit(disp='off', show_warning=False)
    if fit.convergence_flag != 0:
        raise ValueError('GARCH optimiser did not converge')
    state = {'omega': float(fit.params['omega']) / 10000, 'alpha': float(fit.params['alpha[1]']),
             'beta': float(fit.params['beta[1]']), 'nu': float(fit.params['nu']),
             'last_residual': float(fit.resid[-1]) / 100,
             'last_variance': float(fit.conditional_volatility[-1]) ** 2 / 10000}
    samples, diagnostics = simulate_garch(state, prediction['t_years'] * 365 * 24, settings, gap_hours=gap_hours)
    probabilities = np.linspace(0, 1, settings.simulation_quantiles)
    knots = np.quantile(samples, probabilities, method='linear')
    support = prediction['scoring_support']
    lo, hi = (math.log(support[k] / prediction['forward_usd']) for k in ('lower_usd', 'upper_usd'))
    left_count, right_count = int(np.sum(samples <= lo)), int(np.sum(samples >= hi))
    def tail_upper(count: int) -> float:
        return 1. if count == len(samples) else float(beta_distribution.ppf(.95, count + 1, len(samples) - count))
    parameters = {'knots': knots.tolist(), 'probabilities': probabilities.tolist(), 'state': state,
                  'arch_version': arch.__version__, 'numpy_version': np.__version__,
                  'convergence_flag': int(fit.convergence_flag), 'simulation': diagnostics,
                  'simulation_tail_resolution': 1 / len(samples),
                  'quantile_probability_spacing': 1 / (len(probabilities) - 1),
                  'left_tail_path_count': left_count, 'right_tail_path_count': right_count,
                  'left_tail_probability_upper_95': tail_upper(left_count),
                  'right_tail_probability_upper_95': tail_upper(right_count),
                  'tail_interval_scope': 'Monte_Carlo_error_only_not_quantile_surrogate_error',
                  'log_loss_warning': 'Zero density can come from the finite simulation approximation.',
                  'raw_price_mean': 'undefined; I score the common bounded surrogate'}
    return BaselineCDF(prediction['forward_usd'], support['lower_usd'], support['upper_usd'],
                       'garch_quantile_surrogate', parameters)
