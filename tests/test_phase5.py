"""I check CDF extraction with labelled synthetic slices and offline database fixtures."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
from scipy.integrate import quad
from scipy.special import ndtr

from implied_calibration.blackscholes import price_usd
from implied_calibration.cli import main
from implied_calibration.density import (CDFSettings, ImpliedCDF, analytic_probabilities, bounded_pava)
from implied_calibration.distributions import cdf_database
from implied_calibration.plotting import cdf_figure
from implied_calibration.protocol import validate_protocol
from implied_calibration.svi import SVIParams, total_variance
from test_phase1 import OfflineTest
from test_phase2 import BASE, DatabaseFixture, ingest, quote
from test_phase3 import draft, synthetic_chain


def synthetic_cdf(params=None, settings=CDFSettings()):
    return ImpliedCDF(params or SVIParams(0.02, 0.15, -0.35, 0.03, 0.25),
                      100.0, 0.5, -0.3, 0.3, settings)


class CDFTests(OfflineTest):
    def test_flat_vol_limit_is_lognormal_without_scaling_total_variance_twice(self):
        params = SVIParams(0.04, 0.0, 0.0, 0.0, 0.2)
        k = np.linspace(-1.5, 1.5, 601)
        expected = ndtr((k + 0.04 / 2) / math.sqrt(0.04))
        cdf, sf = analytic_probabilities(k, params)
        np.testing.assert_allclose(cdf, expected, atol=2e-16)
        np.testing.assert_allclose(sf, ndtr((-k - 0.04 / 2) / math.sqrt(0.04)), atol=2e-16)
        first = synthetic_cdf(params)
        self.assertEqual(first.cdf_at(110).cdf, replace(first, t_years=0.1).cdf_at(110).cdf)
        self.assertEqual(first.to_record()['diagnostics']['status'], 'ok')

    def test_skewed_digital_matches_black_call_finite_differences_at_three_steps(self):
        model = synthetic_cdf()
        k = np.linspace(-0.8, 0.8, 17)
        strikes = model.forward_usd * np.exp(k)
        cdf, survival = model.probabilities(k)
        for relative_step in (1e-4, 3e-5, 1e-5):
            finite_difference = []
            for strike in strikes:
                step = strike * relative_step
                prices = []
                for shifted in (strike - step, strike + step):
                    shifted_k = math.log(shifted / model.forward_usd)
                    iv = math.sqrt(float(total_variance(shifted_k, model.params)) / model.t_years)
                    prices.append(price_usd(model.forward_usd, float(shifted), model.t_years, iv, 'call'))
                finite_difference.append(-(prices[1] - prices[0]) / (2 * step))
            with self.subTest(relative_step=relative_step):
                np.testing.assert_allclose(survival, finite_difference, atol=2e-8, rtol=2e-7)
        d2 = (-k - total_variance(k, model.params) / 2) / np.sqrt(total_variance(k, model.params))
        self.assertGreater(float(np.max(np.abs(survival - ndtr(d2)))), 0.02)
        np.testing.assert_allclose(cdf + survival, 1.0, atol=2e-16)

    def test_probabilities_are_bounded_and_monotone_on_wide_grid(self):
        model = synthetic_cdf()
        cdf, sf = model.probabilities(np.linspace(-64, 64, 20001))
        self.assertTrue(np.all((0 <= cdf) & (cdf <= 1)))
        self.assertTrue(np.all((0 <= sf) & (sf <= 1)))
        self.assertTrue(np.all(np.diff(cdf) >= 0))
        self.assertTrue(np.all(np.diff(sf) <= 0))
        self.assertFalse(model.to_record()['diagnostics']['repaired'])

    def test_forward_integral_and_tail_mass_for_flat_and_skewed_slices(self):
        for params in (SVIParams(0.04, 0.0, 0.0, 0.0, 0.2), synthetic_cdf().params):
            model = synthetic_cdf(params)
            report = model.to_record()['diagnostics']
            with self.subTest(params=params):
                self.assertLess(abs(report['truncated_survival_integral_usd'] / 100 - 1), 2e-6)
                self.assertLess(report['relative_forward_error'], 2e-6)
                self.assertLess(report['raw_call_identity_relative_error'], 2e-6)
                self.assertLess(report['raw_truncated_tail_mass'], 1e-10)
                integral, error = quad(lambda k: float(model.probabilities(k)[1]) * math.exp(k),
                                       report['grid_k_min'], report['grid_k_max'], epsabs=1e-10)
                self.assertLess(error, 1e-8)
                self.assertAlmostEqual(integral + report['omitted_left_integral_usd'] / 100
                                       + report['omitted_right_integral_usd'] / 100, 1.0, places=9)

    def test_direct_survival_preserves_tiny_tail_after_cdf_rounds_to_one(self):
        cdf, sf = analytic_probabilities(20.0, synthetic_cdf().params)
        self.assertEqual(float(cdf), 1.0)
        self.assertGreater(float(sf), 0)
        self.assertLess(float(sf), 1e-30)

    def test_narrow_flat_variance_keeps_the_known_forward_with_an_irregular_grid(self):
        model=synthetic_cdf(SVIParams(.001,0.,0.,-.9,.1))
        report=model.to_record()['diagnostics']
        self.assertEqual(report['status'],'ok')
        self.assertAlmostEqual(report['mean_usd'],100.,delta=2e-4)

    def test_heavy_lee_wings_report_unresolved_mean_without_rescaling(self):
        model = synthetic_cdf(SVIParams(8.0, 1.99, 0.0, 0.0, 1.0))
        report = model.to_record()['diagnostics']
        self.assertEqual(report['status'], 'tail_not_resolved')
        self.assertIn('tail_mean_unresolved', report['reasons'])
        self.assertFalse(report['repaired'])
        self.assertLess(report['raw_right_tail_mass'], 1e-25)
        self.assertGreater(report['raw_right_omitted_mean_usd'] / 100, 0.5)
        self.assertLess(report['truncated_survival_integral_usd'] / 100, 0.5)

    def test_bounded_pava_reports_isotonic_and_bounds_corrections(self):
        result = bounded_pava([-0.1, 0.4, 0.2, 1.2])
        np.testing.assert_allclose(result.values, [0, 0.3, 0.3, 1])
        self.assertAlmostEqual(result.maximum_correction, 0.2)
        self.assertAlmostEqual(result.maximum_isotonic_correction, 0.1)
        self.assertAlmostEqual(result.maximum_bounds_correction, 0.2)
        self.assertEqual(result.pooled_blocks, 1)
        self.assertEqual(result.changed_points, 4)
        self.assertEqual(bounded_pava([0.1, 0.3, 0.8]).maximum_correction, 0)

    def test_repaired_queries_are_bounded_monotone_and_report_changed_mean(self):
        model = synthetic_cdf(SVIParams(0.001, 0.4, -0.7, 0.0, 0.01))
        report = model.to_record()['diagnostics']
        self.assertTrue(report['repaired'])
        self.assertGreater(report['maximum_correction'], 0.1)
        self.assertGreater(report['pooled_blocks'], 0)
        self.assertGreater(report['maximum_bounds_correction'], 0)
        self.assertGreater(report['maximum_endpoint_correction'], 0)
        self.assertEqual(report['boundary_policy'], 'finite_support_0_1')
        k = np.linspace(-70, 70, 30001)
        cdf, sf = model.probabilities(k)
        self.assertTrue(np.all((0 <= cdf) & (cdf <= 1)))
        self.assertTrue(np.all(np.diff(cdf) >= 0))
        np.testing.assert_allclose(cdf + sf, 1)
        np.testing.assert_array_equal(cdf, model.probabilities(k[::-1])[0][::-1])
        self.assertEqual(model.cdf_at(100).cdf, float(model.probabilities(0)[0]))
        self.assertIn('forward_mismatch', report['reasons'])
        self.assertIsNone(report['raw_call_identity_relative_error'])
        self.assertGreater(report['relative_forward_error'], 0.01)
        integral, _ = quad(lambda x: float(model.probabilities(x)[1]) * math.exp(x), -1, 1, epsabs=1e-6)
        self.assertGreater(integral, 0)

    def test_extrapolated_probability_matches_the_same_query_curve(self):
        for params in (synthetic_cdf().params, SVIParams(0.001, 0.4, -0.7, 0, 0.01)):
            model = synthetic_cdf(params)
            left = model.cdf_at(100 * math.exp(-0.3))
            right = model.cdf_at(100 * math.exp(0.3))
            self.assertAlmostEqual(model.to_record()['diagnostics']['extrapolated_probability_mass'],
                                   left.cdf + right.survival, places=14)
            self.assertFalse(model.cdf_at(100).extrapolated)
            self.assertEqual(model.cdf_at(10).region, 'extrapolated_left')
            self.assertEqual(model.cdf_at(1000).region, 'extrapolated_right')

    def test_repaired_mean_matches_independent_gaussian_quadrature_of_queries(self):
        model = synthetic_cdf(SVIParams(0.01, 0.8, 0.5, 0.1, 0.01))
        self.assertTrue(model.to_record()['diagnostics']['repaired'])
        nodes, weights = np.polynomial.legendre.leggauss(5)
        half_width = np.diff(model._k) / 2
        locations = (model._k[:-1] + half_width)[:, None] + half_width[:, None] * nodes
        _, survival = model.probabilities(locations)
        integral = float(np.sum(half_width * ((survival * np.exp(locations)) @ weights)))
        expected = integral + math.exp(float(model._k[0]))
        self.assertAlmostEqual(model.to_record()['diagnostics']['mean_usd'] / 100, expected, places=11)

    def test_observed_strike_boundaries_are_inside_and_adjacent_prices_are_extrapolated(self):
        for forward in (100.0, 80000.0):
            model = replace(synthetic_cdf(), forward_usd=forward)
            lower, upper = forward * math.exp(-0.3), forward * math.exp(0.3)
            self.assertFalse(model.cdf_at(lower).extrapolated)
            self.assertFalse(model.cdf_at(upper).extrapolated)
            self.assertTrue(model.cdf_at(math.nextafter(lower, 0)).extrapolated)
            self.assertTrue(model.cdf_at(math.nextafter(upper, math.inf)).extrapolated)

    def test_plot_labels_synthetic_inputs_and_extrapolated_wings(self):
        figure = cdf_figure(synthetic_cdf(), synthetic=True, label='generated slice')
        self.assertIn('Synthetic', figure.axes[0].get_title())
        self.assertIn('Extrapolated SVI wings', figure.axes[0].get_legend_handles_labels()[1])
        output = io.BytesIO()
        figure.savefig(output, format='png')
        self.assertTrue(output.getvalue().startswith(b'\x89PNG'))
        figure.clear()

    def test_record_reconstructs_exact_queries_without_storing_grids(self):
        for params in (synthetic_cdf().params, SVIParams(0.001, 0.4, -0.7, 0, 0.01)):
            model = synthetic_cdf(params)
            record = json.loads(json.dumps(model.to_record(), allow_nan=False))
            self.assertLess(len(json.dumps(record)), 4000)
            rebuilt = ImpliedCDF.from_record(record)
            self.assertEqual(record, rebuilt.to_record())
            for price in (0, 1, 80, 100, 120, 1e20, math.inf):
                self.assertEqual(model.cdf_at(price), rebuilt.cdf_at(price))
                json.dumps(asdict(model.cdf_at(price)), allow_nan=False)
            record['diagnostics']['reasons'].append('synthetic mutation')
            self.assertNotIn('synthetic mutation', model.to_record()['diagnostics']['reasons'])

    def test_invalid_inputs_and_settings_fail_explicitly(self):
        model = synthetic_cdf()
        for changes in ({'forward_usd': 0}, {'t_years': 0}, {'t_years': math.nan},
                        {'observed_k_min': 0.4}, {'observed_k_max': 100},
                        {'params': SVIParams(0.0, 0.0, 0, 0, 0.2)},
                        {'params': SVIParams(0.1, 2.0, 0, 0, 0.2)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(model, **changes)
        for price in (-1, math.nan, True):
            with self.assertRaises(ValueError):
                model.cdf_at(price)
        for values in ([], [0.1, math.nan], [[0.1, 0.2]]):
            with self.assertRaises(ValueError):
                bounded_pava(values)
        for field, value in [('maximum_log_extent', 601), ('grid_points', 512),
                             ('tail_probability_tolerance', 1), ('initial_log_extent', 0)]:
            content = deepcopy(draft().content)
            content['cdf'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_protocol(content)


class CDFDatabaseTests(DatabaseFixture):
    def seed_synthetic_chain(self):
        rows = synthetic_chain()
        self.seed([row['instrument_name'] for row in rows])
        self.book(rows=[quote(row['instrument_name'], bid_price=row['bid_btc'], ask_price=row['ask_btc'],
                              mark_price=row['mark_btc'], mark_iv=60, underlying_price=100) for row in rows])
        ingest(self.recording, self.store)

    def test_closed_gate_never_constructs_a_distribution(self):
        self.book()
        ingest(self.recording, self.store)
        with patch('implied_calibration.distributions.ImpliedCDF', side_effect=AssertionError('Gate bypassed')):
            result = cdf_database(self.store.path, draft(), BASE)
        self.assertEqual(result['status'], 'blocked_upstream')
        self.assertEqual(result['n_cdf_constructed'], 0)
        self.assertEqual(result['cdf_status_counts'], {'blocked_upstream': 1})
        json.dumps(result, allow_nan=False)

    def test_cdf_as_of_is_unchanged_by_future_quotes_and_metadata(self):
        self.seed_synthetic_chain()
        before = cdf_database(self.store.path, draft(), BASE)
        self.assertEqual(before['n_cdf_constructed'], 1)
        self.seed([row['instrument_name'] for row in synthetic_chain()], stamp=BASE + 100)
        self.book(stamp=BASE + 100)
        ingest(self.recording, self.store)
        self.store.connection.execute('UPDATE instruments SET tick_size=1 WHERE fetched_at_ms>?', (BASE,))
        self.store.connection.commit()
        self.assertEqual(before, cdf_database(self.store.path, draft(), BASE))
        group = before['groups'][0]
        self.assertLessEqual(group['fetched_at_ms'], before['as_of_ms'])
        json.dumps(before, allow_nan=False)

    def test_cli_cdf_writes_closed_gate_json_and_exit_two(self):
        self.book()
        ingest(self.recording, self.store)
        target = self.root / 'cdf.json'
        argv = ['implied-calibration', 'cdf', '--db', str(self.store.path), '--as-of-ms', str(BASE),
                '--protocol', str(Path(__file__).resolve().parents[1] / 'protocol/protocol.json'),
                '--output', str(target)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['n_cdf_constructed'], 0)
