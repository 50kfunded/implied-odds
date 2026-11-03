"""Synthetic targets test the ETH extension without live requests."""

from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from implied_calibration.historical_data import load_study
from implied_calibration.relative_variance import (PairedCase, RATIO_MODELS, correlation, evaluate_pairs,
    forecast_pair, linear_ratio_fit, load_relative_spec, marginal_decomposition, paired_cases, standalone_weights, summarise_pairs)
from implied_calibration.relative_report import independent_targets, render_note, source_audit, vintage_audit, write_relative_report
from implied_calibration.review_models import utc_ms
from implied_calibration.variance_study import DAY_MS, HOUR_MS


class RelativeVarianceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.spec = json.loads((self.root/'protocol/eth-relative-variance-v1.json').read_text())

    def cases(self, n=180):
        rng = np.random.default_rng(41)
        output = []
        for index in range(n):
            btc = np.exp(rng.normal(-1, .15, 4))
            ratios = np.exp(rng.normal(.5, .12, 4))
            eth = btc*ratios
            actual_btc = float(np.exp(rng.normal(-1, .12)))
            actual_eth = actual_btc*math.exp(.1+.4*math.log(ratios[2])+.6*math.log(ratios[3]))
            origin = (index+1000)*DAY_MS
            output.append(PairedCase(origin, origin, origin+30*DAY_MS,
                                     (tuple(btc), tuple(eth)), (actual_btc, actual_eth)))
        return output

    def test_declaration_is_committed_and_hashes_match(self):
        spec, proof = load_relative_spec(self.root)
        self.assertEqual(spec['primary_comparison'], 'ratio_har_dvol minus ratio_har')
        self.assertFalse(proof['independent_timestamp'])
        self.assertEqual(len(proof['declaration_commit']), 40)
        for asset in spec['assets']:
            config = load_study(self.root/spec['source_configs'][asset])
            self.assertEqual(config['dvol_currency'], asset)
            self.assertEqual(config['price_instrument'], asset+'-PERPETUAL')

    def test_published_relative_note_matches_computed_summary(self):
        summary = self.root/'report/eth-relative-results.json'
        if not summary.is_file():
            self.skipTest('The captured extension has not been published locally yet')
        result = json.loads(summary.read_text(encoding='utf-8'))
        self.assertEqual((self.root/'report/eth-relative-results.md').read_text(encoding='utf-8'), render_note(result))
        self.assertFalse(result['formal_claim_eligible'])
        self.assertEqual(result['declaration']['spec_sha256'],
                         load_relative_spec(self.root)[1]['spec_sha256'])

    def test_regression_recovers_known_relative_variance_relation(self):
        cases = self.cases()
        row = cases[120]
        fitted = forecast_pair(cases, row.origin_ms, 60)
        target = math.log(row.actual[1]/row.actual[0])
        self.assertAlmostEqual(fitted['ratio_forecasts']['ratio_har_dvol'], target, places=12)
        self.assertEqual(fitted['ratio_fits']['ratio_har_dvol']['rank'], 5)
        self.assertEqual(fitted['training_n'], 60)
        self.assertEqual(fitted['latest_training_target_end_ms'], row.origin_ms)
        self.assertEqual(fitted['last_training_origin_ms'], row.origin_ms-30*DAY_MS)

    def test_uncompleted_targets_and_future_features_cannot_change_forecast(self):
        cases = self.cases()
        cutoff = cases[120].origin_ms
        expected = forecast_pair(cases, cutoff, 60)
        changed = [replace(r, actual=(1e20, 1e-20)) if r.target_end_ms > cutoff else r for r in cases]
        changed = [replace(r, features=((1e12,)*4, (1e-12,)*4)) if r.origin_ms > cutoff else r for r in changed]
        self.assertEqual(forecast_pair(changed, cutoff, 60), expected)

    def test_completed_label_exact_boundary_is_admitted_and_one_millisecond_later_is_not(self):
        cases = self.cases()
        cutoff = cases[120].origin_ms
        expected = forecast_pair(cases, cutoff, 60)
        altered = [replace(r, target_end_ms=cutoff+1) if r.origin_ms == cutoff-30*DAY_MS else r for r in cases]
        result = forecast_pair(altered, cutoff, 60)
        self.assertEqual(result['last_training_origin_ms'], cutoff-31*DAY_MS)
        self.assertNotEqual(result['first_training_origin_ms'], expected['first_training_origin_ms'])

    def test_training_requires_both_assets_and_preserves_the_window_size(self):
        cases = self.cases()
        cutoff = cases[120].origin_ms
        altered = [replace(r, actual=(r.actual[0], None)) if r.origin_ms == cutoff-30*DAY_MS else r for r in cases]
        result = forecast_pair(altered, cutoff, 60)
        self.assertEqual(result['training_n'], 60)
        self.assertEqual(result['last_training_origin_ms'], cutoff-31*DAY_MS)
        with self.assertRaisesRegex(ValueError, 'Insufficient'):
            forecast_pair(cases[:80], cases[70].origin_ms, 60)

    def test_ratio_forecasts_ignore_a_common_multiplicative_variance_level(self):
        cases = self.cases()
        cutoff = cases[120].origin_ms
        expected = forecast_pair(cases, cutoff, 60)
        scaled = [replace(r, features=tuple(tuple(value*7 for value in feature) for feature in r.features),
                          actual=tuple(value*7 for value in r.actual)) for r in cases]
        actual = forecast_pair(scaled, cutoff, 60)
        for name in RATIO_MODELS:
            self.assertAlmostEqual(actual['ratio_forecasts'][name], expected['ratio_forecasts'][name], places=12)

    def test_calibration_uses_training_log_errors_and_raw_ratio_is_squared_dvol_ratio(self):
        cases = self.cases()
        cutoff = cases[120].origin_ms
        result = forecast_pair(cases, cutoff, 60)
        train = [r for r in cases if r.target_end_ms <= cutoff][-60:]
        offset = sum(math.log(r.actual[1]/r.actual[0])-math.log(r.features[1][3]/r.features[0][3]) for r in train)/60
        self.assertAlmostEqual(result['calibration_log_offset'], offset)
        self.assertAlmostEqual(result['ratio_forecasts']['calibrated_dvol_ratio'],
                               result['ratio_forecasts']['raw_dvol_ratio']+offset)
        current = cases[120]
        self.assertAlmostEqual(result['ratio_forecasts']['raw_dvol_ratio'],
                               2*math.log(math.sqrt(current.features[1][3])/math.sqrt(current.features[0][3])))

    def test_invalid_features_duplicate_origins_and_bad_shapes_fail(self):
        cases = self.cases()
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            forecast_pair(cases+[cases[0]], cases[120].origin_ms, 60)
        for value in (0., -1., float('nan'), float('inf')):
            bad = list(cases)
            bad[120] = replace(bad[120], features=((value,)*4, bad[120].features[1]))
            with self.assertRaisesRegex(ValueError, 'positive and finite'):
                forecast_pair(bad, bad[120].origin_ms, 60)
        with self.assertRaisesRegex(ValueError, 'Invalid direct'):
            linear_ratio_fit(np.ones((5, 4)), np.ones(5), np.ones(4))

    def test_standalone_variance_budget_loss_identity(self):
        realised_btc, realised_eth, forecast = .25, .9, .7
        btc, eth = standalone_weights(forecast)
        self.assertAlmostEqual(btc+eth, 1)
        imbalance = math.log(eth*eth*realised_eth/(btc*btc*realised_btc))
        target = math.log(realised_eth/realised_btc)
        self.assertAlmostEqual(imbalance*imbalance, (target-forecast)**2)
        self.assertEqual(standalone_weights(2000), (1., 0.))
        with self.assertRaises(ValueError):
            standalone_weights(float('nan'))

    def test_common_relative_error_decomposition_is_exact(self):
        _, _, rows, _ = self.small_evaluation()
        for row in rows:
            for result in marginal_decomposition(row).values():
                self.assertLess(result['identity_error'], 1e-14)
                self.assertAlmostEqual(result['common']+result['relative']/4, result['average_asset_squared_error'])

    def test_unbiased_variance_proxies_need_not_give_an_unbiased_log_ratio(self):
        # Independent two-point measurement errors have unit means but different log means.
        btc, eth = np.array([.9, 1.1]), np.array([.4, 1.6])
        observed_log_ratios = np.log(eth[:, None])-np.log(btc[None, :])
        self.assertEqual(float(btc.mean()), 1.)
        self.assertEqual(float(eth.mean()), 1.)
        self.assertNotEqual(float(observed_log_ratios.mean()), 0.)
        latent_ratio_forecast_error = float(np.mean(observed_log_ratios**2))
        proxy_optimal_error = float(np.mean((observed_log_ratios-observed_log_ratios.mean())**2))
        self.assertLess(proxy_optimal_error, latent_ratio_forecast_error)

    def small_evaluation(self):
        cases = self.cases(210)
        start, end = cases[120].origin_ms, cases[200].origin_ms
        spec = {**self.spec, 'evaluation_start': self.iso(start), 'evaluation_end': self.iso(end),
                'training_completed_labels': 60,
                'inference': {**self.spec['inference'], 'bootstrap_resamples': 2000, 'sensitivity_block_days': []}}
        rows, excluded = evaluate_pairs(cases, spec)
        return cases, spec, rows, excluded

    @staticmethod
    def iso(stamp):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(stamp/1000, timezone.utc).isoformat()

    def test_shared_bootstrap_dates_preserve_asset_difference_identity(self):
        _, spec, rows, excluded = self.small_evaluation()
        self.assertFalse(excluded)
        # Identical marginal loss increments must have exactly zero interaction uncertainty.
        for row in rows:
            row['marginal_losses']['ETH'] = dict(row['marginal_losses']['BTC'])
        first = summarise_pairs(rows, spec)
        second = summarise_pairs(rows, spec)
        self.assertEqual(first, second)
        metric = first['summary']['metrics']['ETH_minus_BTC_incremental_qlike']
        self.assertEqual(metric['mean'], 0.)
        self.assertEqual(metric['confidence_interval_95'], [0., 0.])
        self.assertFalse(first['formal_claim_eligible'])
        self.assertEqual(sum(r['n'] for r in first['phases']), len(rows))

    def test_missing_origin_is_counted_and_withholds_interval(self):
        cases, spec, _, _ = self.small_evaluation()
        missing = utc_ms(spec['evaluation_start'])+20*DAY_MS
        cases = [r for r in cases if r.origin_ms != missing]
        rows, excluded = evaluate_pairs(cases, spec)
        self.assertEqual(excluded, [{'origin_ms': missing, 'reason': 'missing_matched_features_or_targets'}])
        result = summarise_pairs(rows, spec)
        self.assertEqual(result['summary']['calendar_gaps'], 1)
        self.assertIsNone(result['summary']['metrics']['ratio_har_dvol_minus_ratio_har']['confidence_interval_95'])

    def test_pair_construction_rejects_undeclared_alignment(self):
        with self.assertRaises(ValueError):
            paired_cases({}, 0, DAY_MS, lag_days=2)

    def test_revision_audit_distinguishes_used_closes_from_unused_ohlc(self):
        from types import SimpleNamespace
        old = SimpleNamespace(prices={0: (3., 4., 2., 3.)}, dvol={0: (20., 22., 18., 21.)})
        new = SimpleNamespace(prices={0: (3., 4., 2., 3.1)}, dvol={0: (19., 22., 18., 21.)})
        result = vintage_audit(old, new)
        self.assertEqual(result['prices']['changed_closes'], 1)
        self.assertEqual(result['dvol']['changed_closes'], 0)
        self.assertEqual(result['dvol']['changed_ohlc'], 1)
        self.assertIsNone(correlation([1, 1, 1], [2, 3, 4]))

    def test_independent_target_reconstruction_checks_both_assets(self):
        from types import SimpleNamespace
        start = 40*DAY_MS
        returns = [.001*(-1)**i for i in range(720)]
        closes = np.exp(np.r_[0., np.cumsum(returns)])
        prices = {start-HOUR_MS+i*HOUR_MS: (v, v, v, v) for i, v in enumerate(closes)}
        inputs = {asset: SimpleNamespace(prices=prices) for asset in ('BTC', 'ETH')}
        rows = [{'target_start_ms': start, 'target_end_ms': start+30*DAY_MS, 'actual': [.00876, .00876]}]
        self.assertTrue(independent_targets(inputs, rows)['passed'])
        rows[0]['actual'][1] *= 2
        self.assertFalse(independent_targets(inputs, rows)['passed'])

    def test_source_audit_exposes_gaps_and_unused_open_mismatch(self):
        from types import SimpleNamespace
        day = 40*DAY_MS
        prices = {day+i*HOUR_MS: (2., 3., 1., 2.) for i in range(24)}
        dvol_hours = {day+i*HOUR_MS: (20., 22., 18., 21.) for i in range(24)}
        item = SimpleNamespace(prices=prices, dvol={day: (19., 22., 18., 21.)}, alignment_dvol=dvol_hours,
                               alignment_prices={}, duplicate_candles=0, failed_attempts=0)
        config = {'price_start_ms': day, 'price_end_ms': day+DAY_MS, 'dvol_start_ms': day, 'dvol_end_ms': day+DAY_MS,
                  'alignment_check_days_ms': [day], 'dvol_currency': 'ETH', 'price_instrument': 'ETH-PERPETUAL'}
        audit = source_audit(item, config)
        self.assertTrue(audit['required_close_alignment_passed'])
        self.assertEqual(audit['unused_ohlc_discrepancies'], 1)
        del prices[day]
        self.assertEqual(source_audit(item, config)['missing_price_hours'], [day])

    def synthetic_sources(self):
        from types import SimpleNamespace
        first, end = 1000*DAY_MS, 1140*DAY_MS
        inputs, configs = {}, {}
        for index, asset in enumerate(('BTC', 'ETH')):
            times = np.arange(first-31*DAY_MS, end+31*DAY_MS, HOUR_MS, dtype=np.int64)
            returns = .002*np.sin(np.arange(len(times))*(.83+index*.05))+.0003
            closes = np.exp(np.cumsum(returns))
            prices = {int(t): (float(v),)*4 for t, v in zip(times, closes)}
            dvol = {t: (50.+index*15+3*math.sin(t/DAY_MS/5),)*4 for t in range(first-DAY_MS, end+DAY_MS, DAY_MS)}
            day = first+10*DAY_MS
            align = {day+i*HOUR_MS: dvol[day] for i in range(24)}
            inputs[asset] = SimpleNamespace(prices=prices, dvol=dvol, alignment_dvol=align, alignment_prices={},
                duplicate_candles=0, failed_attempts=0, manifest={'asset': asset})
            configs[asset] = {'price_start_ms': int(times[0]), 'price_end_ms': int(times[-1])+HOUR_MS,
                             'dvol_start_ms': first-DAY_MS, 'dvol_end_ms': end+DAY_MS,
                             'alignment_check_days_ms': [day], 'dvol_currency': asset,
                             'price_instrument': asset+'-PERPETUAL', 'origin_end_ms': end}
        spec = {**self.spec, 'case_start': self.iso(first), 'evaluation_start': self.iso(first+120*DAY_MS),
                'evaluation_end': self.iso(end), 'training_completed_labels': 60,
                'inference': {**self.spec['inference'], 'bootstrap_resamples': 2000, 'sensitivity_block_days': []}}
        return inputs, configs, spec

    def test_daily_hourly_target_alignment_and_end_to_end_offline_rebuild(self):
        inputs, configs, spec = self.synthetic_sources()
        cases = paired_cases(inputs, utc_ms(spec['case_start']), utc_ms(spec['evaluation_end']))
        row = cases[-1]
        self.assertEqual(row.target_start_ms, row.origin_ms)
        self.assertEqual(row.target_end_ms, row.origin_ms+30*DAY_MS)
        shifted = paired_cases(inputs, row.origin_ms, row.origin_ms, target_shift_hours=1)[0]
        self.assertEqual(shifted.target_start_ms, row.origin_ms+HOUR_MS)
        lagged = paired_cases(inputs, row.origin_ms, row.origin_ms, lag_days=1)[0]
        self.assertEqual(lagged.features[0][3], (inputs['BTC'].dvol[row.origin_ms-2*DAY_MS][3]/100)**2)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'rebuild'
            with patch('implied_calibration.relative_report.load_relative_spec', return_value=(spec, {'local': True})), \
                 patch('implied_calibration.relative_report.load_study', side_effect=[configs['BTC'], configs['ETH'], configs['BTC']]), \
                 patch('implied_calibration.relative_report.read_study', side_effect=[inputs['BTC'], inputs['ETH'], inputs['BTC']]):
                result = write_relative_report(self.root, output)
            self.assertEqual(result['n'], 21)
            self.assertTrue(result['data_checks_passed'])
            self.assertFalse(result['formal_claim_eligible'])
            self.assertEqual(set(result['sensitivities']), {'one_day_DVOL_lag', 'one_hour_target_shift'})
            self.assertEqual(json.loads((output/'summary.json').read_text())['n'], 21)
            self.assertIn('0.00000', (output/'results.md').read_text())
            self.assertEqual(len((output/'forecasts.csv').read_text().splitlines()), 22)
            self.assertTrue((output/'eth-relative-variance.pdf').read_bytes().startswith(b'%PDF'))
            self.assertEqual(result['btc_vintage_scores']['old'], result['btc_vintage_scores']['new'])


if __name__ == '__main__':
    unittest.main()
