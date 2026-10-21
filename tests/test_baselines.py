"""Synthetic baseline distributions, drift conventions and seeded GARCH paths."""

from dataclasses import asdict, replace
import math
from unittest.mock import Mock, patch

import numpy as np
from scipy.integrate import quad

from implied_calibration.baseline_lock import BaselineSettings
from implied_calibration.baselines import BaselineCDF, hour_overlap, atm_baseline, garch_baseline, realised_baseline, simulate_garch
from implied_calibration.scoring import score_distribution
from test_phase1 import OfflineTest
from test_scoring import EXPIRY, flat_distribution


def prediction_fixture():
    frozen = flat_distribution()
    return {'forward_usd': 100., 't_years': .5, 'params': asdict(frozen.model.params),
            'scoring_support': frozen.support_record()}


def garch_returns(n=2160):
    rng = np.random.default_rng(4321)
    variance, residual = .0001, 0.
    returns = []
    for _ in range(n):
        variance = .000002 + .08 * residual ** 2 + .9 * variance
        residual = math.sqrt(variance) * rng.standard_t(8) * math.sqrt(6/8)
        returns.append(residual)
    return np.array(returns)


class BaselineMathTests(OfflineTest):
    def test_flat_svi_and_atm_baseline_have_identical_scores(self):
        frozen, atm = flat_distribution(), atm_baseline(prediction_fixture())
        for price in (70., 100., 120.):
            actual, expected = score_distribution(atm, price, EXPIRY), score_distribution(frozen, price, EXPIRY)
            for metric in actual.metrics:
                self.assertAlmostEqual(actual.metrics[metric], expected.metrics[metric], delta=1e-8)

    def test_realised_vol_uses_hourly_second_moment_and_snapshot_maturity(self):
        prediction = prediction_fixture()
        prediction['t_years'] = 25.5/(365*24)
        model = realised_baseline(prediction, np.array([.01, -.01, .02]))
        self.assertAlmostEqual(model.parameters['variance'], .0002*25.5)
        self.assertAlmostEqual(model.quantile(.5), 100*math.exp(-model.parameters['variance']/2))
        mean = quad(lambda p:model.quantile(p), 1e-10, 1-1e-10)[0]
        self.assertAlmostEqual(mean, 100., delta=1e-5)

    def test_baselines_share_frozen_bounds_and_explicit_boundary_atoms(self):
        prediction = prediction_fixture()
        prediction['scoring_support'].update(lower_usd=90., upper_usd=110.)
        model = atm_baseline(prediction)
        self.assertGreater(model.support_record()['left_atom_mass'], 0)
        self.assertGreater(model.support_record()['right_atom_mass'], 0)
        self.assertEqual(float(model.probabilities(89.)[0]), 0.)
        self.assertEqual(float(model.probabilities(110.)[0]), 1.)
        self.assertEqual(model.log_return_density(90.)[1], 'support_atom')

    def test_simulation_is_seeded_and_matches_conditional_one_hour_variance(self):
        state = {'omega': .000002, 'alpha': .08, 'beta': .9, 'nu': 8.,
                 'last_residual': .01, 'last_variance': .0001}
        settings = BaselineSettings()
        first, diagnostics = simulate_garch(state, 1., settings)
        second, _ = simulate_garch(state, 1., settings)
        np.testing.assert_array_equal(first, second)
        self.assertAlmostEqual(float(first.var()), .0001, delta=.000008)
        self.assertAlmostEqual(float(first.mean()), 0., delta=.0003)
        self.assertEqual(diagnostics['path_count'], 16384)
        fractional, _ = simulate_garch(state, .25, settings)
        np.testing.assert_allclose(fractional, .5*first)

    def test_gap_allocation_is_continuous_and_preserves_horizon(self):
        for gap in (0.,1e-10,.2,.9999999999,1.,1.0000000001,2.):
            weights = [hour_overlap(step,gap,24.5) for step in range(math.ceil(gap+24.5))]
            self.assertAlmostEqual(sum(weights),24.5)
            self.assertTrue(all(0<=w<=1 for w in weights))
        state = {'omega':.000002,'alpha':.08,'beta':.9,'nu':8.,'last_residual':.01,'last_variance':.0001}
        for gap in (0.,1.):
            first,_ = simulate_garch(state,24.,BaselineSettings(),gap_hours=gap)
            nearby,_ = simulate_garch(state,24.,BaselineSettings(),gap_hours=gap+1e-10)
            self.assertLess(float(np.max(np.abs(first-nearby))),1e-5)

    def test_garch_fit_simulation_roundtrip_and_log_axis_density(self):
        prediction = prediction_fixture()
        prediction['t_years'] = 24.5/(365*24)
        model = garch_baseline(prediction, garch_returns(), BaselineSettings())
        self.assertEqual(model, BaselineCDF(**model.to_record()))
        state = model.parameters['state']
        self.assertLess(state['alpha'] + state['beta'], 1.)
        self.assertGreater(state['nu'], 2.)
        self.assertEqual(model.parameters['simulation']['fractional_hour'], .5)
        k = (model.parameters['knots'][100] + model.parameters['knots'][101])/2
        h = 1e-7
        slope = (float(model.probabilities(100*math.exp(k+h))[0]) -
                 float(model.probabilities(100*math.exp(k-h))[0]))/(2*h)
        self.assertAlmostEqual(math.exp(model.log_return_density(100*math.exp(k))[0]), slope, places=6)
        score = score_distribution(model, 101., EXPIRY)
        self.assertEqual(score.status, 'ok')
        lo, hi = math.log(model.lower_usd/100), math.log(model.upper_usd/100)
        nodes = np.unique([lo, 0., hi, *model.parameters['knots']])
        expected = sum(quad(lambda k: float(model.interior_probabilities(100*math.exp(k))[0 if k<math.log(1.01) else 1])**2
                            *100*math.exp(k), a, b, points=[math.log(1.01)] if a<math.log(1.01)<b else None)[0]
                       for a, b in zip(nodes, nodes[1:]))
        self.assertAlmostEqual(score.metrics['crps_usd'], expected, delta=2e-5)

    def test_simulation_zero_density_is_explicit_and_is_not_a_garch_tail_claim(self):
        model = BaselineCDF(100., 1., 10000., 'garch_quantile_surrogate',
            {'knots': np.linspace(-.1,.1,17).tolist(), 'probabilities': np.linspace(0,1,17).tolist(),
             'simulation_tail_resolution': 1/16384})
        score = score_distribution(model, 120., EXPIRY)
        self.assertTrue(score.diagnostics['negative_log_score_is_infinite'])
        self.assertTrue(score.diagnostics['delivery_extrapolated'])
        self.assertEqual(model.support_record()['raw_price_mean'], 'undefined_for_garch_t')

    def test_nonconvergence_nonstationary_state_and_repeated_knots_are_rejected(self):
        prediction = prediction_fixture()
        with patch('arch.arch_model') as factory:
            factory.return_value.fit.return_value = Mock(convergence_flag=9)
            with self.assertRaisesRegex(ValueError, 'converge'):
                garch_baseline(prediction, garch_returns(), BaselineSettings())
        with self.assertRaises(ValueError):
            simulate_garch({'omega':1.,'alpha':.2,'beta':.9,'nu':8.,'last_residual':0.,'last_variance':1.}, 1., BaselineSettings())
        with self.assertRaises(ValueError):
            BaselineCDF(100.,1.,1000.,'garch_quantile_surrogate',
                        {'knots':[0.]*17,'probabilities':np.linspace(0,1,17).tolist()})
        for setting in (replace(BaselineSettings(), simulation_paths=10), replace(BaselineSettings(), atm_rule='other')):
            with self.assertRaises(ValueError):
                setting.validate()
