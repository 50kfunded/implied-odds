import itertools
import math
import unittest

import numpy as np

from implied_calibration.progressive_evidence import (CashLedger, horizon_means,
                                                       qlike_mean_bound, simulate_cash)
from implied_calibration.progressive_report import check_numeric_tree, early_witness, exact_tree, simulation_experiment
from implied_calibration.variance_study import qlike


class ProgressiveEvidenceTests(unittest.TestCase):
    def test_qlike_affine_reduction_and_margin(self):
        for candidate, comparator, margin in ((2., 1., 0.), (1.5, .7, .1), (1e-100, 1e-101, 0.)):
            bound = qlike_mean_bound(candidate, comparator, margin)
            self.assertIsNotNone(bound)
            self.assertAlmostEqual(qlike(bound, candidate)-qlike(bound, comparator), -margin)
            self.assertLess(qlike(2*bound, candidate)-qlike(2*bound, comparator), -margin)

    def test_equal_or_lower_forecast_never_creates_a_mean_bound(self):
        self.assertIsNone(qlike_mean_bound(1., 1.))
        self.assertIsNone(qlike_mean_bound(.5, 1.))

    def test_bound_is_stable_near_equality_and_for_extreme_scales(self):
        self.assertAlmostEqual(qlike_mean_bound(math.nextafter(1., 2.), 1.), 1.)
        self.assertTrue(math.isfinite(qlike_mean_bound(1e308, 1e-308)))
        self.assertTrue(math.isfinite(qlike_mean_bound(1e308, 5e307)))
        self.assertTrue(math.isfinite(qlike_mean_bound(1e-307, 5e-308)))

    def test_invalid_bounds_are_rejected(self):
        for a, b, margin in ((0, 1, 0), (1, -1, 0), (math.nan, 1, 0), (2, 1, -1), (2, 1, math.inf)):
            with self.assertRaises(ValueError):
                qlike_mean_bound(a, b, margin)

    def test_reservation_and_partial_payout_known_answer(self):
        ledger = CashLedger()
        ledger.issue('first', 2, 2., 1.)
        self.assertEqual(ledger.cash, 0.)
        ledger.advance(1)
        self.assertEqual(ledger.credit('day1', 0, 1, 1.), .5)
        ledger.issue('second', 3, 2., .5)
        ledger.advance(2)
        self.assertEqual(ledger.credit('day2', 1, 2, 2.), 1.5)
        ledger.advance(3)
        self.assertEqual(ledger.credit('day3', 2, 3, 2.), .5)
        self.assertEqual(ledger.cash, 2.)
        replay = CashLedger.replay(ledger.events)
        self.assertEqual((ledger.cash, ledger.maximum), (replay.cash, replay.maximum))

    def test_no_borrowing_and_duplicate_claims(self):
        ledger = CashLedger()
        ledger.issue('first', 2, 1., 1.)
        for name, stake in (('second', .01), ('first', 0.)):
            with self.assertRaises(ValueError):
                ledger.issue(name, 2, 1., stake)
        self.assertEqual(ledger.cash, 0.)

    def test_repeated_outcome_is_idempotent_and_revisions_are_rejected(self):
        ledger = CashLedger()
        ledger.issue('first', 2, 1., 1.)
        ledger.advance(1)
        ledger.credit('day1', 0, 1, 1.)
        self.assertEqual(ledger.credit('day1', 0, 1, 1.), 0.)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            ledger.credit('day1', 0, 1, .9)
        with self.assertRaisesRegex(ValueError, 'same outcome twice'):
            ledger.credit('renamed_day1', 0, 1, 1.)
        self.assertEqual(ledger.cash, 1.)

    def test_unobserved_and_boundary_straddling_dividends_are_rejected(self):
        ledger = CashLedger()
        ledger.issue('first', 1, 1., .5)
        with self.assertRaises(ValueError):
            ledger.credit('future', 0, 1, 1.)
        ledger.advance(2)
        with self.assertRaisesRegex(ValueError, 'straddles'):
            ledger.credit('coarse', 0, 2, 2.)
        self.assertEqual(ledger.cash, .5)
        self.assertFalse(ledger.increments)

    def test_late_observations_credit_only_the_original_claim(self):
        ledger = CashLedger()
        ledger.issue('old', 1, 1., 1.)
        ledger.advance(2)
        ledger.issue('new', 3, 1., 0.)
        self.assertEqual(ledger.credit('delayed', 0, 1, 2.), 2.)
        self.assertEqual(ledger.cash, 2.)

    def test_scalar_ledger_matches_vector_runner_on_every_binary_path(self):
        horizon, periods, fraction = 3, 8, .3
        daily = np.array(list(itertools.product((0., 2.), repeat=periods)))
        paths = simulate_cash(daily, np.ones_like(daily), horizon=horizon, fractions=[fraction])
        for row, expected, maximum in zip(daily, paths['terminal_cash'], paths['maximum_cash']):
            ledger = CashLedger()
            for time, amount in enumerate(row):
                ledger.issue(str(time), time+horizon, 1., fraction*ledger.cash)
                ledger.advance(time+1)
                ledger.credit(str(time), time, time+1, amount/horizon)
            self.assertAlmostEqual(ledger.cash, expected)
            self.assertAlmostEqual(ledger.maximum, maximum)

    def test_completion_account_does_not_pay_before_maturity(self):
        daily = np.full((1, 2), 100.)
        result = simulate_cash(daily, np.ones_like(daily), horizon=3, fractions=[1.], completion_only=True)
        self.assertEqual(result['terminal_cash'][0], 0.)
        self.assertEqual(result['maximum_cash'][0], 1.)

    def test_observation_order_and_final_dividend_are_not_off_by_one(self):
        daily = np.array([[0., 3., 100.]])
        progressive = simulate_cash(daily, np.ones_like(daily), horizon=2, fractions=[1.])
        delayed = simulate_cash(daily, np.ones_like(daily), horizon=2, fractions=[1.], completion_only=True)
        self.assertAlmostEqual(progressive['terminal_cash'][0], 75.)
        self.assertAlmostEqual(delayed['terminal_cash'][0], 0.)

    def test_exact_stopping_checks_and_invalid_naive_product(self):
        for markov in (False, True):
            for horizon in (2, 3, 4):
                result = exact_tree(horizon)
                if markov:
                    result = exact_tree(horizon, markov=True)
                self.assertAlmostEqual(result['maximum_expected_stopped_cash'], 1.)
                if not markov:
                    self.assertGreater(result['naive_product_threshold_20_crossing'], .05)

    def test_exact_early_warning_witness(self):
        result = early_witness()
        self.assertAlmostEqual(result['null_expected_target_average'], 1.)
        self.assertLess(result['null_event_probability'], .05)
        self.assertEqual(result['progressive_first_crossing'], 11)
        self.assertEqual(result['completion_first_crossing'], 30)

    def test_markov_threshold_is_the_future_mean_not_the_current_daily_mean(self):
        transition, mean = np.array([[.8, .2], [.1, .9]]), np.array([.2, 3.])
        self.assertTrue(np.allclose(horizon_means(transition, mean, 2), (transition@mean+transition@transition@mean)/2))
        self.assertFalse(np.allclose(horizon_means(transition, mean, 30), mean))

    def test_invalid_simulation_inputs(self):
        for daily, thresholds, fractions in (([[-1]], [[1]], [.1]), ([[1]], [[0]], [.1]),
                                               ([[math.nan]], [[1]], [.1]), ([[1]], [[1]], [1.1])):
            with self.assertRaises(ValueError):
                simulate_cash(daily, thresholds, fractions=fractions)

    def test_small_simulation_is_seeded_and_reports_every_comparator(self):
        first = simulation_experiment(replications=20)
        self.assertEqual(first, simulation_experiment(replications=20))
        self.assertEqual(len(first), 4)
        for row in first:
            self.assertEqual(len(row['progressive']['individual_fraction_rates']), 7)
            self.assertIn('1.0', row['preselected_phase_zero'])

    def test_result_check_rejects_material_drift_but_allows_final_bits(self):
        check_numeric_tree({'count': 4, 'mean': 1.+1e-14}, {'count': 4, 'mean': 1.})
        for changed in ({'count': 5, 'mean': 1.}, {'count': 4, 'mean': 1.01}, {'count': 4}):
            with self.assertRaises(ValueError):
                check_numeric_tree(changed, {'count': 4, 'mean': 1.})


if __name__ == '__main__':
    unittest.main()
