"""I check SVI fitting on labelled synthetic smiles and offline database fixtures."""

from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np

from implied_calibration.cli import main
from implied_calibration.fitting import fit_database, svi_settings
from implied_calibration.plotting import smile_figure
from implied_calibration.protocol import validate_protocol
from implied_calibration.svi import (FitResult, SVIParams, SVISettings, SmileSlice,
                                     butterfly_g, check_calendars, check_grid, fit_svi,
                                     parameter_violations, spread_weights, total_variance,
                                     variance_derivatives)
from test_phase1 import OfflineTest
from test_phase2 import BASE, DatabaseFixture, ingest, quote
from test_phase3 import draft, synthetic_chain


def generated(params=None, noise=0.0):
    params = params or SVIParams(0.02, 0.15, -0.35, 0.03, 0.25)
    k = np.linspace(-1, 1, 41)
    iv = np.sqrt(total_variance(k, params) / 0.5)
    mid = iv + np.random.default_rng(1847).normal(0, noise, k.size)
    return k, mid - 0.01, mid + 0.01, mid, params


class SVITests(OfflineTest):
    def test_analytic_derivatives_match_finite_differences(self):
        params = generated()[-1]
        x = np.array([-0.7, -0.2, 0.0, 0.4, 0.9])
        h = 1e-4
        first, second = variance_derivatives(x, params)
        np.testing.assert_allclose(first, (total_variance(x + h, params) - total_variance(x - h, params)) / (2 * h), atol=1e-8)
        np.testing.assert_allclose(second, (total_variance(x + h, params) - 2 * total_variance(x, params)
                                           + total_variance(x - h, params)) / h ** 2, atol=3e-8)

    def test_noiseless_parameter_and_smile_recovery(self):
        k, bid, ask, mid, params = generated()
        result = fit_svi(k, bid, ask, mid, 0.5)
        self.assertEqual(result.status, 'ok')
        np.testing.assert_allclose(list(asdict(result.params).values()), list(asdict(params).values()), atol=1e-6)
        self.assertLess(result.rmse_vol_pts, 1e-6)
        self.assertEqual(result.frac_strikes_within_bid_ask, 1)
        self.assertEqual(result.starts_attempted, 10)
        self.assertGreater(result.starts_converged, 0)
        self.assertGreaterEqual(result.min_g, -1e-8)

    def test_noisy_smile_recovers_approximately(self):
        k, bid, ask, mid, params = generated(noise=0.002)
        result = fit_svi(k, bid, ask, mid, 0.5)
        self.assertEqual(result.status, 'ok')
        true_iv = np.sqrt(total_variance(k, params) / 0.5)
        fitted_iv = np.sqrt(total_variance(k, result.params) / 0.5)
        self.assertLess(float(np.sqrt(np.mean((fitted_iv - true_iv) ** 2))), 0.002)
        self.assertLess(abs(result.params.b - params.b), 0.02)
        self.assertLess(abs(result.params.rho - params.rho), 0.1)
        self.assertLess(abs(result.params.m - params.m), 0.05)
        self.assertLess(abs(result.params.s - params.s), 0.08)

    def test_deliberate_butterfly_violation_is_reported(self):
        params = SVIParams(0.001, 0.4, -0.7, 0, 0.01)
        k = np.linspace(-0.7, 0.7, 61)
        iv = np.sqrt(total_variance(k, params) / 0.5)
        self.assertEqual(parameter_violations(params), ())
        self.assertLess(float(butterfly_g(check_grid(params, k, SVISettings()), params).min()), -1e-8)
        result = fit_svi(k, iv - 0.002, iv + 0.002, iv, 0.5)
        self.assertEqual(result.status, 'butterfly_violation')
        self.assertIsNotNone(result.params)
        self.assertLess(result.min_g, -1e-8)

    def test_deterministic_with_seed_and_input_order(self):
        k, bid, ask, mid, _ = generated(noise=0.002)
        result = fit_svi(k, bid, ask, mid, 0.5)
        np.random.seed(123)
        self.assertEqual(result, fit_svi(k[::-1], bid[::-1], ask[::-1], mid[::-1], 0.5))

    def test_lee_bounds_and_negative_a_are_checked(self):
        self.assertIn('lee_wing_bound', parameter_violations(SVIParams(0.01, 1.2, 0.9, 0, 0.2)))
        self.assertEqual(parameter_violations(SVIParams(-0.02, 0.1, -0.25, 0.02, 0.3)), ())
        self.assertIn('negative_minimum_variance', parameter_violations(SVIParams(-0.1, 0.1, 0, 0, 0.2)))
        self.assertIn('s_bound', parameter_violations(SVIParams(0.1, 0.1, 0, 0, 1e-4)))
        self.assertIn('rho_bound', parameter_violations(SVIParams(0.1, 0.1, 0.999, 0, 0.2)))

    def test_spread_floor_and_weight_share_cap(self):
        bid = np.full(20, 0.4)
        ask = np.full(20, 0.5)
        ask[0] = bid[0] + 1e-12
        weights = spread_weights(bid, ask, SVISettings())
        self.assertLessEqual(weights.max() / weights.sum(), 0.2 + 1e-12)
        self.assertGreater(weights[0], weights[1])
        self.assertTrue(np.all(np.isfinite(weights)))

    def test_invalid_or_insufficient_input_returns_a_result(self):
        k, bid, ask, mid, _ = generated()
        for arrays, status in [(([], [], [], []), 'insufficient_strikes'),
                               ((k[:5], bid[:5], ask[:5], mid[:5]), 'insufficient_strikes'),
                               ((np.zeros(41), bid, ask, mid), 'insufficient_strikes'),
                               ((k, bid[:-1], ask, mid), 'invalid_input'),
                               ((k, np.full(41, np.nan), ask, mid), 'invalid_input'),
                               ((k, ask + 1, ask, mid), 'invalid_input')]:
            with self.subTest(status=status):
                self.assertEqual(fit_svi(*arrays, 0.5).status, status)
        self.assertEqual(fit_svi(k, bid, ask, mid, 0).status, 'invalid_input')
        with patch('implied_calibration.svi.least_squares', side_effect=ValueError('synthetic optimizer failure')):
            failure = fit_svi(k, bid, ask, mid, 0.5)
        self.assertEqual(failure.status, 'optimiser_failed')
        self.assertIn('synthetic optimizer failure', failure.message)
        self.assertEqual(failure.starts_attempted, 10)
        limited = fit_svi(k, bid, ask, mid, 0.5, SVISettings(n_starts=2, max_nfev=1))
        self.assertEqual(limited.status, 'optimiser_failed')

    def test_calendar_checks_same_snapshot_without_repairing_params(self):
        params = generated()[-1]
        first = FitResult('ok', params=params)
        lower_variance = replace(params, a=params.a - 0.01)
        later = FitResult('ok', params=lower_variance)
        slices = [SmileSlice('synthetic-a', BASE + 100, 0.1, (-0.5, 0.5), first),
                  SmileSlice('synthetic-a', BASE + 200, 0.2, (-0.4, 0.4), later),
                  SmileSlice('synthetic-b', BASE + 300, 0.3, (-0.6, 0.6), later)]
        output = check_calendars(slices)
        self.assertTrue(output[0].result.calendar_flag)
        self.assertTrue(output[1].result.calendar_flag)
        self.assertFalse(output[2].result.calendar_flag)
        self.assertEqual(output[2].result.calendar_pairs_checked, 0)
        self.assertEqual(output[0].result.params, params)
        self.assertEqual(output[1].result.params, lower_variance)
        self.assertEqual(output[0].result.status, 'ok')
        self.assertAlmostEqual(output[0].result.min_calendar_spread, -0.01)

    def test_plot_has_synthetic_label_bid_ask_and_fit(self):
        k, bid, ask, mid, _ = generated()
        result = fit_svi(k, bid, ask, mid, 0.5)
        figure = smile_figure(k, bid, ask, 0.5, result, synthetic=True, label='generated smile')
        self.assertIn('Synthetic', figure.axes[0].get_title())
        self.assertEqual(figure.axes[0].get_legend_handles_labels()[1], ['Bid IV', 'Ask IV', 'SVI fit'])
        output = io.BytesIO()
        figure.savefig(output, format='png')
        self.assertTrue(output.getvalue().startswith(b'\x89PNG'))
        figure.clear()

    def test_butterfly_rejected_slice_does_not_contaminate_primary_calendar_flags(self):
        params = generated()[-1]
        slices = [SmileSlice('synthetic', 10, 0.1, (-0.5, 0.5), FitResult('ok', params)),
                  SmileSlice('synthetic', 20, 0.2, (-0.5, 0.5),
                             FitResult('butterfly_violation', replace(params, a=0.001)))]
        result = check_calendars(slices)
        self.assertFalse(result[0].result.calendar_flag)
        self.assertEqual(result[0].result.calendar_pairs_checked, 0)
        self.assertIsNone(result[1].result.calendar_flag)
        self.assertEqual(result[1].result.params, slices[1].result.params)


