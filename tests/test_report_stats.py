"""Report statistics, tail conventions and prefix-specific uncertainty checks."""

from dataclasses import replace

import numpy as np
from scipy.stats import binom

from implied_calibration.inference import paired_inference
from implied_calibration.report_stats import (ReportObservation, binary_summary, cumulative_comparison,
                                             histogram, report_statistics, tail_events)
from test_phase1 import OfflineTest
from test_phase6 import DAY, locked_fixture
from test_scoring import flat_distribution


def observation(index, model='svi', difference=0.):
    return ReportObservation(str(index),(index+1)*DAY,DAY,'daily',model,
        {'crps_usd':5.+difference,'pit':.5,'coverage_50':1.,'coverage_80':1.,'coverage_90':1.,
         'negative_log_score_log_return':1.},
        {'negative_log_score_is_infinite':False,'log_score_status':'ok'},
        {f'{side}_{p:g}':0. for side in ('left','right') for p in (.01,.05,.1,.25)})


class ReportStatisticsTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.choices=locked_fixture().protocol.content['evaluation']

    def test_histogram_counts_boundaries_and_reference_match_known_values(self):
        pits=[0.,.05,.15,.95,1.]
        result=histogram(pits,self.choices,1e-6)
        self.assertEqual(sum(result['counts']),5)
        self.assertEqual((result['counts'][0],result['counts'][-1]),(2,2))
        self.assertEqual(result['reference_count_range_95'],binom.ppf([.025,.975],5,.1).tolist())
        self.assertIn('not dependence-aware',result['reference_label'])
        self.assertEqual(result['status'],'insufficient data')
        self.assertIsNone(result['diagnostic']['p_value'])

    def test_binary_intervals_are_gated_and_delayed_dependence_reduces_ess(self):
        result=binary_summary([0.]*100,list(range(100)),DAY,self.choices)
        self.assertEqual(result['status'],'insufficient data')
        self.assertIsNone(result['confidence_interval_95'])
        values=np.random.default_rng(77).integers(0,2,240).astype(float).tolist()
        result=binary_summary(values,[i*DAY for i in range(240)],DAY,self.choices)
        self.assertEqual(result['status'],'descriptive')
        self.assertIsNotNone(result['confidence_interval_95'])

    def test_tail_events_use_frozen_quantiles_with_inclusive_boundaries(self):
        model=flat_distribution()
        for p in (.01,.05,.1,.25):
            lower=model.quantile(p)
            upper=model.quantile(1-p)
            self.assertEqual(tail_events(model,lower)[f'left_{p:g}'],1.)
            self.assertEqual(tail_events(model,upper)[f'right_{p:g}'],1.)
            self.assertEqual(tail_events(model,np.nextafter(lower,np.inf))[f'left_{p:g}'],0.)

    def test_cumulative_bands_match_each_prefix_and_never_use_future_pairs(self):
        rng=np.random.default_rng(44)
        differences=rng.normal(-.2,.6,200)
        rows=[r for i,d in enumerate(differences) for r in (observation(i,difference=float(d)),observation(i,'same_snapshot_atm_lognormal'))]
        result=cumulative_comparison(rows,self.choices,max_points=12)
        self.assertEqual(len(result['points']),12)
        for point in result['points']:
            n=point['n_pairs']
            expected=paired_inference([(i+1)*DAY for i in range(n)],5+differences[:n],np.full(n,5.),DAY,primary=True)
            self.assertEqual(point['status'],expected['status'])
            self.assertEqual(point['confidence_interval_95'],expected['bootstrap']['confidence_interval_95'] if expected['bootstrap'] else None)
        self.assertEqual(result['points'][0]['status'],'insufficient data')
        truncated=cumulative_comparison(rows[:160],self.choices,max_points=12)
        self.assertEqual(truncated['primary']['n_pairs'],80)
        self.assertIn('not simultaneous',result['interval_label'])

    def test_unpaired_scores_are_counted_and_duplicate_expiries_rejected(self):
        rows=[observation(0),observation(1),observation(1,'same_snapshot_atm_lognormal')]
        result=cumulative_comparison(rows,self.choices)
        self.assertEqual(result['unpaired_forecasts'],1)
        self.assertEqual(result['primary']['n_pairs'],1)
        with self.assertRaises(ValueError):
            cumulative_comparison(rows+[rows[-1]],self.choices)

    def test_coverage_preserves_infinite_and_undefined_log_losses(self):
        row=observation(0)
        infinite=replace(row,metrics={**row.metrics,'negative_log_score_log_return':None},
            diagnostics={'negative_log_score_is_infinite':True,'log_score_status':'zero_density'})
        atom=replace(observation(1),metrics={**row.metrics,'negative_log_score_log_return':None},
            diagnostics={'negative_log_score_is_infinite':False,'log_score_status':'support_atom'})
        result=report_statistics([infinite,atom],self.choices,1e-6)
        for item in result['coverage']:
            self.assertEqual((item['infinite_log_losses'],item['undefined_continuous_log_losses']),(1,1))
            self.assertIsNone(item['mean_log_loss'])

    def test_empty_report_gates_every_statistical_panel(self):
        result=report_statistics([],self.choices,1e-6)
        self.assertEqual(result['status'],'insufficient data')
        self.assertEqual(result['pit']['status'],'insufficient data')
        self.assertTrue(all(r['status']=='insufficient data' for r in result['reliability']))
        self.assertEqual(result['crps']['primary']['conclusion'],'insufficient data')

    def test_invalid_pit_and_duplicate_scores_are_rejected(self):
        with self.assertRaises(ValueError):
            histogram([1.1],self.choices,1e-6)
        with self.assertRaises(ValueError):
            report_statistics([observation(0),observation(0)],self.choices,1e-6)

    def test_terminal_expiry_cohort_applies_to_every_panel(self):
        rows = [r for i in range(2) for r in (observation(i),observation(i,'same_snapshot_atm_lognormal'))]
        result = report_statistics(rows,{**self.choices,'expiry_cutoff_ms':DAY},1e-6)
        self.assertEqual(result['crps']['primary']['n_pairs'],1)
        self.assertEqual(result['pit']['n'],1)
        self.assertTrue(all(item['n'] == 1 for item in result['coverage']))
