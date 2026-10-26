"""Public evidence excludes private paths, fails late and retains server bindings."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from implied_calibration.public_variance import (allowed_public_path, current_bundle, export_failure, freeze_once, preflight,
    check_liveness, refresh_and_score, retain_failure, retain_server_receipt, transfer_state,
    quarantine_unreferenced_corruption, validate_current_forecast, validate_staged_changes, verify_publication)
from implied_calibration.variance_forward import write_immutable
from implied_calibration.variance_forward import freeze_record, forward_summary, publish_forecast
import test_variance_forward as forward_tests
from scripts.audit_forward import audit


class PublicVarianceTests(unittest.TestCase):
    setUp = forward_tests.ForwardVarianceTests.setUp
    record = forward_tests.ForwardVarianceTests.record

    def test_restored_forecast_requires_valid_plan_training_source_and_timely_receipt(self):
        from copy import deepcopy
        root = Path(__file__).resolve().parents[1]
        for fault in ('none', 'plan', 'future_training', 'late_receipt', 'missing_raw', 'wrong_origin', 'bad_forecast'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                forward_tests.ForwardVarianceTests.seed_capture(self, folder)
                record = deepcopy(self.record())
                if fault == 'plan':
                    record['plan_hash'] = 'f'*64
                if fault == 'future_training':
                    record['training']['latest_target_end_ms'] = self.origin
                if fault == 'wrong_origin':
                    record['origin_ms'] += 86400000
                if fault == 'bad_forecast':
                    record['forecasts']['har'] = -1.
                frozen = write_immutable(folder/'forecasts'/f'{self.origin}.json', record)
                write_immutable(folder/'receipts'/f'{self.origin}.json', {
                    'origin_ms': self.origin, 'forecast_hash': frozen['content_hash'], 'status': 'accepted',
                    'durable_at_ms': self.origin if fault == 'late_receipt' else self.now})
                if fault == 'missing_raw':
                    (folder/'captures/example.txt').unlink()
                if fault == 'none':
                    validate_current_forecast(root, folder, self.origin, self.plan, self.now)
                else:
                    with self.assertRaises(ValueError):
                        validate_current_forecast(root, folder, self.origin, self.plan, self.now)

    def test_refresh_scores_after_last_origin_without_freezing_and_stops_at_analysis(self):
        from unittest.mock import Mock
        root = Path(__file__).resolve().parents[1]
        factory = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            now = self.plan['future']['origin_end_ms']+86400000
            self.assertEqual(refresh_and_score(root, folder, now_ms=lambda: now, job_factory=factory)['status'], 'refreshed_and_scored')
            factory.return_value.cycle.assert_called_once_with(include_summary=False, freeze_forecasts=False)
            factory.reset_mock()
            now = self.plan['future']['analysis_at_ms']+1
            self.assertEqual(refresh_and_score(root, folder, now_ms=lambda: now, job_factory=factory)['status'], 'terminal_cutoff_passed')
            factory.assert_not_called()

    def test_failure_export_never_publishes_failed_forecasts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder, target = Path(temporary)/'source', Path(temporary)/'public'
            (folder/'forecasts').mkdir(parents=True)
            (folder/'forecasts/123.json').write_text('{}')
            retain_failure(folder, 123, 1, now_ms=lambda: self.now)
            self.assertEqual(export_failure(folder, target)['files'], 1)
            self.assertFalse((target/'forecasts').exists())
            self.assertFalse(json.loads((target/'failures/123-1.json').read_text())['timely_public_witness'])

    def test_corrupt_unreferenced_capture_is_preserved_outside_active_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = folder/'captures/123-abcd/raw/2026-10-25.jsonl.gz'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'bad gzip')
            self.assertEqual(quarantine_unreferenced_corruption(folder), [path.relative_to(folder).as_posix()])
            self.assertFalse(path.exists())
            self.assertEqual((folder/'failed-captures/123-abcd/raw/2026-10-25.jsonl.gz').read_bytes(), b'bad gzip')
            public = folder/'public'
            self.assertEqual(export_failure(folder, public)['files'], 1)

    def test_artifact_contains_only_current_forecast_receipt_and_source_hashes(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            folder, target = Path(temporary)/'source', Path(temporary)/'bundle'
            forward_tests.ForwardVarianceTests.seed_capture(self, folder)
            publish_forecast(folder, self.record(), self.plan, lambda: self.now)
            manifest = current_bundle(root, folder, target, now_ms=lambda: self.now)
            self.assertEqual(len(list(target.rglob('*.json'))), 3)
            self.assertFalse((target/'captures').exists())
            self.assertEqual(manifest['raw_source_sha256'], self.proof['raw_files'])

    def test_interrupted_immutable_write_leaves_no_partial_final_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'record.json'
            with patch('implied_calibration.variance_forward.os.link', side_effect=OSError('interruption')):
                with self.assertRaises(OSError):
                    write_immutable(path, {'value': 1})
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob('*.tmp')), [])
            self.assertEqual(write_immutable(path, {'value': 1})['value'], 1)

    def test_export_excludes_credentials_images_and_unknown_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary)/'source', Path(temporary)/'target'
            (source/'forecasts').mkdir(parents=True)
            (source/'forecasts/123.json').write_text('{}')
            (source/'.env').write_text('private')
            (source/'screenshot.png').write_bytes(b'private')
            (source/'private.json').write_text('{}')
            result = transfer_state(source, target)
            self.assertEqual(result['files'], 1)
            self.assertEqual([p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()], ['forecasts/123.json'])
            self.assertFalse(allowed_public_path('../forecasts/123.json'))
            (target/'forecasts/123.json').write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'replace'):
                transfer_state(source, target)

    def test_export_has_a_size_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary)/'source', Path(temporary)/'target'
            (source/'forecasts').mkdir(parents=True)
            (source/'forecasts/123.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'size budget'):
                transfer_state(source, target, maximum_bytes=1)

    def test_server_receipt_rejects_wrong_hash_late_timestamp_and_foreign_run(self):
        record = {**self.record(), 'content_hash': 'a'*64}
        date = datetime.fromtimestamp(self.now/1000, timezone.utc).isoformat()
        receipt = {'forecast_hash': 'a'*64, 'created_at': date, 'artifact_digest': 'sha256:'+'b'*64,
                   'run_url': 'https://github.com/50kfunded/implied-odds/actions/runs/123'}
        verify_publication(record, receipt, self.plan)
        for changed in ({'forecast_hash': 'c'*64}, {'created_at': datetime.fromtimestamp(self.origin/1000, timezone.utc).isoformat()},
                        {'run_url': 'https://example.com/123'}, {'artifact_digest': 'sha256:unknown'}):
            with self.assertRaises(ValueError):
                verify_publication(record, {**receipt, **changed}, self.plan)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            metadata = folder/'metadata.json'
            metadata.write_text(json.dumps({'name': 'foreign', 'workflow_run': {'id': 123}}))
            with self.assertRaisesRegex(ValueError, 'this forecast workflow'):
                retain_server_receipt(folder, metadata, 123, 1)

    def test_late_run_cannot_start_a_backfill_or_make_api_calls(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch('implied_calibration.cli.provenance', return_value={'working_tree_dirty': False, 'git_commit': '0'*40}), \
             patch('implied_calibration.public_variance.VarianceForwardJob') as job:
            with self.assertRaises(ValueError):
                freeze_once(Path(__file__).resolve().parents[1], Path(temporary), now_ms=lambda: self.origin)
            job.assert_not_called()

    def test_local_audit_counts_permanent_omissions_even_when_status_is_stale(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = audit(Path(__file__).resolve().parents[1], Path(temporary), self.origin+4*86400000+14*3600000)
            self.assertEqual(result['n_missing'], 5)
            self.assertEqual(result['n_frozen'], 0)
            self.assertIsNone(result['local_status'])
            self.assertFalse(result['formal_claim_eligible'])

    def test_preflight_checks_actual_runner_endpoint_results_and_paces_calls(self):
        cutoff = self.origin-self.origin%86400000
        class Client:
            def __init__(self, **kwargs):
                self.calls = []
            def request(self, endpoint, params):
                from types import SimpleNamespace
                self.calls.append((endpoint, params))
                if 'currency' in params:
                    start = cutoff-(86400000 if params['resolution'] == '1D' else 3600000)
                    return SimpleNamespace(result={'data': [[start, 30., 30., 30., 30.]]})
                return SimpleNamespace(result={'status': 'ok', 'ticks': [cutoff-3600000]})
            def close(self):
                pass
        sleeps = []
        with tempfile.TemporaryDirectory() as temporary:
            result = preflight(Path(temporary), client_factory=Client, now_ms=lambda: self.origin, sleep=sleeps.append)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(sleeps, [10., 10.])

    def test_staging_rejects_replacements_deletions_and_private_paths(self):
        self.assertEqual(validate_staged_changes(b'A\0forecasts/123.json\0'), ['forecasts/123.json'])
        for changes in (b'M\0forecasts/123.json\0', b'D\0forecasts/123.json\0', b'A\0.env\0', b'A\0screenshot.png\0'):
            with self.assertRaises(ValueError):
                validate_staged_changes(changes)

    def test_watchdog_fails_without_a_recent_public_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises(ValueError):
                check_liveness(folder, now_ms=lambda: self.origin)
            write_immutable(folder/'observations/123-1.json', {'as_of_ms': self.origin})
            self.assertEqual(check_liveness(folder, now_ms=lambda: self.origin+3600000)['age_hours'], 1.)
            with self.assertRaises(ValueError):
                check_liveness(folder, now_ms=lambda: self.origin+37*3600000)
            with self.assertRaises((ValueError, FileNotFoundError)):
                check_liveness(folder, now_ms=lambda: self.origin+3600000, require_latest_forecast=True)

    def test_shared_source_is_hashed_once_per_audit_and_rechecked_next_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            capture = folder/'captures/example.txt'
            capture.parent.mkdir(parents=True)
            capture.write_bytes(b'capture')
            publish_forecast(folder, self.record(), self.plan, lambda: self.now)
            origin = self.origin+86400000
            proof = {**self.proof, 'feature_cutoff_ms': self.cutoff+86400000, 'latest_fetch_ms': self.now+86400000}
            record = freeze_record(self.cases, (.04, .05, .06, .09), self.plan, origin, self.now+86400000, proof)
            publish_forecast(folder, record, self.plan, lambda: self.now+86400000)
            original = Path.read_bytes
            reads = []
            def counted(path):
                if path.resolve() == capture.resolve():
                    reads.append(path)
                return original(path)
            with patch.object(Path, 'read_bytes', counted):
                self.assertEqual(forward_summary(folder, self.plan, origin, self.guard)['n_frozen'], 2)
            self.assertEqual(len(reads), 1)
            capture.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                forward_summary(folder, self.plan, origin, self.guard)


class WorkflowContractTests(unittest.TestCase):
    def test_cron_is_gated_and_preserves_publication_before_scoreboard(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'.github/workflows/freeze-variance.yml').read_text()
        self.assertIn("cron: '13,23,33 0 * * *'", source)
        self.assertIn("vars.VARIANCE_FORWARD_ENABLED == 'true'", source)
        self.assertIn('cancel-in-progress: false', source)
        self.assertIn('fetch-depth: 0', source)
        self.assertLess(source.index('id: forecast_artifact'), source.index('freeze_variance.py observe'))
        self.assertNotIn('force', source)