class FittingTests(DatabaseFixture):
    def seed_synthetic_chain(self):
        rows = synthetic_chain()
        self.seed([row['instrument_name'] for row in rows])
        self.book(rows=[quote(row['instrument_name'], bid_price=row['bid_btc'], ask_price=row['ask_btc'],
                              mark_price=row['mark_btc'], mark_iv=60, underlying_price=100) for row in rows])
        ingest(self.recording, self.store)

    def test_gate_prevents_any_optimizer_call_and_reports_each_group(self):
        self.book()
        ingest(self.recording, self.store)
        with patch('implied_calibration.fitting.fit_svi', side_effect=AssertionError('Gate bypassed')):
            result = fit_database(self.store.path, draft(), BASE)
        self.assertEqual(result['status'], 'blocked_upstream')
        self.assertEqual(result['n_groups'], 1)
        self.assertEqual(result['n_fit_attempted'], 0)
        self.assertEqual(result['groups'][0]['fit']['status'], 'blocked_upstream')
        json.dumps(result, allow_nan=False)

    def test_as_of_fits_do_not_change_when_future_quotes_and_ticks_arrive(self):
        self.seed_synthetic_chain()
        before = fit_database(self.store.path, draft(), BASE)
        self.assertEqual(before['n_fit_attempted'], 1)
        self.assertEqual(before['n_primary_accepted'], 1)
        self.seed([row['instrument_name'] for row in synthetic_chain()], stamp=BASE + 100)
        self.book(stamp=BASE + 100)
        ingest(self.recording, self.store)
        self.store.connection.execute('UPDATE instruments SET tick_size=1 WHERE fetched_at_ms>?', (BASE,))
        self.store.connection.commit()
        self.assertEqual(before, fit_database(self.store.path, draft(), BASE))

    def test_cli_fit_returns_two_for_closed_gate_and_writes_json(self):
        self.book()
        ingest(self.recording, self.store)
        target = self.root / 'fit.json'
        argv = ['implied-calibration', 'fit', '--db', str(self.store.path), '--as-of-ms', str(BASE),
                '--protocol', str(Path(__file__).resolve().parents[1] / 'protocol/protocol.json'),
                '--output', str(target)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['n_fit_attempted'], 0)

    def test_draft_svi_protocol_rejects_invalid_coupled_choices(self):
        for key, value in [('lee_margin', 2), ('rho_limit', 1), ('maximum_weight_share', 0.01),
                           ('minimum_s', 1e-6), ('n_starts', 0), ('seed', -1),
                           ('grid_points', 2), ('primary_acceptance', 'all_fits')]:
            content = deepcopy(draft().content)
            content['svi'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_protocol(content)
        self.assertEqual(svi_settings(draft()).n_starts, 10)
