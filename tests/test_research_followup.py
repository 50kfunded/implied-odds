import math
import unittest

import numpy as np

from implied_calibration.frequency_audit import cases_from_daily, nested_daily
from implied_calibration.qlike_attribution import components
from implied_calibration.variance_study import DAY_MS, qlike


class FollowupTests(unittest.TestCase):
    def test_qlike_attribution_matches_individual_scores(self):
        rng = np.random.default_rng(1729)
        actual, forecasts = np.exp(rng.normal(size=(2, 100, 2)))
        result = components(actual, forecasts)
        scalar = np.array([np.mean([qlike(a, f) for a, f in zip(y, z)]) for y, z in zip(actual, forecasts)])
        self.assertTrue(np.allclose(result[:, 2], scalar, atol=1e-14))
        self.assertTrue(np.allclose(result[:, 0]+result[:, 1], scalar, atol=1e-14))
        self.assertTrue(np.all(result[:, :2] >= -1e-14))

    def test_common_multiplicative_error_has_zero_relative_penalty(self):
        result = components([[1., 4.]], [[.5, 2.]])[0]
        self.assertAlmostEqual(result[1], 0.)
        self.assertAlmostEqual(result[0], 1-math.log(2))

    def test_opposing_errors_have_zero_common_penalty(self):
        result = components([[2., .5]], [[1., 1.]])[0]
        self.assertAlmostEqual(result[0], 0.)
        self.assertAlmostEqual(result[1], .25)

    def test_bad_attribution_inputs_rejected(self):
        for actual, forecast in (([[0, 1]], [[1, 1]]), ([[1, 1]], [[1, math.nan]]), ([1, 1], [1, 1])):
            with self.assertRaises(ValueError):
                components(actual, forecast)

    def test_nested_frequency_targets_known_constant_log_returns(self):
        change = np.array([.001, .002])
        close = np.exp(np.arange(289)[:, None]*change)
        for minutes in (5, 15, 60):
            variance = nested_daily(close, minutes)[0]
            expected = 288*(minutes/5)*change**2*365
            self.assertTrue(np.allclose(variance, expected, atol=1e-12))

    def test_frequency_close_endpoints_and_target_completion(self):
        daily = np.column_stack((np.arange(1., 101.), 2*np.arange(1., 101.)))
        origins = np.array([30, 31])*DAY_MS
        cases = cases_from_daily(daily, 0, origins, np.ones((2, 2)))
        self.assertAlmostEqual(cases[0].features[0][0], 30.)
        self.assertAlmostEqual(cases[0].features[0][1], np.mean(np.arange(24., 31.)))
        self.assertAlmostEqual(cases[0].actual[0], np.mean(np.arange(31., 61.)))
        self.assertEqual(cases[0].target_end_ms, 60*DAY_MS)
        changed = daily.copy()
        changed[30:] *= 2
        altered = cases_from_daily(changed, 0, origins, np.ones((2, 2)))
        self.assertEqual(cases[0].features, altered[0].features)
        self.assertNotEqual(cases[0].actual, altered[0].actual)

    def test_incomplete_nested_grid_rejected(self):
        with self.assertRaises(ValueError):
            nested_daily(np.ones((288, 2)), 5)


if __name__ == '__main__':
    unittest.main()
