"""I test historical timing, variance units and past-only correction on generated prices."""

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path

import numpy as np

from implied_calibration.historical_data import load_study
from implied_calibration.variance_study import (DAY_MS, HOUR_MS, VarianceForecast, VarianceObservation,
    analyse_variance, bootstrap_means, build_observations, fit_correction, historical_forecast,
    hourly_variance, qlike, summarise_rows)
from test_phase1 import OfflineTest


BASE = 2000 * DAY_MS


def generated(days=400):
    config = deepcopy(load_study(Path(__file__).resolve().parents[1] / 'protocol/historical-variance.json'))
    config.update(origin_start_ms=BASE, origin_end_ms=BASE + (days - 1) * DAY_MS,
                  recalibration_start_ms=BASE, nonoverlap_anchor_ms=BASE)
    prices = {}
    first_close = BASE - 720 * HOUR_MS
    for index, closed in enumerate(range(first_close, BASE + (days + 31) * DAY_MS, HOUR_MS)):
        price = 100 * math.exp(.001 if index % 2 else 0.)
        prices[closed - HOUR_MS] = (price, price, price, price)
    dvol = {origin - DAY_MS: (20., 20., 20., 20.)
            for origin in range(BASE - DAY_MS, BASE + days * DAY_MS, DAY_MS)}
    return prices, dvol, config


