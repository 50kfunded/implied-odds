"""Completed-label fitting, decision units and research-lock checks."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from implied_calibration.incremental import VarianceCase, analyse_incremental, fit_forecasts, risk_budget, regression_diagnostic
from implied_calibration.incremental_report import headline_figure
from implied_calibration.inference import paired_inference, berkowitz_hac
from implied_calibration.inference_study import bootstrap_interval, loss_process, wilson
from implied_calibration.research_plan import load_plan, stopping_choices
from implied_calibration.variance_study import DAY_MS, _metric_summary, bootstrap_means


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

    def test_raw_dvol_comparison_preserves_old_metrics_and_plots_paired_losses(self):
        rng = np.random.default_rng(17)
        cases = []
        base = int(datetime(2023,12,1,tzinfo=timezone.utc).timestamp()*1000)-420*DAY_MS
        for i in range(540):
            x = tuple(rng.uniform(.02,.3,4))
            cases.append(VarianceCase(base+i*DAY_MS,base+i*DAY_MS,base+(i+30)*DAY_MS,x,
                                     .01+.2*x[0]+.3*x[1]+.4*x[2]+.5*x[3]+rng.normal(0,.005)))
        plan = {**self.plan, 'historical_origin_start_ms':base+420*DAY_MS,
                'historical_origin_end_ms':base+499*DAY_MS, 'bootstrap_resamples':2000}
        with patch('implied_calibration.incremental.build_cases',return_value=cases):
            result = analyse_incremental({}, {}, {'origin_start_ms':base,'origin_end_ms':base+539*DAY_MS}, plan)
        records = result['records']
        times = np.array([r['origin_ms'] for r in records])
        raw = np.array([r['losses']['dvol'] for r in records])
        augmented = np.array([r['losses']['har_dvol'] for r in records])
        difference = raw-augmented
        metric = result['summary']['metrics']['qlike_dvol_minus_har_dvol']
        self.assertFalse(result['formal_claim_eligible'])
        self.assertAlmostEqual(metric['mean'],float(difference.mean()))
        samples = bootstrap_means(np.column_stack((raw,augmented)),times,spacing_ms=DAY_MS,
                                  block_rows=60,resamples=2000,seed=plan['bootstrap_seed'])
        expected_interval = np.quantile(samples[:,0]-samples[:,1],[.025,.975])
        np.testing.assert_allclose(metric['confidence_interval_95'],expected_interval,atol=1e-14)
        old_names = ['qlike_'+name for name in plan['models']]+['qlike_har_dvol_minus_har',
                    'qlike_dvol_minus_har','risk_budget_har_dvol_minus_har','qlike_dvol_minus_har_dvol']
        old_values = np.array([[*[r['losses'][name] for name in plan['models']],
            r['losses']['har_dvol']-r['losses']['har'],r['losses']['dvol']-r['losses']['har'],
            r['decision']['har_dvol']['squared_variance_budget_error']-
            r['decision']['har']['squared_variance_budget_error'],
            r['losses']['dvol']-r['losses']['har_dvol']] for r in records])
        for block in (60,*plan['sensitivity_block_days']):
            expected = _metric_summary(old_values,times,old_names,plan,block_days=block)
            observed = result['summary'] if block==60 else result['block_sensitivities'][str(block)]
            for name in old_names:
                self.assertEqual(observed['metrics'][name],expected['metrics'][name])
        for name in plan['models']:
            expected_ratio = np.mean([r['actual']/r['forecasts'][name] for r in records])
            self.assertAlmostEqual(result['summary']['metrics']['realised_to_forecast_'+name]['mean'],expected_ratio)
        years = np.array([datetime.fromtimestamp(r['origin_ms']/1000,timezone.utc).year for r in records])
        yearly = result['concentration']['dvol_minus_har_dvol']['leave_one_year_out']
        self.assertEqual(set(yearly),{'2023','2024'})
        for year,entry in yearly.items():
            remaining = difference[years!=int(year)]
            self.assertEqual(entry['n'],len(remaining))
            self.assertAlmostEqual(entry['mean'],float(remaining.mean()))
        old_dvol_sum = sum(q['sum'] for q in result['concentration']['dvol']['quarters'].values())
        self.assertAlmostEqual(old_dvol_sum,sum(r['losses']['dvol']-r['losses']['trailing'] for r in records))
        with TemporaryDirectory() as folder, patch('implied_calibration.incremental_report.plt.close'):
            headline_figure(result,Path(folder))
            import matplotlib.pyplot as plt
            figure = plt.gcf()
            line = next(line for line in figure.axes[2].lines
                        if line.get_label()=='Raw DVOL minus (HAR + DVOL)')
            np.testing.assert_allclose(line.get_ydata(),np.cumsum(difference),atol=1e-14)
            self.assertAlmostEqual(line.get_ydata()[-1],len(records)*metric['mean'])
            interval_line = next(line for line in figure.axes[1].lines
                                 if line.get_label()=='Raw DVOL minus (HAR + DVOL) interval')
            np.testing.assert_allclose(interval_line.get_xdata(),metric['confidence_interval_95'])
        plt.close(figure)

    def test_constant_paired_difference_has_degenerate_interval_and_gaps_withhold_it(self):
        raw = np.linspace(.1,1.,80)
        values = np.column_stack((raw,raw+.025,np.full(80,-.025)))
        names = ['raw','har_dvol','qlike_dvol_minus_har_dvol']
        times = np.arange(80)*DAY_MS
        plan = {**self.plan,'bootstrap_resamples':2000}
        metric = _metric_summary(values,times,names,plan)['metrics'][names[-1]]
        np.testing.assert_allclose(metric['confidence_interval_95'],[-.025,-.025],atol=1e-14)
        times[40:] += DAY_MS
        metric = _metric_summary(values,times,names,plan)['metrics'][names[-1]]
        self.assertIsNone(metric['confidence_interval_95'])
        self.assertIsNone(metric['hac_standard_error'])
        self.assertFalse(metric['ess_gate_passed'])
