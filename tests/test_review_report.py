"""Matched focal uncertainty and candidate coverage remain explicit."""

import unittest
from unittest.mock import patch

import numpy as np

from implied_calibration.inference_reference import empirical_power, dm_hln, stationary_samples
from implied_calibration.review_models import load_review_spec
from implied_calibration.review_report import benchmark_summary, model_confidence_set, phase_summary, validate_v1_rebuild
from implied_calibration.inference import effective_sample_size
from implied_calibration.variance_study import bootstrap_means
from test_review_models import ROOT


class ReviewReportTests(unittest.TestCase):
    def test_v1_guard_reports_only_tiny_roundoff_and_rejects_material_or_structural_drift(self):
        original = {'n': 1357, 'hash': 'fixed', 'means': [.12548, -.01424]}
        tiny = {**original, 'means': [.12548+1e-15, -.01424]}
        result = validate_v1_rebuild(original, tiny)
        self.assertFalse(result['exact_equal'])
        self.assertEqual(result['floating_roundoff_differences'][0]['path'], '$.means[0]')
        for altered in ({**original, 'n': 1356}, {**original, 'hash': 'changed'},
                        {**original, 'means': [.12549, -.01424]}, {**original, 'extra': 1}):
            with self.assertRaises(ValueError):
                validate_v1_rebuild(original, altered)

    def test_focal_comparison_uses_joint_draws_and_the_matching_qlike_baseline(self):
        rng = np.random.default_rng(8)
        losses = np.abs(rng.normal(size=(120, 3)))
        models = ['qlike_har_dvol', 'qlike_har', 'har_dvol_365']
        spec = {'models': models, 'paired_reference': 'har_dvol_365'}
        plan = {'expected_block_days': 60, 'hac_lags_days': 59, 'bootstrap_resamples': 2000,
                'bootstrap_seed': 271828, 'minimum_effective_sample_size': 60}
        times = np.arange(120)*86400000
        rows = [{'origin_ms': int(stamp), 'losses': dict(zip(models, loss))} for stamp, loss in zip(times, losses)]
        summary = benchmark_summary(rows, spec, plan)
        focal = summary['metrics']['focus_qlike_har_dvol_minus_qlike_har']
        draws = bootstrap_means(losses[:, :2], times, spacing_ms=86400000, block_rows=60,
                                resamples=2000, seed=271828)
        np.testing.assert_allclose(focal['confidence_interval_95'], np.quantile(draws[:, 0]-draws[:, 1], [.025, .975]), atol=1e-14)
        self.assertAlmostEqual(focal['mean'], (losses[:, 0]-losses[:, 1]).mean())
        self.assertAlmostEqual(focal['effective_sample_size'], effective_sample_size(losses[:, 0]-losses[:, 1], 59))
        gapped = rows[:60]+[{**row, 'origin_ms': row['origin_ms']+86400000} for row in rows[60:]]
        self.assertIsNone(benchmark_summary(gapped, spec, plan)['metrics']['focus_qlike_har_dvol_minus_qlike_har']['confidence_interval_95'])
        self.assertEqual(benchmark_summary([], spec, plan)['n'], 0)

    def test_power_vectorised_dm_matches_direct_reference_including_degeneracy(self):
        spec, _ = load_review_spec(ROOT)
        config = {**spec['power'], 'sample_sizes': [365], 'block_days': [60],
                  'replications': 5, 'effect_sizes': [0., -.1]}
        x = np.random.default_rng(321).normal(size=800)
        rng = np.random.default_rng(config['seed']+60*10000+365)
        draws = stationary_samples(x-x.mean(), 365, 5, 60, rng)
        draws[0] = 0.
        with patch('implied_calibration.inference_reference.stationary_samples', return_value=draws):
            result = empirical_power(x, {**spec, 'power': config})
        for row in result['rows']:
            ps = [dm_hln(draw+row['population_shift'], lags=59)['p_value'] for draw in draws]
            expected = sum(p is not None and p < .05 for p in ps)/5
            self.assertEqual(row['methods']['dm_hln_lag59']['rejection_rate'], expected)

    def test_mcs_is_withheld_across_calendar_gaps(self):
        spec, _ = load_review_spec(ROOT)
        rows = [{'origin_ms': i*86400000, 'losses': {name: .1 for name in spec['models']}} for i in range(70)]
        rows[35:] = [{**row, 'origin_ms': row['origin_ms']+86400000} for row in rows[35:]]
        self.assertEqual(model_confidence_set(rows, spec)['status'], 'withheld')

    def test_phase_count_is_a_dependent_display_with_all_thirty_phases(self):
        rows = [{'origin_ms': i*86400000, 'losses': {'dvol': .1, 'har': .2, 'har_dvol': .15}} for i in range(120)]
        result = phase_summary(rows)
        self.assertEqual(result['comparisons']['dvol_minus_har']['n_negative'], 30)
        self.assertEqual(result['comparisons']['dvol_minus_har']['phase_counts'], [4]*30)
        self.assertIn('dependent', result['scope'])
