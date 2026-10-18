"""I test frozen forecasts and proper scores with synthetic data and no network."""

from copy import deepcopy
from contextlib import closing
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
from unittest.mock import patch


from implied_calibration.blackscholes import price_usd
from implied_calibration.evaluation import score_database
from implied_calibration.instruments import parse_name
from implied_calibration.locking import LockedProtocol, load_locked_protocol
from implied_calibration.predictions import freeze_predictions, select_snapshot
from implied_calibration.protocol import validate_protocol
from implied_calibration.settlement import ingest_deliveries
from implied_calibration.store import canonical, digest
from test_phase2 import BASE, DatabaseFixture, ingest, quote
from test_phase3 import draft
from test_settlement import delivery_page


EXPIRY = parse_name('BTC-5DEC26-100-C').expiry_ms
DAY = 86400000
COMMIT = '1' * 40


def locked_fixture():
    content = deepcopy(draft().content)
    content['evaluation']['first_expiry_ms'] = EXPIRY
    content['status'] = 'fixed'
    protocol = validate_protocol(content)
    return LockedProtocol(protocol, {'protocol_hash': protocol.content_hash, 'locked_at_ms': BASE,
                                     'first_expiry_ms': EXPIRY, 'git_commit': '2' * 40}, COMMIT, BASE + 1000)


