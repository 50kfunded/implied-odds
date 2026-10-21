"""Independent high-precision Gaussian checks for tail pricing."""

from decimal import Decimal, localcontext
import math

from implied_calibration.blackscholes import implied_vol, normalised_price, price_usd, vega_usd
from test_phase1 import OfflineTest


def decimal_normal_cdf(value: Decimal) -> Decimal:
    """Use a power series or a continued fraction, independent of production pricing."""
    pi = Decimal('3.1415926535897932384626433832795028841971693993751058209749445923078164062862089986280348253421170679')
    positive = abs(value)
    if positive >= 8:
        fraction = Decimal(0)
        for numerator in range(512, 0, -1):
            fraction = Decimal(numerator) / (positive + fraction)
        tail = (-positive * positive / 2).exp() / ((2 * pi).sqrt() * (positive + fraction))
    else:
        term = positive
        total = term
        for index in range(1, 2000):
            term *= -positive * positive * (2 * index - 1) / (2 * index * (2 * index + 1))
            total += term
            if abs(term) < Decimal('1e-105'):
                break
        else:
            raise AssertionError('High-precision reference series did not converge')
        tail = Decimal('0.5') - total / (2 * pi).sqrt()
    return tail if value < 0 else 1 - tail


def decimal_otm_price(k: float, w: float) -> float:
    with localcontext() as context:
        context.prec = 100
        log_strike = Decimal.from_float(k)
        variance = Decimal.from_float(w)
        root_w = variance.sqrt()
        d1 = -log_strike / root_w + root_w / 2
        d2 = d1 - root_w
        if k >= 0:
            result = decimal_normal_cdf(d1) - log_strike.exp() * decimal_normal_cdf(d2)
        else:
            result = log_strike.exp() * decimal_normal_cdf(-d2) - decimal_normal_cdf(-d1)
        return float(result)


