"""Backfill rejection, immutable records and late terminal-outcome checks."""

from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from implied_calibration.incremental import VarianceCase
from implied_calibration.research_plan import load_plan
from implied_calibration.store import digest
from implied_calibration.variance_forward import (ForecastUnavailable, VarianceForwardJob, freeze_record,
    forward_summary, publish_forecast, read_immutable, recover_forecast_receipt, refresh_start, write_immutable)
from implied_calibration.variance_study import DAY_MS, HOUR_MS


ROOT = Path(__file__).resolve().parents[1]


class ForwardVarianceTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan(ROOT/'protocol/incremental-variance-v1.json')
        self.guard = load_plan(ROOT/'protocol/inference-validation-v1.json')
        self.origin = self.plan['future']['origin_start_ms']
        self.cutoff = self.origin-HOUR_MS
        self.now = self.origin-30*60_000
        self.cases = [VarianceCase(self.cutoff-(450-i)*DAY_MS,self.cutoff-(450-i)*DAY_MS+HOUR_MS,
                      self.cutoff-(420-i)*DAY_MS+HOUR_MS,(.04,.05,.06,.09),.08) for i in range(450)]
        self.proof = {'feature_cutoff_ms':self.cutoff,'latest_fetch_ms':self.now,'daily_close':30.,'last_hourly_close':30.,
                      'seed_manifest_hash':digest(json.loads((ROOT/'recordings/historical-variance/manifest.json').read_text())),
                      'git_commit':'0'*40,'raw_files':{'captures/example.txt':hashlib.sha256(b'capture').hexdigest()}}

    def seed_capture(self,directory):
        path = directory/'captures/example.txt'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'capture')

    def record(self):
        return freeze_record(self.cases,(.04,.05,.06,.09),self.plan,self.origin,self.now,self.proof)

    def test_freeze_uses_completed_shifted_targets_and_precedes_start(self):
        record = self.record()
        self.assertLess(record['frozen_at_ms'],record['origin_ms'])
        self.assertLessEqual(record['training']['latest_target_end_ms'],self.cutoff)
        self.assertEqual(record['target_end_ms'],self.origin+30*DAY_MS)

    def test_freeze_after_window_cannot_backfill_or_ignore_close_failure(self):
        with self.assertRaises(ValueError):
            freeze_record(self.cases,(.04,.05,.06,.09),self.plan,self.origin,self.origin,self.proof)
        with self.assertRaises(ValueError):
            freeze_record(self.cases,(.04,.05,.06,.09),self.plan,self.origin,self.now,{**self.proof,'last_hourly_close':31.})
        with self.assertRaises(ValueError):
            freeze_record(self.cases,(.04,.05,.06,.16),self.plan,self.origin,self.now,self.proof)

    def test_late_fit_and_late_durable_write_are_not_accepted(self):
        with self.assertRaises(ValueError):
            freeze_record(self.cases,(.04,.05,.06,.09),self.plan,self.origin,self.now,self.proof,
                          completed_now_ms=lambda:self.origin)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            publish_forecast(directory,self.record(),self.plan,lambda:self.origin)
            result = forward_summary(directory,self.plan,self.origin,self.guard)
            self.assertEqual(result['n_frozen'],0)
            self.assertEqual(result['n_missing'],1)

    def test_immutable_record_rejects_replacement_and_detects_changed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'forecast.json'
            payload = self.record()
            original = write_immutable(path,payload)
            self.assertEqual(original,write_immutable(path,payload))
            with self.assertRaises(ValueError):
                write_immutable(path,{**payload,'origin_ms':self.origin+DAY_MS})
            path.write_text(path.read_text().replace('0.08','0.18'),encoding='utf-8')
            with self.assertRaises(ValueError):
                read_immutable(path)

    def test_all_planned_days_count_including_missing_endpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = forward_summary(Path(temporary),self.plan,self.plan['future']['analysis_at_ms'],self.guard)
            self.assertEqual(result['n_planned'],365)
            self.assertEqual(result['n_missing'],365)
            self.assertFalse(result['formal_claim_eligible'])
            self.assertNotIn('descriptive_terminal',result)

    def test_scores_after_terminal_availability_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.seed_capture(directory)
            forecast = publish_forecast(directory,self.record(),self.plan,lambda:self.now)
            terminal = self.plan['future']['analysis_at_ms']
            score = {'origin_ms':self.origin,'forecast_hash':forecast['content_hash'],'scored_at_ms':terminal+1,
                     'losses':{'har':1.,'har_dvol':.9}}
            write_immutable(directory/'scores/first.json',score)
            result = forward_summary(directory,self.plan,terminal+DAY_MS,self.guard)
            self.assertEqual(result['n_frozen'],1)
            self.assertEqual(result['n_scored'],0)

    def test_changed_source_cannot_keep_an_accepted_forecast(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.seed_capture(directory)
            publish_forecast(directory,self.record(),self.plan,lambda:self.now)
            self.assertEqual(forward_summary(directory,self.plan,self.origin,self.guard)['n_frozen'],1)
            (directory/'captures/example.txt').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                forward_summary(directory,self.plan,self.origin,self.guard)

    def test_missing_forecast_receipt_recovers_without_refitting_only_before_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.seed_capture(directory)
            path = directory/'forecasts'/f'{self.origin}.json'
            record = write_immutable(path,self.record())
            self.assertEqual(forward_summary(directory,self.plan,self.origin,self.guard)['n_frozen'],0)
            with patch('implied_calibration.variance_forward.fit_forecasts',side_effect=AssertionError('I must not refit')):
                recover_forecast_receipt(directory,record,self.plan,lambda:self.now+1)
            self.assertEqual(forward_summary(directory,self.plan,self.origin,self.guard)['n_frozen'],1)
            self.assertEqual(read_immutable(path),record)
            with self.assertRaises(ForecastUnavailable):
                recover_forecast_receipt(directory,record,self.plan,lambda:self.origin)

    def test_unavailable_midnight_inputs_retry_without_terminating_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            elapsed = [0.]
            job = VarianceForwardJob.__new__(VarianceForwardJob)
            job.directory = Path(temporary)
            job.state = {'status':'starting','cycles':0,'completed_cycles':0,'failed_cycles':0}
            job.now_ms = lambda:self.now+int(elapsed[0]*1000)
            job.sleep = lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds)
            job.cycle = Mock(side_effect=[ForecastUnavailable('missing_inputs'),{}])
            with patch('implied_calibration.variance_forward.time.monotonic',side_effect=lambda:elapsed[0]), \
                 patch('implied_calibration.cli.provenance',return_value={'working_tree_dirty':False,'git_commit':'0'*40}):
                state = job.run(600)
            self.assertEqual((state['cycles'],state['completed_cycles'],state['failed_cycles']),(2,1,1))
            self.assertEqual(state['status'],'stopped')

    def test_refresh_returns_to_old_internal_gaps_instead_of_latest_close_only(self):
        end = self.cutoff
        prices = {t:1 for t in range(end-24*HOUR_MS,end,HOUR_MS)}
        del prices[end-8*HOUR_MS]
        self.assertEqual(refresh_start(prices,HOUR_MS,end),end-8*HOUR_MS)
        daily = {t:1 for t in range(end-10*DAY_MS,end,DAY_MS)}
        del daily[end-5*DAY_MS]
        self.assertEqual(refresh_start(daily,DAY_MS,end),end-5*DAY_MS)
