"""Offline recovery, saved inversions and local operational evidence checks."""

from contextlib import ExitStack, closing
from copy import deepcopy
import json
import sqlite3
from threading import Event
from unittest.mock import Mock, patch

from implied_calibration.blackscholes import implied_vol
from implied_calibration.cleaning import clean_database
from implied_calibration.client import ApiResponse, AttemptRecord
from implied_calibration.collection import CollectionConfig, CollectionJob, collect
from implied_calibration.operational_status import build_scoreboard, liveness_check, write_scoreboard
from implied_calibration.predictions import freeze_predictions
from implied_calibration.protocol import validate_protocol
from implied_calibration.raw import read_raw
from implied_calibration.report import build_report
from implied_calibration.settlement import RecoverableDeliveryCaptureError, fetch_delivery_pages, ingest_deliveries
from test_phase1 import SyntheticClock
from test_phase2 import DatabaseFixture
from test_phase6 import COMMIT, DAY, EXPIRY, locked_fixture
from test_phase7 import extension_fixture
from test_settlement import delivery_page
import test_phase6 as phase6


class CleaningCacheTests(DatabaseFixture):
    seed_chain = phase6.FrozenForecastTests.seed_chain

    def cached(self, protocol=None, cutoff=EXPIRY, identity=None):
        return clean_database(self.store.path, protocol or locked_fixture().protocol, cutoff,
                              snapshot_id=identity, cache_path=self.root / 'clean-cache.sqlite')

    def test_repeated_report_does_not_invert_unchanged_quotes(self):
        self.seed_chain()
        kwargs = {'clean_cache_path': self.root / 'clean-cache.sqlite'}
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            first = build_report(self.store.path, locked_fixture(), extension_fixture(), EXPIRY, COMMIT, **kwargs)
            self.assertEqual(inversion.call_count, 28)
        with patch('implied_calibration.cleaning.implied_vol', side_effect=AssertionError('I repeated an inversion')):
            second = build_report(self.store.path, locked_fixture(), extension_fixture(), EXPIRY, COMMIT, **kwargs)
        self.assertEqual(first, second)

    def test_new_groups_are_incremental_and_earlier_cutoffs_match_uncached_results(self):
        first_id = self.seed_chain()
        first = self.cached(cutoff=EXPIRY - DAY)
        second_id = self.seed_chain(EXPIRY - DAY + 1_000)
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            second = self.cached()
        self.assertEqual(inversion.call_count, 28)
        self.assertEqual(second, clean_database(self.store.path, locked_fixture().protocol, EXPIRY))
        with patch('implied_calibration.cleaning.implied_vol', side_effect=AssertionError('I repeated an inversion')):
            self.assertEqual(first, self.cached(cutoff=EXPIRY - DAY))
            selected = self.cached(identity=second_id)
        self.assertEqual([group['snapshot_id'] for group in selected['groups']], [second_id])
        self.assertNotEqual(first_id, second_id)

    def test_quote_metadata_protocol_and_code_changes_invalidate_saved_groups(self):
        identity = self.seed_chain()
        self.cached()
        changes = [
            ('UPDATE quotes SET mark_iv_pct=59 WHERE snapshot_id=?', (identity,)),
            ('UPDATE instruments SET tick_size=tick_size*2', ()),
        ]
        for sql, params in changes:
            with self.store.connection:
                self.store.connection.execute(sql, params)
            with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
                saved = self.cached()
            self.assertEqual(inversion.call_count, 28)
            self.assertEqual(saved, clean_database(self.store.path, locked_fixture().protocol, EXPIRY))
        choices = deepcopy(locked_fixture().protocol.content)
        choices['cleaning']['maximum_spread_to_mid'] /= 2
        protocol = validate_protocol(choices)
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            self.cached(protocol)
        self.assertEqual(inversion.call_count, 28)
        with patch('implied_calibration.cleaning._cache_implementation', return_value='changed'), \
             patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            self.cached()
        self.assertEqual(inversion.call_count, 28)

    def test_corrupt_saved_payload_is_recomputed(self):
        self.seed_chain()
        first = self.cached()
        with closing(sqlite3.connect(self.root / 'clean-cache.sqlite')) as cache, cache:
            cache.execute("UPDATE clean_groups SET payload_json='{}'")
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            self.assertEqual(first, self.cached())
        self.assertEqual(inversion.call_count, 28)

    def test_prediction_cleaning_does_not_grow_with_unrelated_old_snapshots(self):
        selected = self.seed_chain()
        baseline = self.root/'before-history.sqlite'
        with closing(sqlite3.connect(baseline)) as connection:
            self.store.connection.backup(connection)
        arguments = (locked_fixture(), EXPIRY-DAY, EXPIRY-DAY, COMMIT)
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            before = freeze_predictions(baseline, *arguments, expiries=[EXPIRY])
            before_calls = inversion.call_count
        for i in range(20):
            self.seed_chain(EXPIRY-DAY-(i+1)*1000)
        with patch('implied_calibration.cleaning.implied_vol', wraps=implied_vol) as inversion:
            after = freeze_predictions(self.store.path, *arguments, expiries=[EXPIRY])
            self.assertEqual(inversion.call_count, before_calls)
        self.assertEqual(before['predictions'], after['predictions'])
        self.assertEqual(next(row['snapshot_id'] for row in after['predictions'] if row['lead_ms'] == DAY), selected)
        details = self.store.connection.execute('EXPLAIN QUERY PLAN SELECT * FROM quotes WHERE snapshot_id=?', (selected,)).fetchall()
        self.assertTrue(any('quotes_snapshot' in str(tuple(row)) for row in details))


