"""I challenge SVI derivatives, weighting, bounded starts, and calendar flags independently."""

from dataclasses import replace
from decimal import Decimal, localcontext
import math

import numpy as np

from implied_calibration.svi import (
    FitResult, SmileSlice, SVIParams, SVISettings, check_calendars,
    fit_svi, parameter_violations, spread_weights, total_variance, variance_derivatives,
)
from test_phase1 import OfflineTest


def decimal_derivatives(k: float, params: SVIParams) -> tuple[float, float]:
    with localcontext() as context:
        context.prec = 80
        a, b, rho, m, s = (Decimal.from_float(value) for value in (
            params.a, params.b, params.rho, params.m, params.s))
        x = Decimal.from_float(k)
        h = s * Decimal('0.0001')

        def curve(point: Decimal) -> Decimal:
            offset = point - m
            return a + b * (rho * offset + (offset * offset + s * s).sqrt())

        left2, left, center, right, right2 = (curve(x + index * h) for index in (-2, -1, 0, 1, 2))
        first = (left2 - 8 * left + 8 * right - right2) / (12 * h)
        second = (-left2 + 16 * left - 30 * center + 16 * right - right2) / (12 * h * h)
        return float(first), float(second)


class IndependentSVITests(OfflineTest):
    def test_analytic_derivatives_match_high_precision_finite_differences(self):
        cases = (SVIParams(0.04, 0.15, -0.4, 0.05, 0.2),
                 SVIParams(-0.02, 0.3, -0.2, 0.03, 0.15),
                 SVIParams(0.02, 0.2, 0.7, 0.1, 0.00011))
        for params in cases:
            for offset in (-8, -1, 0, 1, 8):
                k = params.m + offset * params.s
                expected_first, expected_second = decimal_derivatives(k, params)
                first, second = variance_derivatives(k, params)
                with self.subTest(params=params, offset=offset):
                    self.assertLess(abs(float(first) - expected_first), 1e-11 * max(1, abs(expected_first)))
                    self.assertLess(abs(float(second) - expected_second), 1e-10 * max(1, abs(expected_second)))

    def test_weight_cap_and_equal_share_boundary(self):
        settings = SVISettings()
        bid = np.full(8, 0.5)
        ask = bid + np.array([1e-6, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        weights = spread_weights(bid, ask, settings)
        self.assertTrue(np.all(np.isfinite(weights)))
        self.assertTrue(np.all(weights > 0))
        self.assertAlmostEqual(float(weights.mean()), 1.0, places=14)
        self.assertLessEqual(float(weights.max() / weights.sum()), settings.maximum_weight_share + 1e-14)
        self.assertAlmostEqual(float(weights[-2] / weights[-1]), 0.6 ** 2 / 0.5 ** 2, places=12)
        equal = spread_weights(bid, ask, replace(settings, maximum_weight_share=1 / 8))
        np.testing.assert_allclose(equal, np.ones(8), rtol=1e-13, atol=1e-13)

    def test_negative_a_surface_recovers_without_forcing_a_nonnegative(self):
        params = SVIParams(-0.02, 0.3, -0.2, 0.03, 0.15)
        self.assertEqual(parameter_violations(params), ())
        k = np.linspace(-0.6, 0.6, 31)
        iv = np.sqrt(total_variance(k, params) / 0.5)
        result = fit_svi(k, iv - 0.005, iv + 0.005, iv, 0.5)
        self.assertEqual(result.status, 'ok')
        self.assertLess(result.params.a, 0)
        np.testing.assert_allclose(total_variance(k, result.params), total_variance(k, params),
                                   rtol=1e-9, atol=1e-11)

    def test_valid_narrow_s_bounds_do_not_break_random_starts(self):
        params = SVIParams(0.02, 0.03, -0.25, 0.0, 0.005)
        k = np.linspace(-0.1, 0.1, 15)
        iv = np.sqrt(total_variance(k, params) / 0.1)
        settings = replace(SVISettings(), maximum_s=0.02, n_starts=3)
        result = fit_svi(k, iv - 0.005, iv + 0.005, iv, 0.1, settings)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(result.starts_attempted, 3)
        self.assertLess(result.rmse_vol_pts, 1e-6)
        self.assertTrue(settings.minimum_s < result.params.s <= settings.maximum_s)

    def test_calendar_flags_are_pairwise_and_do_not_rewrite_parameters(self):
        params = SVIParams(0.02, 0.08, -0.3, 0.01, 0.15)
        k = (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)
        slices = [SmileSlice('synthetic-a', 10, 0.1, k, FitResult('ok', params)),
                  SmileSlice('synthetic-a', 20, 0.2, k, FitResult('ok', replace(params, a=0.03))),
                  SmileSlice('synthetic-a', 30, 0.3, k, FitResult('ok', replace(params, a=0.025))),
                  SmileSlice('synthetic-b', 15, 0.15, k, FitResult('ok', replace(params, a=0.01)))]
        checked = check_calendars(slices)
        self.assertEqual([entry.result.params for entry in checked], [entry.result.params for entry in slices])
        self.assertEqual([entry.result.calendar_flag for entry in checked[:3]], [False, True, True])
        self.assertEqual([entry.result.calendar_pairs_checked for entry in checked], [2, 2, 2, 0])
        self.assertAlmostEqual(checked[1].result.min_calendar_spread, -0.005, places=14)
        self.assertAlmostEqual(checked[2].result.min_calendar_spread, -0.005, places=14)
        self.assertTrue(all(entry.result.calendar_flag is None for entry in slices))

    def test_exact_lee_boundary_is_rejected(self):
        params = SVIParams(0.04, 2 / 1.5, 0.5, 0.0, 0.2)
        self.assertIn('lee_wing_bound', parameter_violations(params))
        self.assertNotIn('lee_wing_bound', parameter_violations(replace(params, b=math.nextafter(params.b, 0))))

    def test_invalid_tolerances_cannot_turn_a_bad_smile_into_an_accepted_fit(self):
        params = SVIParams(0.01, 0.8, 0.5, 0.1, 0.01)
        k = np.linspace(-0.7, 0.7, 41)
        iv = np.sqrt(total_variance(k, params) / 0.5)
        invalid_settings = ({'butterfly_tolerance': math.inf}, {'butterfly_tolerance': -1e-8},
                            {'calendar_tolerance': math.inf}, {'grid_standard_deviations': math.nan},
                            {'local_grid_scales': 0.0}, {'n_starts': True})
        for changes in invalid_settings:
            with self.subTest(changes=changes):
                result = fit_svi(k, iv - 0.005, iv + 0.005, iv, 0.5,
                                 replace(SVISettings(), **changes))
                self.assertEqual(result.status, 'invalid_input')
                self.assertEqual(result.starts_attempted, 0)
