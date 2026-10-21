"""Known-input dependence calculations and seeded AR(1) experiments."""

import math
import numpy as np
from scipy.special import ndtr

from implied_calibration.inference import (berkowitz_hac, dm_test, effective_sample_size, newey_west,
                                          overlap_count, paired_inference, stationary_bootstrap)
from test_phase1 import OfflineTest

DAY = 86400000


def ar1(seed, n, rho=.6):
    rng = np.random.default_rng(seed)
    noise = rng.normal(size=n+1000)*math.sqrt(1-rho*rho)
    x = np.empty(len(noise))
    x[0] = noise[0]
    for i in range(1,len(x)):
        x[i] = rho*x[i-1] + noise[i]
    return x[1000:]


class DependenceTests(OfflineTest):
    def test_newey_west_and_dm_match_hand_calculation(self):
        x = np.array([1.,2.,4.,3.,5.])
        y = x-x.mean()
        expected = (y@y + y[1:]@y[:-1])/5
        self.assertAlmostEqual(float(newey_west(x,1)[0,0]), expected)
        result = dm_test(x,1)
        self.assertAlmostEqual(result['statistic'], x.mean()/math.sqrt(expected/5))
        self.assertAlmostEqual(dm_test(-x,1)['statistic'], -result['statistic'])
        self.assertAlmostEqual(dm_test(x*1e-12,1)['statistic'], result['statistic'])
        self.assertEqual(dm_test(np.ones(10),1)['status'], 'insufficient data')

    def test_vector_hac_matches_explicit_matrix(self):
        x = np.array([[1.,2.],[4.,1.],[2.,7.],[3.,3.]])
        y = x-x.mean(axis=0)
        expected = (y.T@y + .5*(y[1:].T@y[:-1]+y[:-1].T@y[1:]))/4
        np.testing.assert_allclose(newey_west(x,1), expected)

    def test_stationary_bootstrap_is_seeded_and_requires_2000_resamples(self):
        x = ar1(1,200)
        self.assertEqual(stationary_bootstrap(x), stationary_bootstrap(x))
        with self.assertRaises(ValueError):
            stationary_bootstrap(x,resamples=1999)
        with self.assertRaises(ValueError):
            stationary_bootstrap([1.,math.inf])

    def test_stationary_bootstrap_coverage_on_many_ar1_simulations(self):
        covered = 0
        for seed in range(60):
            interval = stationary_bootstrap(ar1(100+seed,500),seed=1729+seed)['confidence_interval_95']
            covered += interval[0] <= 0 <= interval[1]
        self.assertGreaterEqual(covered, 50)
        self.assertLessEqual(covered, 60)

    def test_ess_reflects_positive_serial_dependence(self):
        values = ar1(123,12000,.6)
        expected = len(values)*(1-.6)/(1+.6)
        self.assertAlmostEqual(effective_sample_size(values), expected, delta=.25*expected)
        self.assertEqual(effective_sample_size(np.ones(200)), 0.)
        self.assertLessEqual(effective_sample_size(ar1(2,1000,-.6)),1000)

    def test_hac_ess_catches_delayed_dependence_missed_by_adjacent_pairs(self):
        noise = np.random.default_rng(18).normal(size=77)
        x = noise[7:]+noise[:-7]
        self.assertLess(effective_sample_size(x),35.)
        result = paired_inference(np.arange(70)*DAY,x,np.zeros(70),7*DAY,primary=True)
        self.assertEqual(result['status'],'insufficient data')

    def test_overlap_block_and_hac_lags_cover_lead(self):
        times = np.arange(200,dtype=np.int64)*(DAY//4)
        self.assertEqual(overlap_count(times,7*DAY),28)
        result = paired_inference(times,ar1(4,200,.1),np.zeros(200),7*DAY)
        self.assertEqual(result['expected_block_length'],28)
        self.assertEqual(result['hac_lags'],27)
        with self.assertRaises(ValueError):
            overlap_count(np.array([1,1]),DAY)

    def test_every_inference_field_is_gated_when_ess_is_small(self):
        x = ar1(1,40)
        result = paired_inference(np.arange(40)*DAY,x,np.zeros(40),DAY,primary=True)
        self.assertEqual(result['status'],'insufficient data')
        self.assertEqual(result['conclusion'],'insufficient data')
        self.assertIsNone(result['bootstrap'])
        self.assertIsNone(result['dm_cross_check'])
        pit = berkowitz_hac(ndtr(x))
        self.assertEqual(pit['status'],'insufficient data')
        self.assertIsNone(pit['p_value'])
        self.assertIsNone(pit['statistic'])

    def test_only_primary_comparison_gets_a_p_value(self):
        x = ar1(12,600,.1)-1
        times = np.arange(600)*DAY
        primary = paired_inference(times,x,np.zeros(600),DAY,primary=True)
        secondary = paired_inference(times,x,np.zeros(600),DAY)
        self.assertEqual(primary['status'],'ok')
        self.assertIsNotNone(primary['bootstrap']['p_value'])
        self.assertIsNone(secondary['bootstrap']['p_value'])
        self.assertIsNone(primary['dm_cross_check']['p_value'])
        self.assertEqual(secondary['conclusion'],'descriptive only')

    def test_correlated_calibrated_normal_pits_do_not_test_rho_zero(self):
        rejected = 0
        for seed in range(24):
            result = berkowitz_hac(ndtr(ar1(900+seed,1500,.6)))
            self.assertEqual(result['status'],'ok')
            rejected += result['p_value'] < .05
        self.assertLessEqual(rejected,5)
        shifted = berkowitz_hac(ndtr(ar1(901,1500,.6)+1))
        self.assertLess(shifted['p_value'],.001)

    def test_pit_transform_clipping_is_counted_without_changing_raw_input(self):
        pits = [0.,1.,.5]
        result = berkowitz_hac(pits)
        self.assertEqual(result['pit_clipped_count'],2)
        self.assertEqual(pits,[0.,1.,.5])
        with self.assertRaises(ValueError):
            berkowitz_hac([-.1,.5])
