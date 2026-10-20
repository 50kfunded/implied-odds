"""I challenge completed-label fitting, decision units and research locks."""

from dataclasses import replace
from pathlib import Path
import unittest

import numpy as np

from implied_calibration.incremental import VarianceCase, fit_forecasts, risk_budget, regression_diagnostic
from implied_calibration.inference import paired_inference, berkowitz_hac
from implied_calibration.inference_study import bootstrap_interval, loss_process, wilson
from implied_calibration.research_plan import load_plan, stopping_choices
from implied_calibration.variance_study import DAY_MS


ROOT = Path(__file__).resolve().parents[1]


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(ROOT/'protocol/incremental-variance-v1.json')
        rng = np.random.default_rng(6)
        self.cases = []
        for i in range(450):
            x = tuple(rng.uniform(.02,.3,4))
            y = .01+.2*x[0]+.3*x[1]+.4*x[2]+.5*x[3]
            self.cases.append(VarianceCase(i*DAY_MS,i*DAY_MS,(i+30)*DAY_MS,x,y))

    def test_augmented_fit_recovers_known_linear_relation_and_identical_training(self):
        result = fit_forecasts(self.cases,420*DAY_MS,(.1,.2,.3,.4),self.plan)
        self.assertAlmostEqual(result['forecasts']['har_dvol'],.41,places=12)
        self.assertEqual(result['training']['n'],365)
        self.assertEqual(result['training']['latest_target_end_ms'],420*DAY_MS)
        self.assertTrue(result['training']['same_rows_for_both_models'])

    def test_mutating_unfinished_labels_cannot_change_forecast(self):
        cutoff = 420*DAY_MS
        before = fit_forecasts(self.cases,cutoff,(.1,.2,.3,.4),self.plan)
        changed = [replace(r,actual=999999.) if r.target_end_ms > cutoff else r for r in self.cases]
        self.assertEqual(before,fit_forecasts(changed,cutoff,(.1,.2,.3,.4),self.plan))
        self.assertIsNone(fit_forecasts(self.cases,393*DAY_MS,(.1,.2,.3,.4),self.plan))

    def test_floor_is_explicit_and_preserves_raw_prediction(self):
        changed = [replace(r,actual=1.-3*r.features[0]) for r in self.cases]
        result = fit_forecasts(changed,420*DAY_MS,(10.,.2,.3,.4),self.plan)
        self.assertLess(result['fits']['har']['raw_prediction'],0)
        self.assertTrue(result['fits']['har']['floor_hit'])
        self.assertEqual(result['forecasts']['har'],self.plan['forecast_floor_variance'])

    def test_risk_budget_is_a_variance_diagnostic_with_separate_fees(self):
        decision = risk_budget(.09,.16,self.plan)
        self.assertAlmostEqual(decision['weight'],.5)
        self.assertAlmostEqual(decision['realised_exposure_variance'],.0225)
        self.assertAlmostEqual(decision['squared_variance_budget_error'],(.0225-.04)**2)
        self.assertAlmostEqual(decision['entry_exit_fee_fraction']['20'],.002)

    def test_level_calibration_recovers_independent_analytic_line(self):
        x = np.arange(100,dtype=float)/100
        result = regression_diagnostic(2+3*x,np.column_stack((np.ones(100),x)),np.array([2.,3.]))
        np.testing.assert_allclose(result['coefficients'],[2.,3.],atol=1e-12)

    def test_bad_inference_arguments_fail_before_gate_and_timestamp_cast(self):
        for threshold in (float('nan'),float('inf'),0,-1):
            with self.assertRaises(ValueError):
                paired_inference([1,2,3],[1,2,3],[1,1,1],1,minimum_ess=threshold)
            with self.assertRaises(ValueError):
                berkowitz_hac([.2,.3],minimum_ess=threshold)
        with self.assertRaises(ValueError):
            paired_inference([1.1,2.2,3.3],[1,2,3],[1,1,1],1)

    def test_stopping_amendment_keeps_early_decisions_descriptive(self):
        amendment = load_plan(ROOT/'protocol/forward-inference-v1.json')
        choices = stopping_choices({},amendment['parent_protocol_hash'],amendment['parent_baseline_hash'],amendment['analysis_at_ms']-1)
        self.assertFalse(choices['formal_decision_allowed'])
        self.assertTrue(stopping_choices({},amendment['parent_protocol_hash'],amendment['parent_baseline_hash'],amendment['analysis_at_ms'])['formal_decision_allowed'])

    def test_geometric_segment_bootstrap_and_population_null(self):
        interval = bootstrap_interval(np.ones(31)*7,resamples=2000,block=60,rng=np.random.default_rng(4))
        np.testing.assert_allclose(interval,[7,7])
        x = loss_process(365,'overlap_30_plus_slow_365',np.random.default_rng(4))
        self.assertGreater(abs(float(x.mean())),.001)
        self.assertEqual(len(x),365)
        self.assertEqual(wilson(0,0),None)
        self.assertGreater(wilson(0,200)[1],0)
