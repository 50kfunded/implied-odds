"""I price undiscounted Black-76 options and invert prices without doing I/O."""

from dataclasses import dataclass
import math

from scipy.integrate import quad
from scipy.optimize import brentq
from scipy.special import log_ndtr


@dataclass(frozen=True)
class IVResult:
    iv: float
    reason: str
    relative_price_error: float | None = None


def _finite(*values: float) -> bool:
    try:
        return all(type(value) in (int, float) and math.isfinite(value) for value in values)
    except OverflowError:
        return False


def _log_strike(forward_usd: float, strike_usd: float) -> float:
    if forward_usd / 2 < strike_usd < forward_usd * 2:
        return math.log1p((strike_usd - forward_usd) / forward_usd)
    return math.log(strike_usd) - math.log(forward_usd)


def _positive_integral_log_price(k: float, w: float) -> float:
    """I integrate a positive payoff when the normal-CDF terms nearly cancel."""
    root_w = math.sqrt(w)
    z = abs(k) / root_w + root_w / 2
    prefactor = max(k, 0.0) - z * z / 2 - math.log(2 * math.pi) / 2 + math.log(root_w)
    if prefactor == -math.inf:
        return -math.inf
    scale = z + 1

    def kernel(u: float) -> float:
        q = u / scale
        increment = root_w * q
        exponent = -z * q - q * q / 2
        if increment == 0:
            return math.exp(exponent) * u
        if increment > 50:
            return math.exp(exponent + increment) * u * (-math.expm1(-increment)) / increment
        return math.exp(exponent) * u * math.expm1(increment) / increment

    # I rescale the integral to stay near one even for very small total variance.
    result = quad(kernel, 0.0, 80.0, epsabs=5e-15, epsrel=5e-14,
                  limit=128, full_output=1)
    integral, error = result[:2]
    if len(result) != 3 or not math.isfinite(integral) or integral <= 0 or error > integral * 5e-12:
        return math.nan
    return prefactor + math.log(integral) - 2 * math.log(scale)


def _otm_log_price(k: float, w: float) -> float:
    if w == 0:
        return -math.inf
    root_w = math.sqrt(w)
    if k == 0:
        return math.log(math.erf(root_w / math.sqrt(8)))
    d1 = -k / root_w + root_w / 2
    d2 = d1 - root_w
    if k > 0:
        first, second = float(log_ndtr(d1)), k + float(log_ndtr(d2))
    else:
        first, second = k + float(log_ndtr(-d2)), float(log_ndtr(-d1))
    difference = second - first
    if math.isnan(difference) or difference > -0.01:
        return _positive_integral_log_price(k, w)
    return first + math.log(-math.expm1(difference))


def normalised_price(k: float, w: float, option_type: str) -> float:
    """I return USD premium / forward from log-strike and total variance."""
    if not _finite(k, w) or w < 0 or abs(k) > 700 or option_type not in ('call', 'put'):
        return math.nan
    otm = math.exp(_otm_log_price(k, w))
    if option_type == 'call':
        return otm + (-math.expm1(k) if k < 0 else 0.0)
    return otm + (math.expm1(k) if k > 0 else 0.0)


def price_usd(forward_usd: float, strike_usd: float, t_years: float,
              iv: float, option_type: str) -> float:
    if not _finite(forward_usd, strike_usd, t_years, iv):
        return math.nan
    if min(forward_usd, strike_usd) <= 0 or min(t_years, iv) < 0:
        return math.nan
    k = _log_strike(forward_usd, strike_usd)
    w = iv * iv * t_years
    if not math.isfinite(w) or abs(k) > 700 or option_type not in ('call', 'put'):
        return math.nan
    intrinsic_usd = max(forward_usd - strike_usd, 0) if option_type == 'call' else max(
        strike_usd - forward_usd, 0)
    return forward_usd * math.exp(_otm_log_price(k, w)) + intrinsic_usd


def vega_usd(forward_usd: float, strike_usd: float, t_years: float, iv: float) -> float:
    """I differentiate USD premium with respect to decimal volatility."""
    if not _finite(forward_usd, strike_usd, t_years, iv) or min(
            forward_usd, strike_usd, t_years, iv) <= 0:
        return math.nan
    root_w = iv * math.sqrt(t_years)
    if root_w == 0 or not math.isfinite(root_w):
        return math.nan
    k = _log_strike(forward_usd, strike_usd)
    d1 = -k / root_w + root_w / 2
    return forward_usd * math.sqrt(t_years) * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)


def implied_vol(price: float, forward_usd: float, strike_usd: float,
                t_years: float, option_type: str, *, lower_iv: float = 1e-4,
                upper_iv: float = 10.0, absolute_tolerance: float = 5e-324,
                relative_tolerance: float = 8.881784197001252e-16,
                max_iterations: int = 512, max_relative_price_error: float = 1e-10) -> IVResult:
    """I invert a USD premium with Brent and check its relative repricing error."""
    def failure(reason: str) -> IVResult:
        return IVResult(math.nan, reason)

    if (not _finite(price, forward_usd, strike_usd, t_years)
            or min(forward_usd, strike_usd, t_years) <= 0
            or option_type not in ('call', 'put')):
        return failure('invalid_input')
    if (not _finite(lower_iv, upper_iv, absolute_tolerance, relative_tolerance, max_relative_price_error)
            or not 0 < lower_iv < upper_iv or absolute_tolerance <= 0
            or relative_tolerance < 4 * math.ulp(1.0) or max_relative_price_error <= 0
            or type(max_iterations) is not int or max_iterations < 1):
        return failure('invalid_bracket')
    intrinsic_usd = max(forward_usd - strike_usd, 0) if option_type == 'call' else max(
        strike_usd - forward_usd, 0)
    upper_usd = forward_usd if option_type == 'call' else strike_usd
    if price <= intrinsic_usd:
        return failure('at_or_below_intrinsic')
    if price >= upper_usd:
        return failure('at_or_above_upper_bound')
    k = _log_strike(forward_usd, strike_usd)
    if abs(k) > 700:
        return failure('numerical_range')
    time_value = (price - intrinsic_usd) / forward_usd
    if time_value <= 0:
        return failure('numerical_range')
    target = math.log(time_value)

    def residual(iv: float) -> float:
        return _otm_log_price(k, iv * iv * t_years) - target

    low, high = residual(lower_iv), residual(upper_iv)
    if math.isnan(low) or math.isnan(high):
        return failure('numerical_failure')
    if low > 0 or high < 0:
        return failure('outside_iv_bracket')
    try:
        iv, solved = brentq(residual, lower_iv, upper_iv, xtol=absolute_tolerance,
                            rtol=relative_tolerance, maxiter=max_iterations,
                            full_output=True, disp=False)
    except (ValueError, OverflowError) as exc:
        return failure('numerical_failure' if isinstance(exc, OverflowError) else 'solver_failed')
    if not solved.converged:
        return failure('solver_failed')
    repriced = price_usd(forward_usd, strike_usd, t_years, iv, option_type)
    error = abs(repriced - price) / price
    if not math.isfinite(error) or error >= max_relative_price_error:
        return IVResult(math.nan, 'repricing_failed', error)
    return IVResult(float(iv), 'ok', error)