class FrozenForecastTests(DatabaseFixture):
    def seed_chain(self, stamp=None):
        stamp = EXPIRY - DAY if stamp is None else stamp
        names = [f'BTC-5DEC26-{strike}-{"P" if strike<100 else "C"}' for strike in (95,97,99,100,101,103,105)]
        self.seed(names, stamp=stamp)
        rows = []
        for name in names:
            parsed = parse_name(name)
            mid = price_usd(100., parsed.strike, (EXPIRY-stamp)/(365*DAY), .6, parsed.option_type) / 100
            rows.append(quote(name, bid_price=mid*.99, ask_price=mid*1.01, mark_price=mid,
                              mark_iv=60., underlying_price=100.))
        identity = self.book(stamp=stamp, rows=rows)
        ingest(self.recording, self.store)
        return identity

    def freeze(self, now=None):
        now = EXPIRY-DAY if now is None else now
        return freeze_predictions(self.store.path, locked_fixture(), now, now, COMMIT, expiries=[EXPIRY])

    def test_freeze_stores_parameters_hashes_snapshot_maturity_and_never_recomputes(self):
        identity = self.seed_chain()
        first = self.freeze()
        row = next(r for r in first['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(row['snapshot_id'], identity)
        self.assertEqual(row['gap_ms'], 0)
        self.assertEqual(row['t_years'], 1/365)
        self.assertEqual(row['scoring_support']['policy'], 'collapse_tails_to_boundary_atoms')
        payload = {k:v for k,v in row.items() if k != 'content_hash'}
        self.assertEqual(digest(payload), row['content_hash'])
        with patch('implied_calibration.predictions.cdf_database', side_effect=AssertionError('Recomputed frozen forecast')):
            second = self.freeze(EXPIRY-DAY+1000)
        self.assertEqual(first['predictions'], second['predictions'])
        self.assertEqual(second['added'], 0)

    def test_future_rows_do_not_change_a_fresh_forecast(self):
        self.seed_chain()
        baseline = self.root/'baseline.sqlite'
        with closing(sqlite3.connect(baseline)) as other:
            self.store.connection.backup(other)
        self.seed_chain(EXPIRY-DAY+1)
        self.store.connection.execute('UPDATE instruments SET tick_size=1 WHERE fetched_at_ms>?', (EXPIRY-DAY,))
        self.store.connection.commit()
        args = (locked_fixture(), EXPIRY-DAY, EXPIRY-DAY, COMMIT)
        before = freeze_predictions(baseline, *args, expiries=[EXPIRY])
        after = freeze_predictions(self.store.path, *args, expiries=[EXPIRY])
        self.assertEqual(before['predictions'], after['predictions'])

    def test_latest_rejected_snapshot_is_not_replaced_by_an_older_good_one(self):
        older = self.seed_chain(EXPIRY-DAY-100)
        selected = self.book(stamp=EXPIRY-DAY)
        ingest(self.recording, self.store)
        result = self.freeze()
        row = next(r for r in result['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row['snapshot_id'], selected)
        self.assertNotEqual(row['snapshot_id'], older)
        self.assertNotEqual(row['status'], 'ok')

    def test_snapshot_tolerance_includes_the_boundary(self):
        self.seed_chain(EXPIRY-DAY-600000)
        row = next(r for r in self.freeze()['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row['gap_ms'], 600000)
        self.assertEqual(row['status'], 'ok')

    def test_stale_or_missing_snapshot_is_recorded_once(self):
        self.seed_chain(EXPIRY-DAY-600001)
        row = next(r for r in self.freeze()['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row['status'], 'no_snapshot')
        self.seed_chain(EXPIRY-DAY)
        again = next(r for r in self.freeze()['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row, again)

    def test_not_due_and_late_forecasts_are_not_backfilled(self):
        before = self.freeze(EXPIRY-DAY-1)
        self.assertFalse(any(r['lead_ms'] == DAY for r in before['predictions']))
        after = self.freeze(EXPIRY-DAY+600001)
        row = next(r for r in after['predictions'] if r['lead_ms'] == DAY)
        self.assertEqual(row['status'], 'late_freeze')

    def test_zero_snapshots_records_no_snapshot_and_does_not_backfill(self):
        empty = self.freeze(EXPIRY-7*DAY)
        row = next(r for r in empty['predictions'] if r['lead_ms'] == 7*DAY)
        self.assertEqual(row['status'], 'no_snapshot')
        self.assertIsNone(row['snapshot_id'])
        self.seed_chain(EXPIRY-7*DAY)
        self.assertEqual(next(r for r in self.freeze(EXPIRY-7*DAY)['predictions'] if r['lead_ms']==7*DAY), row)

    def test_seven_day_forecast_freezes_successfully(self):
        stamp = EXPIRY-7*DAY
        self.seed_chain(stamp)
        result = self.freeze(stamp)
        success = next(r for r in result['predictions'] if r['lead_ms']==7*DAY)
        self.assertEqual(success['status'],'ok')
        self.assertEqual(success['t_years'],7/365)

    def test_freeze_deadline_includes_exact_tolerance(self):
        self.seed_chain()
        row = next(r for r in self.freeze(EXPIRY-DAY+600000)['predictions'] if r['lead_ms']==DAY)
        self.assertEqual(row['status'],'ok')

    def test_computation_crossing_the_freeze_deadline_records_the_completion_time(self):
        self.seed_chain()
        target=EXPIRY-DAY
        result=freeze_predictions(self.store.path,locked_fixture(),target,target,COMMIT,
                                  expiries=[EXPIRY],now_ms=lambda:target+600001)
        row=next(r for r in result['predictions'] if r['lead_ms']==DAY)
        self.assertEqual(row['status'],'late_freeze')
        self.assertEqual(row['created_at_ms'],target+600001)
        self.assertIsNone(row['distribution'])

    def test_snapshot_query_excludes_target_plus_one(self):
        at = self.seed_chain()
        self.seed_chain(EXPIRY-DAY+1)
        row = select_snapshot(self.store.connection, EXPIRY-DAY, EXPIRY-DAY+100)
        self.assertEqual(row['snapshot_id'], at)

    def test_update_delete_and_replace_cannot_change_a_prediction(self):
        self.seed_chain()
        self.freeze()
        row = self.store.connection.execute('SELECT * FROM predictions LIMIT 1').fetchone()
        for sql, params in [("UPDATE predictions SET status='changed' WHERE prediction_id=?", (row['prediction_id'],)),
                            ('DELETE FROM predictions WHERE prediction_id=?', (row['prediction_id'],)),
                            ('INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?)', tuple(row))]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(sql, params)
            self.store.connection.rollback()
        self.assertEqual(tuple(self.store.connection.execute('SELECT * FROM predictions WHERE prediction_id=?', (row['prediction_id'],)).fetchone()), tuple(row))

    def test_missing_delivery_stays_pending_then_scores_without_refitting(self):
        self.seed_chain()
        frozen = self.freeze()
        pending = score_database(self.store.path, locked_fixture(), EXPIRY+100, EXPIRY+100, COMMIT)
        self.assertIn('no_delivery_price', pending['status_counts'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scores').fetchone()[0], 0)
        self.write(delivery_page(EXPIRY+101))
        ingest_deliveries(self.recording, self.store.path, EXPIRY+101)
        with patch('implied_calibration.predictions.cdf_database', side_effect=AssertionError('Scoring refitted a smile')):
            scored = score_database(self.store.path, locked_fixture(), EXPIRY+200, EXPIRY+200, COMMIT)
        self.assertEqual(scored['added'], 1)
        self.assertEqual(scored['class_and_lead_summary'][0]['expiry_class'], 'daily')
        self.assertEqual(scored['scores'][0]['score']['diagnostics']['log_score_axis'], 'ln_delivery_over_frozen_forward')
        again = score_database(self.store.path, locked_fixture(), EXPIRY+300, EXPIRY+300, '3'*40)
        self.assertEqual(scored['scores'], again['scores'])
        self.assertEqual(again['added'], 0)
        records = [json.loads(r[0]) for r in self.store.connection.execute('SELECT payload_json FROM predictions ORDER BY expiry_ms,lead_ms')]
        self.assertEqual(sorted(records,key=lambda r:r['lead_ms']), sorted(frozen['predictions'],key=lambda r:r['lead_ms']))

    def test_future_delivery_is_not_visible_to_earlier_scoring(self):
        self.seed_chain()
        self.freeze()
        self.write(delivery_page(EXPIRY+200))
        ingest_deliveries(self.recording, self.store.path, EXPIRY+200)
        result = score_database(self.store.path, locked_fixture(), EXPIRY+100, EXPIRY+100, COMMIT)
        self.assertEqual(result['added'], 0)
        self.assertIn('no_delivery_price', result['status_counts'])


class LockWitnessTests(DatabaseFixture):
    def test_lock_mutation_and_late_witness_are_rejected(self):
        locked = locked_fixture()
        locked.validate()
        with self.assertRaises(ValueError):
            replace(locked, witness_at_ms=EXPIRY).validate()
        locked.protocol.content['cleaning']['minimum_maturity_hours'] = 5
        with self.assertRaises(ValueError):
            locked.validate()

    def test_real_git_loader_requires_committed_matching_lock(self):
        folder = self.root/'protocol'
        folder.mkdir()
        protocol_path = folder/'protocol.json'
        locked = locked_fixture()
        protocol_path.write_text(canonical(locked.protocol.content)+'\n',encoding='utf-8',newline='\n')
        environment = dict(os.environ)
        environment.update({'GIT_AUTHOR_NAME':'50kfunded','GIT_COMMITTER_NAME':'50kfunded',
                            'GIT_AUTHOR_EMAIL':'328319197+50kfunded@users.noreply.github.com',
                            'GIT_COMMITTER_EMAIL':'328319197+50kfunded@users.noreply.github.com',
                            'GIT_AUTHOR_DATE':'2026-01-01T00:00:00+00:00',
                            'GIT_COMMITTER_DATE':'2026-01-01T00:00:00+00:00'})
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],env=environment,stderr=subprocess.STDOUT,text=True).strip()
        git('init','--initial-branch=main')
        git('add','protocol/protocol.json')
        git('commit','-m','fix synthetic protocol')
        lock = dict(locked.lock,git_commit=git('rev-parse','HEAD'),locked_at_ms=BASE+1000)
        (folder/'protocol.lock.json').write_text(canonical(lock)+'\n',encoding='utf-8',newline='\n')
        with self.assertRaisesRegex(ValueError,'committed matching'):
            load_locked_protocol(protocol_path)
        environment['GIT_AUTHOR_DATE']=environment['GIT_COMMITTER_DATE']='2026-01-01T00:00:02+00:00'
        git('add','protocol/protocol.lock.json')
        git('commit','-m','lock synthetic protocol')
        result=load_locked_protocol(protocol_path)
        self.assertEqual(result.witness_commit,git('rev-parse','HEAD'))
        self.assertEqual(result.protocol.content_hash,locked.protocol.content_hash)
        (folder/'protocol.lock.json').write_text('{}\n',encoding='utf-8',newline='\n')
        with self.assertRaisesRegex(ValueError,'lock fields'):
            load_locked_protocol(protocol_path)