class OperationalRecoveryTests(DatabaseFixture):
    def config(self):
        return CollectionConfig(self.root / 'capture', self.store.path, self.root / 'report',
                                interval=10, seconds=31)

    def setup_collection(self, pages):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('implied_calibration.cli.provenance', return_value={
            'git_commit': COMMIT, 'working_tree_dirty': False}))
        stack.enter_context(patch('implied_calibration.collection.load_locked_protocol', return_value=locked_fixture()))
        stack.enter_context(patch('implied_calibration.collection.load_locked_baselines', return_value=extension_fixture()))
        stack.enter_context(patch.object(CollectionJob, '_history', return_value={'readiness_status': 'ok'}))
        stack.enter_context(patch('implied_calibration.collection.ingest', return_value={}))
        stack.enter_context(patch('implied_calibration.collection.freeze_predictions', return_value={
            'added': 0, 'predictions': [], 'baselines': []}))
        stack.enter_context(patch('implied_calibration.collection.score_database', return_value={'added': 0, 'scores': []}))
        stack.enter_context(patch('implied_calibration.collection.score_baselines', return_value={'added': 0, 'scores': []}))
        pending = iter(pages)
        self.delivery_offsets = []
        offsets = self.delivery_offsets
        class RetainedClient:
            def __init__(self, *, on_attempt, now_ms, **kwargs):
                self.retain, self.now = on_attempt, now_ms
            def request(self, endpoint, params):
                if endpoint != 'public/get_delivery_prices':
                    return Mock()
                offsets.append(params['offset'])
                page = next(pending)
                stamp = self.now()
                record = AttemptRecord(stamp, stamp, stamp, 0, endpoint, params, 200,
                                       page['body'], 1, True, 'ok', '')
                self.retain(record)
                return ApiResponse(page['body']['result'], record)
            def close(self):
                pass
        return RetainedClient

    def test_moving_total_recovers_next_cycle_from_zero_with_retained_attempts(self):
        older = [{'date': '2026-12-04', 'delivery_price': 100.}]
        pages = [delivery_page(EXPIRY, total=2), delivery_page(EXPIRY, older, total=3),
                 delivery_page(EXPIRY, total=2), delivery_page(EXPIRY, older, total=2)]
        factory = self.setup_collection(pages)
        result = collect(self.config(), clock=SyntheticClock(EXPIRY + 100_000), client_factory=factory)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['cycles'], result['completed_cycles'], result['failed_cycles']), (2, 2, 1))
        self.assertEqual((result['attempts'], result['failed_attempts']), (4, 0))
        self.assertEqual((result['settlement_capture_attempts'], result['settlement_capture_failures']), (2, 1))
        self.assertEqual(self.delivery_offsets, [0, 1, 0, 1])
        retained = [list(read_raw(path)) for path in self.config().output.rglob('*.jsonl.gz')]
        self.assertEqual(sum(map(len, retained)), 4)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_sources').fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_prices').fetchone()[0], 2)
        scoreboard = json.loads((self.config().report_output / 'scoreboard.json').read_text())
        self.assertEqual(scoreboard['liveness']['status'], 'inactive')

    def test_immutable_delivery_conflict_stays_fatal_and_counts_attempted_cycle(self):
        self.write(delivery_page(EXPIRY + 1))
        ingest_deliveries(self.recording, self.store.path, EXPIRY + 1)
        conflict = delivery_page(EXPIRY, [{'date': '2026-12-05', 'delivery_price': 102.}])
        factory = self.setup_collection([conflict])
        with self.assertRaisesRegex(ValueError, 'Conflicting official delivery'):
            collect(self.config(), clock=SyntheticClock(EXPIRY + 100_000), client_factory=factory)
        state = json.loads((self.config().output / 'status.json').read_text())
        self.assertEqual((state['cycles'], state['completed_cycles'], state['failed_cycles']), (1, 0, 1))
        self.assertEqual((state['attempts'], state['settlement_capture_failures']), (1, 1))
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(self.store.connection.execute('SELECT delivery_price FROM delivery_prices').fetchone()[0], 101.)

    def test_page_limit_has_a_typed_reason_and_bad_configuration_stays_fatal(self):
        client = Mock()
        client.request.return_value = ApiResponse({'data': [{'date': '2026-12-05'}], 'records_total': 2}, None)
        with self.assertRaises(RecoverableDeliveryCaptureError) as raised:
            fetch_delivery_pages(client, count=1, max_pages=1, sleep=Mock())
        self.assertEqual(raised.exception.reason, 'pagination_page_limit')
        with self.assertRaises(ValueError) as raised:
            fetch_delivery_pages(client, count=0)
        self.assertNotIsInstance(raised.exception, RecoverableDeliveryCaptureError)

    def test_blocked_renderer_leaves_forecast_cycles_free_and_coalesces_latest_cutoff(self):
        started, release = Event(), Event()
        rendered = []
        factory = self.setup_collection([delivery_page(EXPIRY)])
        clock = SyntheticClock(EXPIRY + 100_000)
        job = CollectionJob(self.config(), locked_fixture(), extension_fixture(), COMMIT,
                            clock=clock, client_factory=factory)
        def render(config, parent, extension, cutoff, commit):
            rendered.append(cutoff)
            started.set()
            if not release.wait(5):
                raise AssertionError('I did not release my renderer')
            return {'status': 'ok'}
        try:
            with patch('implied_calibration.collection._render_report', side_effect=render), \
                 patch('implied_calibration.collection.score_database', return_value={'added': 1, 'scores': []}):
                first = job.cycle(clock.now_ms())
                self.assertTrue(started.wait(2))
                self.assertEqual(first['report']['status'], 'rendering')
                clock.sleep(10)
                second = job.cycle(clock.now_ms())
                clock.sleep(10)
                third = job.cycle(clock.now_ms())
                self.assertEqual(second['report']['status'], 'rendering')
                self.assertEqual(third['report']['status'], 'rendering')
                self.assertEqual(len(rendered), 1)
                latest = clock.now_ms()
                release.set()
                job.close_reports()
                self.assertEqual(rendered, [first['report']['as_of_ms'], latest])
        finally:
            release.set()
            job.close_reports()


