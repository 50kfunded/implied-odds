"""Independent scale arithmetic, completed-label availability and paired reporting."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from implied_calibration.exploratory_extensions import analyse_extensions, load_extension_spec, rolling_extensions
from implied_calibration.incremental import VarianceCase, fit_forecasts
from implied_calibration.incremental_report import headline_figure, write_incremental_report
from implied_calibration.research_plan import load_plan
from implied_calibration.variance_study import DAY_MS, bootstrap_means, qlike


ROOT = Path(__file__).resolve().parents[1]


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(ROOT/'protocol/incremental-variance-v1.json')
        self.spec = load_extension_spec(ROOT/'protocol/exploratory-dvol-v1.json')
        rng = np.random.default_rng(738)
        self.cases = []
        for i in range(510):
            x = tuple(rng.uniform(.02,.3,4))
            self.cases.append(VarianceCase(i*DAY_MS,i*DAY_MS,(i+30)*DAY_MS,x,2*x[3]))
        self.record = self.record_at(420)

    def record_at(self, day):
        case = self.cases[day]
        fit = fit_forecasts(self.cases,case.feature_cutoff_ms,case.features,self.plan)
        return {'origin_ms':case.feature_cutoff_ms,'feature_cutoff_ms':case.feature_cutoff_ms,'target_end_ms':case.target_end_ms,
                'actual':case.actual,**fit}

    def test_scale_uses_mean_of_individual_ratios_and_exact_shared_rows(self):
        result = rolling_extensions(self.cases,self.record,365)
        self.assertAlmostEqual(result['scale_factor'],2.)
        self.assertAlmostEqual(result['forecasts']['dvol_rescaled'],2*self.record['forecasts']['dvol'])
        self.assertEqual(result['training']['first_feature_cutoff_ms'],26*DAY_MS)
        self.assertEqual(result['training']['last_feature_cutoff_ms'],390*DAY_MS)
        self.assertEqual(result['training']['latest_target_end_ms'],420*DAY_MS)
        varied = [replace(r,actual=r.actual*(1+r.features[3])) for r in self.cases]
        fitted = rolling_extensions(varied,self.record,365)
        expected = np.mean([r.actual/r.features[3] for r in varied[26:391]])
        wrong = np.mean([r.actual for r in varied[26:391]])/np.mean([r.features[3] for r in varied[26:391]])
        self.assertAlmostEqual(fitted['scale_factor'],expected)
        self.assertGreater(abs(expected-wrong),.001)

    def test_unfinished_current_and_discarded_old_labels_cannot_change_forecasts(self):
        before = rolling_extensions(self.cases,self.record,365)
        changed = [replace(r,actual=99999.) if i < 26 or r.target_end_ms > 420*DAY_MS else r
                   for i,r in enumerate(self.cases)]
        record = {**self.record,'actual':99999.}
        self.assertEqual(before,rolling_extensions(changed,record,365))
        changed[390] = replace(changed[390],actual=3*changed[390].features[3])
        self.assertAlmostEqual(rolling_extensions(changed,record,365)['scale_factor'],2+1/365)

    def test_equal_blend_is_in_variance_units_without_fitted_weight(self):
        record = deepcopy(self.record)
        record['forecasts'].update(har=.04,dvol=.16)
        result = rolling_extensions(self.cases,record,365)
        self.assertAlmostEqual(result['forecasts']['har_dvol_equal'],.10)
        self.assertNotAlmostEqual(result['forecasts']['har_dvol_equal'],((.2+.4)/2)**2)

    def test_training_mismatch_missing_rows_and_invalid_scale_inputs_fail(self):
        record = deepcopy(self.record)
        record['training']['first_feature_cutoff_ms'] += DAY_MS
        with self.assertRaisesRegex(ValueError,'differ'):
            rolling_extensions(self.cases,record,365)
        with self.assertRaisesRegex(ValueError,'full completed'):
            rolling_extensions(self.cases[100:],self.record,365)
        for actual,dvol in [(float('nan'),.1),(0.,.1),(.1,0.),(.1,float('inf'))]:
            cases = self.cases.copy()
            cases[390] = replace(cases[390],actual=actual,features=(.1,.1,.1,dvol))
            with self.assertRaisesRegex(ValueError,'positive finite'):
                rolling_extensions(cases,self.record,365)

    def test_analytic_full_sample_scale_check_is_distinct_from_forecasts(self):
        actual = np.array([.03,.09,.11,.4])
        forecast = np.array([.05,.1,.2,.3])
        scale = float(np.mean(actual/forecast))
        loss = lambda c: np.mean([qlike(a,c*f) for a,f in zip(actual,forecast)])
        self.assertLess(loss(scale),loss(scale*.9))
        self.assertLess(loss(scale),loss(scale*1.1))
        self.assertAlmostEqual(loss(1)-loss(scale),scale-1-np.log(scale))

    def test_extension_summary_preserves_inputs_and_joint_bootstrap(self):
        records = [self.record_at(i) for i in range(420,500)]
        before = deepcopy(records)
        result = analyse_extensions(self.cases,records,{**self.plan,'bootstrap_resamples':2000},self.spec)
        self.assertEqual(records,before)
        self.assertEqual(result['n'],80)
        rows = result['records']
        values = np.array([[r['losses']['dvol_rescaled'],r['losses']['har_dvol']] for r in rows])
        draws = bootstrap_means(values,np.array([r['origin_ms'] for r in rows]),spacing_ms=DAY_MS,
                                block_rows=60,resamples=2000,seed=self.plan['bootstrap_seed'])
        expected = np.quantile(draws[:,0]-draws[:,1],[.025,.975])
        np.testing.assert_allclose(result['summary']['metrics']['qlike_dvol_rescaled_minus_har_dvol']['confidence_interval_95'],expected,atol=1e-14)
        self.assertFalse(result['formal_claim_eligible'])
        self.assertEqual(set(result['block_sensitivities']),{'30','90','180','365'})
        self.assertEqual(sum(v['n'] for v in result['annual'].values()),80)
        self.assertEqual(result['in_sample_scale_check']['dvol']['role'],'in_sample_algebraic_scale_check_not_a_forecast')

    def test_published_spec_rejects_tuned_weight_or_window(self):
        import json
        for field in ('weight','window'):
            spec = deepcopy(self.spec)
            if field=='weight':
                spec['har_dvol_equal']['har_weight'] = .6
            else:
                spec['training_completed_origins'] = 180
            with TemporaryDirectory() as folder:
                path = Path(folder)/'spec.json'
                path.write_text(json.dumps(spec),encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_extension_spec(path)

    def test_report_writer_keeps_original_csv_and_serializes_separate_extensions(self):
        import csv
        import json
        from types import SimpleNamespace
        records = [self.record_at(i) for i in range(420,500)]
        for row in records:
            row['losses'] = {name:qlike(row['actual'],value) for name,value in row['forecasts'].items()}
        before = deepcopy(records)
        plan = {**self.plan,'bootstrap_resamples':2000}
        fixture = {'records':records,'n':len(records),'summary':{}}
        historical = {'origin_start_ms':0,'origin_end_ms':509*DAY_MS,'alignment_check_days_ms':[]}
        with TemporaryDirectory() as folder, \
             patch('implied_calibration.incremental_report.load_study',return_value=historical), \
             patch('implied_calibration.incremental_report.read_study',return_value=SimpleNamespace(prices={},dvol={})), \
             patch('implied_calibration.incremental_report.load_plan',return_value=plan), \
             patch('implied_calibration.incremental_report.analyse_incremental',return_value=fixture), \
             patch('implied_calibration.incremental_report.build_cases',return_value=self.cases), \
             patch('implied_calibration.incremental_report.alignment_checks',return_value={}), \
             patch('implied_calibration.incremental_report.headline_figure'):
            output = Path(folder)
            write_incremental_report(ROOT,output,simulations=False)
            full = json.loads((output/'report.json').read_text(encoding='utf-8'))
            summary = json.loads((output/'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(full['records'],before)
            self.assertEqual(len(full['exploratory_extensions']['records']),80)
            self.assertNotIn('records',summary)
            self.assertNotIn('records',summary['exploratory_extensions'])
            with (output/'forecasts.csv').open(newline='',encoding='utf-8') as handle:
                original = list(csv.DictReader(handle))
            with (output/'extensions-forecasts.csv').open(newline='',encoding='utf-8') as handle:
                extensions = list(csv.DictReader(handle))
            self.assertEqual(len(original),80)
            self.assertEqual(len(extensions),80)
            self.assertNotIn('forecast_dvol_rescaled',original[0])
            self.assertAlmostEqual(float(original[0]['forecast_dvol']),before[0]['forecasts']['dvol'])
            self.assertEqual(float(extensions[0]['scale_factor']),2.)
            self.assertAlmostEqual(float(extensions[0]['forecast_dvol_rescaled']),2*float(original[0]['forecast_dvol']))
            self.assertEqual(records,before)

    def test_headline_figure_uses_added_means_and_paired_intervals(self):
        import matplotlib.pyplot as plt
        records = [self.record_at(i) for i in range(420,500)]
        for row in records:
            row['losses'] = {name:qlike(row['actual'],value) for name,value in row['forecasts'].items()}
        extra = analyse_extensions(self.cases,records,{**self.plan,'bootstrap_resamples':2000},self.spec)
        metrics = extra['summary']['metrics']
        reverse = metrics['qlike_har_minus_har_dvol']
        primary = {**reverse,'mean':-reverse['mean'],
                   'confidence_interval_95':[-x for x in reverse['confidence_interval_95'][::-1]]}
        result = {'n':80,'records':records,'exploratory_extensions':extra,
                  'summary':{'metrics':{'qlike_har_dvol_minus_har':primary,
                             'qlike_dvol_minus_har_dvol':metrics['qlike_dvol_minus_har_dvol']}}}
        with TemporaryDirectory() as folder, patch('implied_calibration.incremental_report.plt.close'):
            headline_figure(result,Path(folder))
            figure = plt.gcf()
            axes = figure.axes
            expected = [np.mean([r['losses'][name] for r in extra['records']]) for name in self.spec['table_models']]
            np.testing.assert_allclose([bar.get_height() for bar in axes[0].patches],expected)
            self.assertEqual(axes[0].get_xticklabels()[2].get_text(),'50/50\nblend')
            self.assertEqual(len(axes[1].get_yticklabels()),4)
            np.testing.assert_allclose(axes[1].lines[4].get_xdata(),metrics['qlike_dvol_rescaled_minus_har_dvol']['confidence_interval_95'])
            np.testing.assert_allclose(axes[2].lines[0].get_ydata(),np.cumsum([r['losses']['har_dvol']-r['losses']['har'] for r in records]))
            self.assertTrue((Path(folder)/'figure-1.pdf').is_file())
        plt.close(figure)
