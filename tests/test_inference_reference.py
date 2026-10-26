"""Reference tests reject gaps and retain the declared statistic conventions."""

import math
import unittest

import numpy as np
from scipy.stats import t

from implied_calibration.inference import newey_west
from implied_calibration.inference_reference import dm_hln, stationary_samples, subsampling
from implied_calibration.variance_study import DAY_MS


class ReferenceInferenceTests(unittest.TestCase):
    def test_hln_matches_the_declared_factor_and_student_reference(self):
        x = np.random.default_rng(43).normal(.1, 1, 400)
        result = dm_hln(x, lags=29)
        factor = math.sqrt((400+1-60+30*29/400)/400)
        expected = x.mean()/math.sqrt(newey_west(x, 29)[0, 0]/400)*factor
        self.assertAlmostEqual(result['statistic'], expected)
        self.assertAlmostEqual(result['p_value'], 2*t.sf(abs(expected), 399))
        negative = dm_hln(-x, lags=29)
        self.assertAlmostEqual(negative['statistic'], -result['statistic'])
        self.assertAlmostEqual(negative['p_value'], result['p_value'])
        self.assertIsNone(dm_hln(np.ones(400), lags=29)['p_value'])

    def test_both_methods_refuse_compressed_calendar_gaps(self):
        x = np.random.default_rng(6).normal(size=400)
        times = np.arange(400)*DAY_MS
        times[200:] += DAY_MS
        for method in (lambda: dm_hln(x, lags=59, timestamps=times),
                       lambda: subsampling(x, timestamps=times)):
            with self.assertRaisesRegex(ValueError, 'consecutive'):
                method()

    def test_subsampling_is_shift_and_scale_equivariant(self):
        x = np.random.default_rng(72).normal(size=400)
        base = np.asarray(subsampling(x)['confidence_interval_95'])
        moved = np.asarray(subsampling(3*x+2)['confidence_interval_95'])
        np.testing.assert_allclose(moved, 3*base+2, atol=1e-12)
        self.assertEqual(subsampling(x)['block'], math.floor(400**.7))
        self.assertIsNone(subsampling(x[:30])['confidence_interval_95'])

    def test_stationary_draws_are_seeded_and_keep_source_values(self):
        source = np.arange(30, dtype=float)
        a = stationary_samples(source, 100, 20, 10, np.random.default_rng(9))
        b = stationary_samples(source, 100, 20, 10, np.random.default_rng(9))
        np.testing.assert_array_equal(a, b)
        self.assertEqual(a.shape, (20, 100))
        self.assertTrue(set(a.ravel()).issubset(set(source)))


if __name__ == '__main__':
    unittest.main()
