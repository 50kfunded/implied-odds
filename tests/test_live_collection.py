"""I test live-job ordering, crash recovery and snapshot isolation without a network."""

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch

from implied_calibration.client import ClientError
from implied_calibration.collection import CollectionConfig, CollectionJob, FileLease, collect, request_stop
from implied_calibration.history import HOUR_MS
from implied_calibration.snapshot_report import snapshot_report
from test_phase1 import OfflineTest, SyntheticClock
from test_phase2 import BASE, DatabaseFixture, ingest, quote
from test_phase3 import draft, synthetic_chain
from test_phase6 import COMMIT, locked_fixture
from test_phase7 import extension_fixture


class SelectedSnapshotTests(DatabaseFixture):
    def seed_chain(self, stamp, mark_iv=60):
        rows = synthetic_chain()
        self.seed([r['instrument_name'] for r in rows], stamp=stamp)
        identity = self.book(stamp=stamp, rows=[quote(r['instrument_name'], bid_price=r['bid_btc'],
            ask_price=r['ask_btc'], mark_price=r['mark_btc'], mark_iv=mark_iv, underlying_price=100) for r in rows])
        ingest(self.recording, self.store)
        return identity

    def test_snapshot_figure_is_real_exploratory_and_does_not_freeze(self):
        self.seed_chain(BASE)
        output = self.root / 'snapshot'
        before = self.store.connection.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
        result = snapshot_report(self.store.path, draft(), output, COMMIT, BASE)
        first = (output / 'market-smile.svg').read_bytes()
        snapshot_report(self.store.path, draft(), output, COMMIT, BASE)
        self.assertEqual(first, (output / 'market-smile.svg').read_bytes())
        self.assertFalse(result['synthetic'])
        self.assertEqual(result['kind'], 'exploratory_market_snapshot')
        self.assertIn('Real Deribit quotes: exploratory', first.decode())
        self.assertEqual(before, self.store.connection.execute('SELECT COUNT(*) FROM predictions').fetchone()[0])

    def test_a_rejected_later_snapshot_cannot_leave_a_stale_figure(self):
        self.seed_chain(BASE)
        output = self.root / 'snapshot'
        snapshot_report(self.store.path, draft(), output, COMMIT, BASE)
        self.seed_chain(BASE + 1000, 47)
        result = snapshot_report(self.store.path, draft(), output, COMMIT, BASE + 1000)
        self.assertEqual(result['n_accepted_cdfs'], 0)
        self.assertFalse((output / 'market-smile.svg').exists())
        self.assertNotIn('figure_sha256', result)


class CollectionTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = CollectionConfig(self.root / 'capture', self.root / 'research.sqlite', self.root / 'report',
                                       interval=10, seconds=21)
        self.clock = SyntheticClock(BASE)

    def mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        parent = locked_fixture()
        extension = extension_fixture()
        stack.enter_context(patch('implied_calibration.cli.provenance', return_value={
            'git_commit': COMMIT, 'working_tree_dirty': False}))
        stack.enter_context(patch('implied_calibration.collection.load_locked_protocol', return_value=parent))
        stack.enter_context(patch('implied_calibration.collection.load_locked_baselines', return_value=extension))
        return parent, extension

    def test_os_lease_prevents_duplicate_and_recovers_after_process_exit(self):
        path = self.root / 'lease'
        with FileLease(path):
            with self.assertRaisesRegex(OSError, 'locked'):
                with FileLease(path):
                    self.fail('Duplicate lease')
        with FileLease(path):
            pass
        script = 'import os,sys; from pathlib import Path; from implied_calibration.collection import FileLease; x=FileLease(Path(sys.argv[1])); x.__enter__(); os._exit(0)'
        subprocess.run([sys.executable, '-c', script, str(path)], check=True, capture_output=True)
        with FileLease(path):
            pass

    def test_configuration_rejects_fast_polling_and_raw_report_overlap(self):
        for config in (replace(self.config, interval=9), replace(self.config, seconds=0),
                       replace(self.config, report_output=self.config.output / 'report')):
            with self.assertRaises(ValueError):
                config.validate()

    def test_alignment_graceful_stop_and_restart_keep_distinct_segments(self):
        self.mocks()
        segments = []
        def cycle(job, slot):
            segments.append((job.session, slot))
            if len(segments) == 2:
                request_stop(job.config.output)
            return {'failures': []}
        with patch.object(CollectionJob, 'cycle', cycle):
            result = collect(self.config, clock=self.clock)
            self.assertEqual(result['status'], 'stopped')
            second = collect(replace(self.config, seconds=1), clock=self.clock)
        self.assertNotEqual(result['session'], second['session'])
        self.assertTrue(all(slot % 10000 == 0 for _, slot in segments))
        self.assertEqual(result['cycles'], 2)
        self.assertFalse((self.config.output / 'STOP').exists())

    def test_sleep_gap_is_retained_and_no_slots_are_replayed(self):
        self.mocks()
        def cycle(job, slot):
            job.clock.sleep(35)
            return {'failures': []}
        with patch.object(CollectionJob, 'cycle', cycle):
            result = collect(replace(self.config, seconds=41), clock=self.clock)
        events = [json.loads(line) for p in (self.config.output / 'heartbeat').glob('*.jsonl') for line in p.read_text().splitlines()]
        self.assertTrue(any(e['event'] == 'gap' and e['reason'] == 'cycle_overrun' and e['count'] == 3 for e in events))
        self.assertTrue(all(e['ok'] is False for e in events if e['event'] == 'gap'))
        self.assertFalse(next(e for e in events if e['event'] == 'session_end')['ok'])
        self.assertGreater(result['skipped_slots'], 0)
        self.assertEqual(result['cycles'], 2)

    def test_fatal_failure_updates_status_and_releases_lease(self):
        self.mocks()
        with patch.object(CollectionJob, 'cycle', side_effect=ValueError('Synthetic schema failure')):
            with self.assertRaisesRegex(ValueError, 'schema failure'):
                collect(self.config, clock=self.clock)
        status = json.loads((self.config.output / 'status.json').read_text())
        self.assertEqual(status['status'], 'failed')
        with FileLease(self.config.output / '.collector.lock'):
            pass

    def test_restart_after_backward_clock_never_repeats_a_recorded_slot(self):
        self.mocks()
        self.config.output.mkdir()
        (self.config.output / 'status.json').write_text(json.dumps({'last_scheduled_ms': BASE}), encoding='utf-8')
        self.clock.wall = BASE - 10000
        observed = []
        def cycle(job, slot):
            observed.append(slot)
            request_stop(job.config.output)
            return {'failures': []}
        with patch.object(CollectionJob, 'cycle', cycle):
            result = collect(replace(self.config, seconds=31), clock=self.clock)
        self.assertEqual(observed, [BASE + 10000])
        self.assertEqual(result['clock_changes'], 1)

    def test_cycle_bootstraps_history_before_metadata_index_and_book_then_freezes(self):
        parent, extension = self.mocks()
        self.config.output.mkdir()
        order = []
        client = Mock()
        client.request.side_effect = lambda endpoint, parameters: order.append(endpoint)
        job = CollectionJob(self.config, parent, extension, COMMIT, clock=self.clock,
                            client_factory=lambda **kwargs: client)
        def history(segment):
            order.append('history')
            job.history_hour = self.clock.now_ms() // HOUR_MS
            return {'readiness_status': 'ok'}
        def freeze(*args, **kwargs):
            order.append('freeze')
            return {'added': 0, 'predictions': [], 'baselines': []}
        with patch.object(job, '_history', history), \
             patch('implied_calibration.collection.ingest', return_value={}), \
             patch('implied_calibration.collection.freeze_predictions', side_effect=freeze), \
             patch('implied_calibration.collection.fetch_delivery_pages', return_value={}), \
             patch('implied_calibration.collection.ingest_deliveries', return_value={}), \
             patch('implied_calibration.collection.score_database', return_value={'added': 0, 'scores': []}), \
             patch('implied_calibration.collection.score_baselines', return_value={'added': 0, 'scores': []}):
            job.cycle(BASE)
            job.cycle(BASE + 10000)
        self.assertEqual(order[:5], ['history', 'public/get_instruments', 'public/get_index_price',
                                    'public/get_book_summary_by_currency', 'freeze'])
        self.assertEqual(order.count('history'), 1)
        self.assertEqual(order.count('public/get_instruments'), 1)
        self.assertEqual(order.count('freeze'), 2)

    def test_failed_history_is_retried_and_is_not_marked_fresh(self):
        parent, extension = self.mocks()
        job = CollectionJob(self.config, parent, extension, COMMIT, clock=self.clock)
        with patch.object(job, '_history', side_effect=ClientError('network_error', 'Synthetic failure')), \
             patch.object(job, '_client', side_effect=ValueError('Stop after history')):
            with self.assertRaisesRegex(ValueError, 'Stop after history'):
                job.cycle(BASE)
        self.assertIsNone(job.history_hour)

    def test_history_refresh_fills_a_long_sleep_and_keeps_failed_refresh_cutoff(self):
        parent, extension = self.mocks()
        job = CollectionJob(self.config, parent, extension, COMMIT, clock=self.clock,
                            client_factory=lambda **kwargs: Mock())
        old_hour = self.clock.now_ms() // HOUR_MS - 8
        job.history_hour = old_hour
        with patch('implied_calibration.collection.fetch_history', side_effect=ClientError('network_error', 'Synthetic outage')):
            with self.assertRaises(ClientError):
                job._history(self.root / 'failed')
        self.assertEqual(job.history_hour, old_hour)
        with patch('implied_calibration.collection.fetch_history', return_value={}) as fetched, \
             patch('implied_calibration.collection.ingest_history', return_value={}), \
             patch('implied_calibration.collection.trailing_returns', return_value={'status': 'ok'}):
            job._history(self.root / 'recovered')
        end = self.clock.now_ms() // HOUR_MS * HOUR_MS
        self.assertEqual(fetched.call_args.args[1:3], (end - 10 * HOUR_MS, end))
        self.assertEqual(job.history_hour, end // HOUR_MS)

    def test_no_data_bootstrap_never_permanently_marks_short_history_fresh(self):
        parent, extension = self.mocks()
        job = CollectionJob(self.config, parent, extension, COMMIT, clock=self.clock,
                            client_factory=lambda **kwargs: Mock())
        with patch('implied_calibration.collection.fetch_history', return_value={'no_data_pages': 4}) as fetched, \
             patch('implied_calibration.collection.ingest_history', return_value={'added': 0}):
            first = job._history(self.root / 'first')
            second = job._history(self.root / 'second')
        self.assertIsNone(job.history_hour)
        self.assertEqual(first['readiness_status'], 'short_history')
        self.assertEqual(second['readiness_status'], 'short_history')
        self.assertEqual(fetched.call_count, 2)
        self.assertTrue(all(call.args[2] - call.args[1] == 2162 * HOUR_MS for call in fetched.call_args_list))