class VarianceStudyTests(OfflineTest):
    def test_hourly_variance_uses_720_returns_721_closes_and_365_day_units(self):
        prices, dvol, config = generated(2)
        value, reason = hourly_variance(prices, BASE - 720 * HOUR_MS, BASE, 8760)
        self.assertEqual(reason, 'ok')
        self.assertAlmostEqual(value, .001 ** 2 * 8760, places=13)
        forecast, reasons = historical_forecast(prices, dvol, BASE, config)
        self.assertEqual(reasons, [])
        self.assertAlmostEqual(forecast.dvol_variance, .04)
        self.assertEqual(forecast.dvol_known_at_ms, BASE)
        self.assertEqual(forecast.trailing_known_at_ms, BASE)
        del prices[BASE - 721 * HOUR_MS]
        self.assertEqual(historical_forecast(prices, dvol, BASE, config)[1], ['trailing_missing_prices'])

    def test_future_prices_and_uncompleted_dvol_cannot_change_forecast_inputs(self):
        prices, dvol, config = generated(2)
        before, _ = historical_forecast(prices, dvol, BASE, config)
        for opened in list(prices):
            if opened >= BASE:
                prices[opened] = (999., 999., 999., 999.)
        dvol[BASE] = (999., 999., 999., 999.)
        self.assertEqual(before, historical_forecast(prices, dvol, BASE, config)[0])

    def test_qlike_scale_is_mean_of_ratios_and_minimises_training_loss(self):
        past = [VarianceObservation(VarianceForecast(BASE + index * DAY_MS, BASE, BASE, v, .04),
                                   BASE, BASE + (index + 1) * DAY_MS, y, None)
                for index, (v, y) in enumerate(((.04, .02), (.08, .08)))]
        result = fit_correction(past, BASE + 2 * DAY_MS, 2)
        self.assertAlmostEqual(result['factor'], .75)
        def loss(scale):
            return sum(qlike(row.realised_variance, scale * row.forecast.dvol_variance) for row in past)
        self.assertLess(loss(.75), loss(.74))
        self.assertLess(loss(.75), loss(.76))
        self.assertNotAlmostEqual(result['factor'], .1 / .12)
        self.assertEqual(qlike(.04, .04), 0)
        self.assertAlmostEqual(qlike(.08, .04), 1 - math.log(2))
        with self.assertRaises(ValueError):
            qlike(0., .04)

    def test_correction_excludes_unfinished_outcomes_and_includes_exact_boundary(self):
        prices, dvol, config = generated(400)
        rows, excluded = build_observations(prices, dvol, config)
        self.assertEqual(excluded, [])
        origin = BASE + 394 * DAY_MS
        before = fit_correction(rows, origin, 365)
        self.assertIsNotNone(before)
        self.assertEqual(before['n_completed_origins'], 365)
        self.assertEqual(before['latest_training_outcome_ms'], origin)
        changed = [replace(row, realised_variance=row.realised_variance * 1000)
                   if row.target_end_ms > origin else row for row in rows]
        self.assertEqual(before, fit_correction(changed, origin, 365))
        self.assertIsNone(fit_correction(rows, origin - 1, 365))
        self.assertAlmostEqual(before['factor'], .00876 / .04, places=11)

    def test_missing_future_price_gets_an_excluded_origin_reason(self):
        prices, dvol, config = generated(1)
        del prices[BASE + 719 * HOUR_MS]
        rows, excluded = build_observations(prices, dvol, config)
        self.assertEqual(rows, [])
        self.assertEqual(excluded, [{'origin_ms': BASE, 'reasons': ['future_missing_prices']}])

    def test_declared_alignment_sensitivities_use_old_dvol_or_fully_future_window(self):
        prices, dvol, config = generated(2)
        dvol[BASE - 2 * DAY_MS] = (30., 30., 30., 30.)
        forecast, _ = historical_forecast(prices, dvol, BASE, config, dvol_lag_days=1)
        self.assertEqual(forecast.dvol_known_at_ms, BASE - DAY_MS)
        self.assertEqual(forecast.dvol_variance, .09)
        rows, _ = build_observations(prices, dvol, config, future_start_shift_hours=1)
        self.assertEqual(rows[0].target_start_ms, BASE + HOUR_MS)
        self.assertEqual(rows[0].target_end_ms, BASE + 721 * HOUR_MS)
        self.assertEqual(rows[0].forecast.trailing_known_at_ms, BASE)

    def test_bootstrap_is_seeded_and_rejects_internal_calendar_gaps(self):
        values = np.array([[0.], [0.], [100.], [100.]])
        dates = np.array([0, 1, 10, 11], dtype=np.int64) * DAY_MS
        args = dict(spacing_ms=DAY_MS, block_rows=1000000, resamples=2000, seed=1)
        with self.assertRaisesRegex(ValueError, 'calendar gaps'):
            bootstrap_means(values, dates, **args)
        compressed = bootstrap_means(values, np.arange(4) * DAY_MS, **args)
        np.testing.assert_array_equal(compressed, bootstrap_means(values, np.arange(4) * DAY_MS, **args))
        self.assertLess(float(np.std(compressed)), 1)

    def test_small_sample_is_gated_and_nonoverlap_blocks_use_calendar_days(self):
        prices, dvol, config = generated(3)
        result = analyse_variance(prices, dvol, config)
        self.assertEqual(result['primary']['status'], 'insufficient data')
        self.assertEqual(result['primary']['conclusion'], 'insufficient data')
        self.assertEqual(result['primary']['n'], 3)
        rows, _ = build_observations(prices, dvol, config)
        summary = summarise_rows([rows[0], replace(rows[1], forecast=replace(rows[1].forecast, origin_ms=BASE + 30 * DAY_MS))],
                                  config, spacing_days=30)
        self.assertEqual(summary['expected_block_rows'], 2)
        self.assertEqual(summary['calendar_gaps'], 0)

    def test_calendar_gaps_withhold_intervals_and_formal_claims(self):
        prices, dvol, config = generated(3)
        rows, _ = build_observations(prices, dvol, config)
        summary = summarise_rows([rows[0], rows[2]], config)
        self.assertEqual(summary['calendar_gaps'], 1)
        for metric in summary['metrics'].values():
            self.assertIsNone(metric['confidence_interval_95'])
            self.assertIsNone(metric['hac_standard_error'])
            self.assertFalse(metric['ess_gate_passed'])
