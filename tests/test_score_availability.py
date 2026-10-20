"""I test post-commit score availability at the fixed analysis boundary offline."""

from contextlib import contextmanager, redirect_stdout
import io
import json
import sqlite3
from unittest.mock import patch

from implied_calibration.cli import main
from implied_calibration.evaluation import score_database
from implied_calibration.report import build_report
from implied_calibration.settlement import ingest_deliveries
from test_phase1 import SyntheticClock
from test_phase2 import DatabaseFixture
from test_phase6 import COMMIT, EXPIRY, locked_fixture
from test_phase7 import extension_fixture
from test_settlement import delivery_page
import test_phase6 as phase6
import test_phase7 as phase7


class ScoreAvailabilityTests(DatabaseFixture):
    seed_chain = phase6.FrozenForecastTests.seed_chain
    freeze_all = phase7.FrozenBaselineTests.freeze_all

    def setUp(self):
        super().setUp()
        self.seed_chain()
        self.freeze_all()
        self.write(delivery_page(EXPIRY + 100))
        ingest_deliveries(self.recording, self.store.path, EXPIRY + 100)
        self.terminal = EXPIRY + 1000
        self.clock = SyntheticClock(self.terminal - 1)

    def choices(self, evaluation, parent_hash, baseline_hash, as_of_ms):
        return {**evaluation, 'formal_decision_allowed': as_of_ms >= self.terminal,
                'expiry_cutoff_ms': EXPIRY, 'final_analysis_at_ms': self.terminal,
                'stopping_amendment_hash': 'a' * 64}

    def cli(self, command):
        captured = io.StringIO()
        with patch('sys.argv', ['cli', command, '--db', str(self.store.path)]), \
             patch('implied_calibration.cli.provenance', return_value={
                 'git_commit': COMMIT, 'working_tree_dirty': False}), \
             patch('implied_calibration.cli.utc_ms', side_effect=self.clock.now_ms), \
             patch('implied_calibration.cli.load_locked_protocol', return_value=locked_fixture()), \
             patch('implied_calibration.cli.load_locked_baselines', return_value=extension_fixture()), \
             redirect_stdout(captured):
            code = main()
        self.assertNotEqual(code, 1, captured.getvalue())
        return json.loads(captured.getvalue())

    @contextmanager
    def delayed_commit(self, table):
        real_connect = sqlite3.connect
        clock, terminal = self.clock, self.terminal

        class CommitBoundary(sqlite3.Connection):
            def __exit__(connection, exc_type, exc, traceback):
                crosses = (exc_type is None and connection.in_transaction and clock.wall < terminal
                           and connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] > 0)
                result = super().__exit__(exc_type, exc, traceback)
                if crosses:
                    clock.wall = terminal + 1
                return result

        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, factory=CommitBoundary)

        with patch('implied_calibration.store.sqlite3.connect', side_effect=connect):
            yield

    def check_terminal(self, svi_count):
        with patch('implied_calibration.research_plan.stopping_choices', side_effect=self.choices):
            inferred = self.cli('infer')
            report = build_report(self.store.path, locked_fixture(), extension_fixture(),
                                  self.terminal + 1000, COMMIT)
        self.assertEqual(inferred['as_of_ms'], self.terminal)
        self.assertEqual(inferred['primary_comparison']['n_pairs'], 0)
        self.assertEqual(report['as_of_ms'], self.terminal)
        self.assertEqual((report['n_svi_scores'], report['n_baseline_scores']), (svi_count, 0))

    def receipt(self, table):
        return json.loads(self.store.connection.execute(
            'SELECT payload_json FROM score_availability WHERE source_table=?', (table,)).fetchone()[0])

    def test_live_cli_svi_commit_after_terminal_is_withheld_by_cli_inference_and_report(self):
        with self.delayed_commit('score_records'):
            result = self.cli('score')
        score = next(record for record in result['scores'] if record['status'] == 'ok')
        self.assertEqual(score['created_at_ms'], self.terminal - 1)
        self.assertEqual(self.receipt('score_records')['available_at_ms'], self.terminal + 1)
        self.check_terminal(0)

    def test_live_cli_baseline_commit_after_terminal_is_withheld_by_cli_inference_and_report(self):
        score_database(self.store.path, locked_fixture(), EXPIRY + 200, EXPIRY + 200, COMMIT)
        with self.delayed_commit('baseline_scores'):
            result = self.cli('score')
        score = next(record for record in result['baselines']['scores'] if record['status'] == 'ok')
        self.assertEqual(score['created_at_ms'], self.terminal - 1)
        self.assertEqual(self.receipt('baseline_scores')['available_at_ms'], self.terminal + 1)
        self.check_terminal(1)

    def test_interrupted_receipt_is_withheld_and_recovery_uses_current_time(self):
        with patch('implied_calibration.evaluation.retain_score_availability', side_effect=RuntimeError('interrupted receipt')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted receipt'):
                score_database(self.store.path, locked_fixture(), self.clock.wall, self.clock.wall,
                               COMMIT, now_ms=self.clock.now_ms)
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM score_records').fetchone()[0], 1)
        self.clock.wall = self.terminal + 100
        self.check_terminal(0)
        self.cli('score')
        self.assertEqual(self.receipt('score_records')['available_at_ms'], self.clock.wall)
        self.check_terminal(0)

    def test_timely_receipts_remain_in_the_terminal_cohort(self):
        self.cli('score')
        self.clock.wall = self.terminal + 1
        with patch('implied_calibration.research_plan.stopping_choices', side_effect=self.choices):
            inferred = self.cli('infer')
            report = build_report(self.store.path, locked_fixture(), extension_fixture(), self.clock.wall, COMMIT)
        self.assertEqual(inferred['primary_comparison']['n_pairs'], 1)
        self.assertEqual((report['n_svi_scores'], report['n_baseline_scores']), (1, 1))

    def test_receipt_rows_are_append_only(self):
        self.cli('score')
        row = self.store.connection.execute('SELECT * FROM score_availability LIMIT 1').fetchone()
        for sql, parameters in (
                ('DELETE FROM score_availability', ()),
                ('UPDATE score_availability SET payload_json=payload_json', ()),
                ('INSERT OR REPLACE INTO score_availability VALUES (?,?,?)', tuple(row))):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(sql, parameters)
            self.store.connection.rollback()