class ScoreboardLedgerTests(DatabaseFixture):
    seed_chain = phase6.FrozenForecastTests.seed_chain

    def scoreboard(self, now=EXPIRY - DAY):
        return build_scoreboard(self.store.path, locked_fixture().protocol.content_hash,
                                extension_fixture().content_hash,
                                {'status': 'waiting', 'updated_at_ms': now, 'interval_seconds': 10}, now)

    def test_frozen_hashes_are_chained_retained_and_sql_immutable(self):
        self.seed_chain()
        frozen = freeze_predictions(self.store.path, locked_fixture(), EXPIRY - DAY, EXPIRY - DAY,
                                    COMMIT, expiries=[EXPIRY], baseline_extension=extension_fixture())
        first = self.scoreboard()
        write_scoreboard(first, self.root / 'report')
        self.assertEqual(first['forecasts']['frozen'], len(frozen['predictions']))
        self.assertEqual(first['forecasts']['accepted'], 1)
        self.assertGreater(first['forecasts']['rejection_reason_counts']['late_freeze'], 0)
        self.assertEqual(first, self.scoreboard())
        anchors = list((self.root / 'report' / 'frozen-anchors').glob('*.json'))
        self.assertEqual(len(anchors), 1)
        for sql in ('DELETE FROM frozen_hash_ledger', 'UPDATE frozen_hash_ledger SET payload_hash=payload_hash',
                    'INSERT OR REPLACE INTO frozen_hash_ledger SELECT * FROM frozen_hash_ledger LIMIT 1',
                    "INSERT OR REPLACE INTO frozen_hash_ledger SELECT (SELECT MAX(sequence)+1 FROM frozen_hash_ledger),source_table,record_id,payload_hash,created_at_ms,(SELECT entry_hash FROM frozen_hash_ledger ORDER BY sequence DESC LIMIT 1),'changed' FROM frozen_hash_ledger LIMIT 1"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(sql)
            self.store.connection.rollback()
        rollback = deepcopy(first)
        rollback['frozen_anchor'] = {'schema_version': 1, 'entries': 0, 'genesis_hash': '0' * 64, 'head_hash': '0' * 64}
        rollback['frozen_ledger'] = []
        with self.assertRaisesRegex(ValueError, 'rollback'):
            write_scoreboard(rollback, self.root / 'report')
        (self.root / 'report' / 'frozen-anchor.json').unlink()
        with self.assertRaisesRegex(ValueError, 'rollback'):
            write_scoreboard(rollback, self.root / 'report')

    def test_external_ledger_tampering_is_detected_after_sql_guards_are_removed(self):
        self.seed_chain()
        freeze_predictions(self.store.path, locked_fixture(), EXPIRY - DAY, EXPIRY - DAY,
                           COMMIT, expiries=[EXPIRY])
        self.scoreboard()
        with self.store.connection:
            self.store.connection.execute('DROP TRIGGER immutable_frozen_hash_ledger_UPDATE')
            self.store.connection.execute("UPDATE frozen_hash_ledger SET payload_hash='changed' WHERE sequence=1")
        with self.assertRaisesRegex(ValueError, 'ledger mismatch'):
            self.scoreboard()

    def test_liveness_age_detects_stopped_stale_and_backward_clock_states(self):
        state = {'status': 'collecting', 'updated_at_ms': 100_000, 'interval_seconds': 10}
        self.assertEqual(liveness_check(state, 100_000)['status'], 'fresh')
        self.assertEqual(liveness_check(state, 160_001)['status'], 'stale')
        self.assertEqual(liveness_check(state, 98_999)['status'], 'clock_moved_backward')
        self.assertEqual(liveness_check({**state, 'status': 'failed'}, 160_001)['status'], 'inactive')
