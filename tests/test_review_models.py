"""New benchmark specifications preserve availability, units and failure reporting."""

from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.optimize import check_grad

from implied_calibration.incremental import VarianceCase
from implied_calibration.review_models import (bates_granger, completed_rows, daily_returns,
    benchmark_records, ewma_forecast, garch_forecast, load_review_spec, log_smearing,
    mz_forecast, qlike_fit, qlike_objective)
from implied_calibration.variance_study import DAY_MS, HOUR_MS


ROOT = Path(__file__).resolve().parents[1]


class ReviewModelTests(unittest.TestCase):
    def setUp(self):
        self.spec, _ = load_review_spec(ROOT)

    def test_public_spec_matches_amendment_and_retains_separate_cohorts(self):
        self.assertEqual(len(self.spec['models']), 17)
        self.assertTrue(self.spec['exploratory_focus'].startswith('qlike_har_dvol minus qlike_har'))
        self.assertNotEqual(self.spec['common_origin_start'], self.spec['window_check_cohort']['start'])

    def test_completed_labels_include_boundary_and_exclude_unfinished_targets(self):
        rows = [VarianceCase(i*DAY_MS, i*DAY_MS, (i+30)*DAY_MS, (.1, .1, .1, .1), float(i+1)) for i in range(800)]
        chosen = completed_rows(rows, 759*DAY_MS, 730)
        self.assertEqual(len(chosen), 730)
        self.assertEqual(chosen[-1].target_end_ms, 759*DAY_MS)
        changed = [row if row.target_end_ms <= 759*DAY_MS else VarianceCase(row.feature_cutoff_ms,
                   row.target_start_ms, row.target_end_ms, row.features, 1e100) for row in rows]
        self.assertEqual(chosen, completed_rows(changed, 759*DAY_MS, 730))
        with self.assertRaises(ValueError):
            completed_rows(rows, 758*DAY_MS, 730)

    def test_log_smearing_recovers_arithmetic_target_mean_without_gaussian_assumption(self):
        x = np.ones((6, 3))
        y = np.array([.01, .02, .03, .07, .1, .4])
        fit = log_smearing(x, y, np.ones(3))
        self.assertAlmostEqual(fit['forecast'], y.mean(), places=12)
        self.assertGreater(fit['forecast'], np.exp(np.log(y).mean()))
        with self.assertRaises(ValueError):
            log_smearing(x, [0, *y[1:]], np.ones(3))

    def test_qlike_gradient_and_intercept_optimum(self):
        rng = np.random.default_rng(12)
        design = np.column_stack((np.ones(30), rng.normal(size=(30, 3))))
        y = np.exp(rng.normal(-1, .5, 30))
        beta = np.array([-.7, .1, -.2, .05])
        error = check_grad(lambda b: qlike_objective(b, design, y, .01)[0],
                           lambda b: qlike_objective(b, design, y, .01)[1], beta)
        self.assertLess(error, 1e-6)
        fit = qlike_fit(np.ones((30, 3)), y, np.ones(3), self.spec['qlike'])
        self.assertAlmostEqual(fit['forecast'], float(y.mean()), places=7)

    def test_optimizer_failure_is_not_a_forecast(self):
        with patch('implied_calibration.review_models.minimize', return_value=SimpleNamespace(success=False, message='limit')):
            with self.assertRaisesRegex(ValueError, 'optimizer failed'):
                qlike_fit(np.ones((10, 3)), np.ones(10), np.ones(3), self.spec['qlike'])

    def test_daily_return_cutoff_and_ewma_units(self):
        prices = {(day*DAY_MS-HOUR_MS): (1., 1., 1., float(np.exp(.01*day))) for day in range(32)}
        prices[32*DAY_MS-HOUR_MS] = (1., 1., 1., 1e50)
        returns = daily_returns(prices, 31*DAY_MS, 31)
        np.testing.assert_allclose(returns, .01, atol=1e-15)
        self.assertAlmostEqual(ewma_forecast(returns), 365*.01**2, places=12)
        with self.assertRaises(ValueError):
            daily_returns({}, 31*DAY_MS, 31)

    def test_bates_granger_uses_only_completed_genuine_forecasts(self):
        rows = [{'origin_ms': i*DAY_MS, 'target_end_ms': (i+30)*DAY_MS,
                 'actual': .1+i/1000, 'har': .1+i/1000, 'dvol': .5} for i in range(400)]
        result = bates_granger(rows, 395*DAY_MS, .2, .4)
        self.assertAlmostEqual(result['har_weight'], 1.)
        self.assertAlmostEqual(result['forecast'], .2)
        self.assertLessEqual(result['latest_weight_target_end_ms'], 395*DAY_MS)
        future = [{**row, 'actual': 1e20} if row['target_end_ms'] > 395*DAY_MS else row for row in rows]
        self.assertEqual(result, bates_granger(future, 395*DAY_MS, .2, .4))
        self.assertEqual(result, bates_granger(list(reversed(rows)), 395*DAY_MS, .2, .4))
        failed = [{**row, 'har': None} if row['origin_ms'] == 360*DAY_MS else row for row in rows]
        with self.assertRaisesRegex(ValueError, 'vintage failed'):
            bates_granger(failed, 395*DAY_MS, .2, .4)
        same = [{**row, 'har': .2, 'dvol': .2} for row in rows]
        self.assertEqual(bates_granger(same, 395*DAY_MS, .2, .4)['har_weight'], .5)

    def test_garch_averages_all_thirty_variances_and_records_failures(self):
        fitted = SimpleNamespace(convergence_flag=0, params={'omega': 1.},
                                 forecast=lambda **kw: SimpleNamespace(variance=np.arange(1., 31.)[None, :]))
        fake = SimpleNamespace(fit=lambda **kw: fitted)
        with patch('arch.arch_model', return_value=fake):
            result = garch_forecast(np.full(730, .01), 800*DAY_MS, self.spec['garch'])
            self.assertAlmostEqual(result['forecast'], 15.5*365/10000)
            fitted.convergence_flag = 1
            with self.assertRaisesRegex(ValueError, 'optimizer failed'):
                garch_forecast(np.full(730, .01), 800*DAY_MS, self.spec['garch'])

    def test_mz_negative_raw_prediction_keeps_declared_floor_and_hit(self):
        result = mz_forecast([1., 2., 3.], [.3, .2, .1], 10., 1e-6)
        self.assertLess(result['raw_prediction'], 0)
        self.assertTrue(result['floor_hit'])
        self.assertEqual(result['forecast'], 1e-6)

    def test_egarch_scores_first_batch_and_reports_independent_batch_gap(self):
        seen = []
        def forecast(**kwargs):
            seen.append(kwargs['rng']((2,)))
            level = 2. if len(seen) % 2 else 8.
            return SimpleNamespace(simulations=SimpleNamespace(variances=np.full((1, 2000, 30), level)))
        fitted = SimpleNamespace(convergence_flag=0, params={'omega': 1.}, forecast=forecast)
        with patch('arch.arch_model', return_value=SimpleNamespace(fit=lambda **kw: fitted)):
            result = garch_forecast(np.full(730, .01), 800*DAY_MS, self.spec['garch'], egarch=True)
        self.assertAlmostEqual(result['forecast'], 2*365/10000)
        self.assertAlmostEqual(result['independent_batch_relative_gap'], 3.)
        self.assertAlmostEqual(result['monte_carlo_standard_error'], 0., places=15)
        self.assertFalse(np.array_equal(seen[0], seen[1]))
        seed = self.spec['garch']['seed']+800
        np.testing.assert_array_equal(seen[0], np.random.default_rng(seed).standard_normal((2,)))
        np.testing.assert_array_equal(seen[1], np.random.default_rng(seed+10_000_000).standard_normal((2,)))

    def test_coverage_failures_and_floor_hits_survive_the_compact_summary(self):
        def stamp(day):
            return datetime.fromtimestamp(day*DAY_MS/1000, timezone.utc).isoformat()
        cases = [VarianceCase(i*DAY_MS, i*DAY_MS, (i+30)*DAY_MS, (.1, .12, .15, .2), .13) for i in range(1100)]
        plan = {'training_completed_origins': 365, 'forecast_floor_variance': 1e-6}
        spec = {**self.spec, 'common_origin_start': stamp(1000), 'common_origin_end': stamp(1000)}
        fake = {'forecast': .15, 'floor_hit': False, 'monte_carlo_standard_error': 0., 'independent_batch_relative_gap': 0.}
        with patch('implied_calibration.review_models.daily_returns', return_value=np.full(730, .01)), \
             patch('implied_calibration.review_models.garch_forecast', return_value=fake), \
             patch('implied_calibration.review_models.mz_forecast', return_value={'forecast': 1e-6, 'floor_hit': True, 'raw_prediction': -.1}):
            result = benchmark_records(cases, {}, plan, spec)
        self.assertEqual(result['floor_hits']['mz_dvol'], 1)
        self.assertEqual(result['raw_prediction_minimum']['mz_dvol'], -.1)
        self.assertEqual(result['coverage']['mz_dvol'], 1)
        self.assertEqual(len(result['common_records']), 1)
        short = {**spec, 'common_origin_start': stamp(450), 'common_origin_end': stamp(450)}
        with patch('implied_calibration.review_models.daily_returns', return_value=np.full(730, .01)), \
             patch('implied_calibration.review_models.garch_forecast', return_value=fake) as garch:
            result = benchmark_records(cases, {}, plan, short)
        self.assertEqual(result['coverage']['har_730'], 0)
        self.assertEqual(result['coverage']['dvol'], 1)
        self.assertEqual(result['coverage']['garch_11'], 1)
        self.assertEqual(len(result['common_records']), 0)
        self.assertIn('har_730', [row['model'] for row in result['failures']])
        self.assertEqual(garch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