class PrecisionTests(OfflineTest):
    def test_btc_usd_identity_and_call_put_round_trip_grid(self):
        successes = 0
        underflow_or_intrinsic = 0
        for forward in (1.0, 80000.0):
            for ratio in (0.5, 0.9, 1.0, 1.1, 2.0):
                for t_years in (1 / (365 * 24), 1 / 365, 1.0):
                    for iv in (0.2, 0.7, 1.8):
                        for side in ('call', 'put'):
                            strike = forward * ratio
                            price = price_usd(forward, strike, t_years, iv, side)
                            btc = normalised_price(math.log(ratio), iv * iv * t_years, side)
                            with self.subTest(forward=forward, ratio=ratio, t=t_years, iv=iv, side=side):
                                self.assertAlmostEqual(price / forward, btc, delta=max(1e-300, btc * 1e-11))
                                intrinsic = max(forward - strike, 0) if side == 'call' else max(strike - forward, 0)
                                result = implied_vol(price, forward, strike, t_years, side)
                                if price <= intrinsic:
                                    underflow_or_intrinsic += 1
                                    self.assertEqual(result.reason, 'at_or_below_intrinsic')
                                else:
                                    successes += 1
                                    self.assertEqual(result.reason, 'ok')
                                    repriced = price_usd(forward, strike, t_years, result.iv, side)
                                    self.assertLess(abs(repriced - price) / price, 1e-10)
        self.assertGreater(successes, 100)
        self.assertGreater(underflow_or_intrinsic, 0)

    def test_vega_matches_price_derivative_and_parity(self):
        for strike in (80.0, 100.0, 120.0):
            call = price_usd(100, strike, 0.5, 0.6, 'call')
            put = price_usd(100, strike, 0.5, 0.6, 'put')
            self.assertAlmostEqual(call - put, 100 - strike, places=12)
            h = 1e-5
            for side in ('call', 'put'):
                difference = (price_usd(100, strike, 0.5, 0.6 + h, side)
                              - price_usd(100, strike, 0.5, 0.6 - h, side)) / (2 * h)
                self.assertAlmostEqual(vega_usd(100, strike, 0.5, 0.6), difference, delta=1e-7)

    def test_inversion_boundaries_invalid_inputs_and_finite_bracket(self):
        for side, strike, intrinsic, upper in [('call', 80.0, 20.0, 100.0), ('put', 120.0, 20.0, 120.0)]:
            for price in (intrinsic, intrinsic - 1):
                result = implied_vol(price, 100, strike, 0.5, side)
                self.assertTrue(math.isnan(result.iv))
                self.assertEqual(result.reason, 'at_or_below_intrinsic')
            for price in (upper, upper + 1):
                self.assertEqual(implied_vol(price, 100, strike, 0.5, side).reason,
                                 'at_or_above_upper_bound')
        for change in [(math.nan, 100, 100, 1, 'call'), (1, 0, 100, 1, 'call'),
                       (1, 100, 100, 0, 'put'), (1, 100, 100, 1, 'bad'),
                       (1, 10 ** 400, 100, 1, 'call')]:
            self.assertEqual(implied_vol(*change).reason, 'invalid_input')
        price = price_usd(100, 100, 1, 0.5, 'call')
        self.assertEqual(implied_vol(price, 100, 100, 1, 'call', lower_iv=1, upper_iv=2).reason,
                         'outside_iv_bracket')
        self.assertEqual(implied_vol(price, 100, 100, 1, 'call', lower_iv=2, upper_iv=1).reason,
                         'invalid_bracket')
        self.assertEqual(implied_vol(price, 100, 100, 1, 'call', max_iterations=1).reason, 'solver_failed')
        self.assertEqual(price_usd(100, 80, 0, 0.6, 'call'), 20)
        self.assertEqual(price_usd(100, 120, 1, 0, 'put'), 20)

    def test_small_variance_and_tails_match_independent_decimal_reference(self):
        for variance in (1e-4, 1e-8, 1e-12, 1e-16, 1e-20, 1e-24, 1e-32):
            for standard_deviations in (0.0, 1e-6, 0.001, 0.1, 1.0, 5.0, 20.0, 30.0):
                for side in (-1, 1):
                    k = side * standard_deviations * math.sqrt(variance)
                    option_type = 'put' if k < 0 else 'call'
                    with self.subTest(w=variance, k=k, option_type=option_type):
                        reference = decimal_otm_price(k, variance)
                        actual = normalised_price(k, variance, option_type)
                        self.assertGreater(reference, 0)
                        self.assertTrue(math.isfinite(actual))
                        self.assertLess(abs(actual - reference) / reference, 5e-12)

    def test_independent_prices_invert_and_reprice_in_short_expiries(self):
        iv = 0.4
        for variance in (1e-4, 1e-8, 1e-12, 1e-16):
            for standard_deviations in (0.0, 0.1, 1.0, 5.0, 20.0, 30.0):
                for side in (-1, 1):
                    strike = math.exp(side * standard_deviations * math.sqrt(variance))
                    k = math.log1p(strike - 1)
                    option_type = 'put' if k < 0 else 'call'
                    reference = decimal_otm_price(k, variance)
                    with self.subTest(w=variance, strike=strike, option_type=option_type):
                        result = implied_vol(reference, 1.0, strike, variance / (iv * iv), option_type)
                        self.assertEqual(result.reason, 'ok')
                        repriced = price_usd(1.0, strike, variance / (iv * iv), result.iv, option_type)
                        self.assertLess(abs(repriced - reference) / reference, 1e-10)
                        self.assertLess(abs(result.iv - iv) / iv, 5e-11)

    def test_representable_deep_tail_and_underflow_are_distinct(self):
        self.assertGreater(normalised_price(0.3, 1e-4, 'call'), 0)
        self.assertEqual(normalised_price(1.0, 1e-8, 'call'), 0)
        self.assertEqual(normalised_price(-1.0, 1e-8, 'put'), 0)

    def test_vega_underflow_returns_nan_without_dividing_by_zero(self):
        self.assertTrue(math.isnan(vega_usd(1.0, 1.0, 1e-300, 1e-300)))
