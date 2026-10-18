"""I check proper scores on synthetic normal, lognormal and SVI distributions."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
from unittest.mock import patch

import numpy as np
from scipy.integrate import quad
from scipy.special import ndtr

from implied_calibration.density import ImpliedCDF
from implied_calibration.scoring import FrozenCDF, ScoreSettings, crps_integral, expiry_class, score_distribution
from implied_calibration.svi import SVIParams
from test_phase1 import OfflineTest

EXPIRY = int(datetime(2026,12,5,8,tzinfo=timezone.utc).timestamp()*1000)


def flat_distribution():
    return FrozenCDF.for_model(ImpliedCDF(SVIParams(.04, 0., 0., 0., .2), 100., .5, -.3, .3))



class ProperScoreTests(OfflineTest):
    def test_crps_matches_normal_closed_form_on_the_same_integrator(self):
        mu, sigma = 1., 2.
        for outcome in (-3., 0., 1., 8.):
            z = (outcome - mu) / sigma
            expected = sigma * (z * (2 * ndtr(z) - 1) + 2 * math.exp(-z*z/2) / math.sqrt(2*math.pi) - 1/math.sqrt(math.pi))
            result = crps_integral(lambda x: ndtr((x-mu)/sigma), lambda x: ndtr((mu-x)/sigma),
                                   outcome, -21., 23.)
            self.assertTrue(result.converged)
            self.assertAlmostEqual(result.value, expected, delta=2e-6)

    def test_pit_is_uniform_for_seeded_lognormal_simulation(self):
        model = flat_distribution()
        prices = 100 * np.exp(-.02 + .2 * np.random.default_rng(1729).normal(size=20000))
        pit, _ = model.probabilities(prices)
        self.assertAlmostEqual(float(pit.mean()), .5, delta=.008)
        self.assertAlmostEqual(float(pit.var()), 1/12, delta=.003)
        self.assertTrue(np.all(np.abs(np.histogram(pit, bins=np.linspace(0, 1, 11))[0] - 2000) < 180))

    def test_crps_matches_lognormal_closed_form_in_narrow_distributions_and_far_tails(self):
        for variance in (1e-10, 1e-8, 1e-6, .04):
            sigma = math.sqrt(variance)
            for forward in (100., 1e6):
                mu = math.log(forward) - variance / 2
                for z in (-10., -2., 0., 2., 10.):
                    observed = math.exp(mu + sigma*z)
                    expected = observed * (2*ndtr(z)-1) - 2*forward * (ndtr(z-sigma)+ndtr(sigma/math.sqrt(2))-1)
                    result = crps_integral(lambda p: ndtr((np.log(p)-mu)/sigma),
                                           lambda p: ndtr((mu-np.log(p))/sigma), observed,
                                           forward*math.exp(-32),forward*math.exp(32),log_price=True)
                    with self.subTest(variance=variance,forward=forward,z=z):
                        self.assertTrue(result.converged)
                        self.assertAlmostEqual(result.value,expected,
                                               delta=max(2e-6,2e-7*abs(expected)))

    def test_log_score_uses_log_return_density_and_is_invariant_to_usd_scale(self):
        frozen = flat_distribution()
        settings = replace(ScoreSettings(), crps_absolute_tolerance_usd=1e-9)
        first = score_distribution(frozen, 110., EXPIRY, settings)
        k = math.log(1.1)
        expected = .5 * math.log(2 * math.pi * .04) + (k + .02) ** 2 / (.08)
        self.assertAlmostEqual(first.metrics['negative_log_score_log_return'], expected, places=12)
        scaled = FrozenCDF.for_model(replace(frozen.model, forward_usd=10000.))
        second = score_distribution(scaled, 11000., EXPIRY, settings)
        self.assertAlmostEqual(first.metrics['negative_log_score_log_return'], second.metrics['negative_log_score_log_return'], places=12)
        self.assertAlmostEqual(second.metrics['crps_usd'] / first.metrics['crps_usd'], 100, places=6)

    def test_narrow_normal_tail_outcome_does_not_false_converge(self):
        mu, sigma = 100., .001
        for z in (-10., 10.):
            observed = mu + z*sigma
            expected = sigma * (z*(2*ndtr(z)-1)+2*math.exp(-z*z/2)/math.sqrt(2*math.pi)-1/math.sqrt(math.pi))
            result = crps_integral(lambda p:ndtr((p-mu)/sigma),lambda p:ndtr((mu-p)/sigma),
                                   observed,-900.,1100.)
            self.assertTrue(result.converged)
            self.assertAlmostEqual(result.value,expected,delta=1e-6)

    def test_skewed_log_density_matches_cdf_derivative_in_log_price(self):
        model = ImpliedCDF(SVIParams(.02,.15,-.35,.03,.25),100.,.5,-.3,.3)
        frozen = FrozenCDF.for_model(model)
        for k in (-.5, 0., .5):
            h = 1e-5
            derivative = (float(model.probabilities(k+h)[0]) - float(model.probabilities(k-h)[0])) / (2*h)
            actual, status = frozen.log_return_density(100 * math.exp(k))
            self.assertEqual(status, 'ok')
            self.assertAlmostEqual(math.exp(actual), derivative, delta=2e-8)

    def test_skewed_central_crps_resolves_and_matches_independent_quadrature(self):
        for params in (SVIParams(.02,.15,-.35,.03,.25),SVIParams(.002,.05,-.6,.02,.25)):
            model=ImpliedCDF(params,100.,.5,-.3,.3)
            frozen=FrozenCDF.for_model(model)
            score=score_distribution(frozen,100.,EXPIRY)
            self.assertEqual(score.status,'ok')
            diag=model.to_record()['diagnostics']
            left=quad(lambda k:float(model.probabilities(k)[0])**2*100*math.exp(k),
                      diag['grid_k_min'],0.,epsabs=1e-9)[0]
            right=quad(lambda k:float(model.probabilities(k)[1])**2*100*math.exp(k),
                       0.,diag['grid_k_max'],epsabs=1e-9)[0]
            self.assertAlmostEqual(score.metrics['crps_usd'],left+right,delta=1e-6)

    def test_tail_exceedances_keep_raw_pit_and_infinite_log_loss(self):
        frozen = flat_distribution()
        for observed, side, pit in ((frozen.lower_usd / 2, 'left', 0.), (frozen.upper_usd * 2, 'right', 1.)):
            score = score_distribution(frozen, observed, EXPIRY)
            self.assertEqual(score.metrics['pit'], pit)
            self.assertEqual(score.diagnostics['tail_exceedance'], side)
            self.assertTrue(score.diagnostics['pit_transform_clipped'])
            self.assertTrue(score.diagnostics['negative_log_score_is_infinite'])
            self.assertIsNone(score.metrics['negative_log_score_log_return'])
            self.assertEqual(score.diagnostics['log_score_status'], 'zero_density')
            self.assertGreaterEqual(score.metrics['crps_usd'], 0)
            json.dumps(asdict(score), allow_nan=False)

    def test_exact_boundary_atoms_are_distinct_from_interior_and_outside(self):
        frozen = flat_distribution()
        for boundary, interior, exterior in ((frozen.lower_usd, math.inf, 0.), (frozen.upper_usd, 0., math.inf)):
            self.assertEqual(frozen.log_return_density(boundary)[1], 'support_atom')
            self.assertEqual(frozen.log_return_density(math.nextafter(boundary, interior))[1], 'ok')
            self.assertEqual(frozen.log_return_density(math.nextafter(boundary, exterior))[1], 'zero_density')

    def test_central_intervals_and_nonpositive_density_are_explicit(self):
        frozen = flat_distribution()
        score = score_distribution(frozen, 100., EXPIRY)
        self.assertEqual([score.metrics[f'coverage_{level}'] for level in (50,80,90)], [1.,1.,1.])
        intervals = score.diagnostics['intervals']
        self.assertLess(intervals['90']['lower_usd'], intervals['50']['lower_usd'])
        self.assertGreater(intervals['90']['upper_usd'], intervals['50']['upper_usd'])
        with patch('implied_calibration.scoring.butterfly_g', return_value=np.asarray(-1.)):
            invalid = score_distribution(frozen, 100., EXPIRY)
        self.assertEqual(invalid.status, 'invalid_density')
        self.assertEqual(invalid.diagnostics['log_score_status'], 'nonpositive_density')

    def test_expiry_class_precedence_and_time_validation(self):
        for value, expected in [('2024-02-29','daily'), ('2026-12-04','weekly'),
                                ('2026-01-30','monthly'), ('2026-03-27','quarterly'),
                                ('2027-12-31','quarterly')]:
            self.assertEqual(expiry_class(int(datetime.fromisoformat(value+'T08:00:00+00:00').timestamp()*1000)), expected)
        with self.assertRaises(ValueError):
            expiry_class(EXPIRY-1)


