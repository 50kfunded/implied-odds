"""I check the complete frozen-baseline path and its immutable evaluation inputs."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
import sqlite3
import subprocess
from unittest.mock import patch

import numpy as np

from implied_calibration.baseline_evaluation import freeze_baselines, infer_database, score_baselines
from implied_calibration.baseline_lock import BaselineSettings, LockedBaselines, load_locked_baselines
from implied_calibration.evaluation import score_database
from implied_calibration.history import HOUR_MS, ingest_history
from implied_calibration.predictions import freeze_predictions
from implied_calibration.settlement import ingest_deliveries
from implied_calibration.store import canonical, digest
from test_baselines import garch_returns
from test_history import history_page
from test_phase2 import BASE, DatabaseFixture
from test_phase6 import COMMIT, DAY, EXPIRY, locked_fixture
import test_phase6 as phase6
from test_settlement import delivery_page


def extension_fixture():
    parent = locked_fixture()
    content = {'version':1,'parent_protocol_hash':parent.protocol.content_hash,'first_expiry_ms':EXPIRY,
               'settings':asdict(BaselineSettings())}
    return LockedBaselines(content,{'extension_hash':digest(content),'git_commit':'2'*40,'locked_at_ms':BASE},COMMIT,BASE+1000)


class FrozenBaselineTests(DatabaseFixture):
    seed_chain = phase6.FrozenForecastTests.seed_chain

    def freeze_all(self,stamp=None,**kwargs):
        stamp=EXPIRY-DAY if stamp is None else stamp
        return freeze_predictions(self.store.path,locked_fixture(),stamp,stamp,COMMIT,expiries=[EXPIRY],
                                  baseline_extension=extension_fixture(),**kwargs)

    def seed_history(self,cutoff=None):
        cutoff=EXPIRY-DAY if cutoff is None else cutoff
        last_close=cutoff//HOUR_MS*HOUR_MS
        returns=garch_returns()
        prices=(100*np.exp(np.r_[0.,np.cumsum(returns)])).tolist()
        self.write(history_page(last_close-len(prices)*HOUR_MS,prices,cutoff))
        ingest_history(self.recording,self.store.path,cutoff)

    def settle_score(self):
        self.write(delivery_page(EXPIRY+100))
        ingest_deliveries(self.recording,self.store.path,EXPIRY+100)
        score_database(self.store.path,locked_fixture(),EXPIRY+200,EXPIRY+200,COMMIT)
        return score_baselines(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+200,EXPIRY+200,COMMIT)

    def test_all_three_baselines_freeze_same_snapshot_and_score_same_delivery(self):
        identity=self.seed_chain()
        self.seed_history()
        result=self.freeze_all()
        baselines=[r for r in result['baselines'] if r['lead_ms']==DAY]
        self.assertEqual(len(baselines),3)
        self.assertEqual({r['status'] for r in baselines},{'ok'})
        self.assertEqual({r['snapshot_id'] for r in baselines},{identity})
        self.assertEqual({r['history_cutoff_ms'] for r in baselines},{EXPIRY-DAY})
        with patch('implied_calibration.baselines.garch_baseline',side_effect=AssertionError('Refit')):
            repeated=self.freeze_all(EXPIRY-DAY+1000)
        self.assertEqual(result['baselines'],repeated['baselines'])
        self.assertEqual(result['baseline_status_counts'],repeated['baseline_status_counts'])
        scores=self.settle_score()
        self.assertEqual(scores['added'],3)
        self.assertEqual({r['delivery']['delivery_price'] for r in scores['scores'] if r['status']=='ok'},{101.})
        self.assertEqual(score_baselines(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+300,EXPIRY+300,COMMIT)['added'],0)
        inferred=infer_database(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+300)
        self.assertEqual(inferred['status'],'insufficient data')
        self.assertEqual(inferred['primary_comparison']['n_pairs'],1)
        self.assertEqual(len(inferred['groups']),30)
        json.dumps(inferred,allow_nan=False)

    def test_short_history_is_frozen_and_cannot_be_backfilled(self):
        self.seed_chain()
        result=self.freeze_all()
        baselines=[r for r in result['baselines'] if r['lead_ms']==DAY]
        self.assertEqual(sorted(r['status'] for r in baselines),['ok','short_history','short_history'])
        self.seed_history()
        repeated=self.freeze_all(EXPIRY-DAY+1)
        self.assertEqual(result['baselines'],repeated['baselines'])

    def test_parent_forecast_without_baselines_is_not_backfilled(self):
        self.seed_chain()
        freeze_predictions(self.store.path,locked_fixture(),EXPIRY-DAY,EXPIRY-DAY,COMMIT,expiries=[EXPIRY])
        self.assertEqual(self.freeze_all()['baselines'],[])

    def test_baseline_completion_after_deadline_is_recorded_as_late(self):
        self.seed_chain()
        target=EXPIRY-DAY
        parent=freeze_predictions(self.store.path,locked_fixture(),target,target,COMMIT,expiries=[EXPIRY])
        prediction=next(r for r in parent['predictions'] if r['lead_ms']==DAY)
        clock=iter([target,target+600001]+[target+600001]*4)
        with self.store.connection:
            late=freeze_baselines(self.store.connection,prediction,extension_fixture(),locked_fixture(),target,COMMIT,lambda:next(clock))
        self.assertTrue(any(r['status']=='late_freeze' for r in late))
        self.assertTrue(all(r['distribution'] is None for r in late if r['status']=='late_freeze'))

    def test_future_history_and_scores_leave_earlier_inference_unchanged(self):
        self.seed_chain()
        self.freeze_all()
        cutoff=EXPIRY+10
        before=infer_database(self.store.path,extension_fixture(),locked_fixture(),cutoff)
        self.settle_score()
        after=infer_database(self.store.path,extension_fixture(),locked_fixture(),cutoff)
        self.assertEqual(before,after)
        scored=score_baselines(self.store.path,extension_fixture(),locked_fixture(),cutoff,cutoff,COMMIT)
        self.assertEqual(scored['added'],0)

    def test_terminal_inference_pins_score_availability_and_early_output_is_descriptive(self):
        from implied_calibration.report import build_report
        self.seed_chain()
        self.freeze_all()
        choices = {**locked_fixture().protocol.content['evaluation'], 'formal_decision_allowed': True,
                   'expiry_cutoff_ms': EXPIRY, 'final_analysis_at_ms': EXPIRY+10,
                   'stopping_amendment_hash': 'a'*64}
        with patch('implied_calibration.research_plan.stopping_choices',return_value=choices):
            before = infer_database(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+300)
            self.settle_score()
            after = infer_database(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+10000)
            self.assertEqual(before,after)
            self.assertEqual(after['as_of_ms'],EXPIRY+10)
            report = build_report(self.store.path,locked_fixture(),extension_fixture(),EXPIRY+10000,COMMIT)
            self.assertEqual(report['as_of_ms'],EXPIRY+10)
            self.assertEqual(report['n_svi_scores'],0)
        early = infer_database(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+300)
        self.assertEqual(early['primary_comparison']['decision_schedule'],'interim_descriptive')
        self.assertIsNone(early['primary_comparison']['bootstrap'])

    def test_historical_scores_are_repeatable_previews_without_persisted_rows(self):
        self.seed_chain()
        self.freeze_all()
        self.write(delivery_page(EXPIRY+100))
        ingest_deliveries(self.recording,self.store.path,EXPIRY+100)
        args=(self.store.path,locked_fixture(),EXPIRY+200,EXPIRY+210,COMMIT)
        first=score_database(*args)
        second=score_database(*args)
        self.assertEqual(first,second)
        self.assertEqual(first['mode'],'historical_preview')
        before=score_baselines(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+200,EXPIRY+210,COMMIT,parent_preview=first['scores'])
        after=score_baselines(self.store.path,extension_fixture(),locked_fixture(),EXPIRY+200,EXPIRY+210,COMMIT,parent_preview=second['scores'])
        self.assertEqual(before,after)
        self.assertEqual(before['added'],0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM score_records').fetchone()[0],0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM baseline_scores').fetchone()[0],0)

    def test_immutable_baselines_history_and_scores_block_replace_update_delete(self):
        self.seed_chain()
        self.seed_history()
        self.freeze_all()
        self.settle_score()
        for table in ('baseline_protocols','baseline_predictions','baseline_scores','history_sources','hourly_observations'):
            row=self.store.connection.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
            for sql,params in ((f'DELETE FROM {table}',()),
                               (f'UPDATE {table} SET {row.keys()[0]}={row.keys()[0]}',()),
                               (f'INSERT OR REPLACE INTO {table} VALUES ({",".join("?" for _ in row)})',tuple(row))):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(sql,params)
                self.store.connection.rollback()

    def test_extension_rejects_changed_parent_late_witness_bad_shape_and_boolean_version(self):
        fixture=extension_fixture()
        fixture.validate(locked_fixture())
        for field,value in (('version',True),('settings',[]),('parent_protocol_hash','0'*64)):
            content=deepcopy(fixture.content)
            content[field]=value
            broken=LockedBaselines(content,{**fixture.lock,'extension_hash':digest(content)},COMMIT,BASE+1000)
            with self.assertRaises(ValueError):
                broken.validate(locked_fixture())
        late=LockedBaselines(fixture.content,fixture.lock,COMMIT,EXPIRY-7*DAY)
        with self.assertRaisesRegex(ValueError,'before'):
            late.validate(locked_fixture())

    def test_real_git_baseline_lock_requires_matching_committed_settings_and_lock(self):
        folder=self.root/'protocol'
        folder.mkdir()
        path=folder/'baselines.json'
        fixture=extension_fixture()
        path.write_text(canonical(fixture.content)+'\n',encoding='utf-8',newline='\n')
        environment=dict(os.environ)
        environment.update({'GIT_AUTHOR_NAME':'50kfunded','GIT_COMMITTER_NAME':'50kfunded',
            'GIT_AUTHOR_EMAIL':'328319197+50kfunded@users.noreply.github.com',
            'GIT_COMMITTER_EMAIL':'328319197+50kfunded@users.noreply.github.com',
            'GIT_AUTHOR_DATE':'2026-01-01T00:00:00+00:00','GIT_COMMITTER_DATE':'2026-01-01T00:00:00+00:00'})
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],env=environment,stderr=subprocess.STDOUT,text=True).strip()
        git('init','--initial-branch=main')
        git('add','protocol/baselines.json')
        git('commit','-m','fix synthetic baseline choices')
        lock={**fixture.lock,'git_commit':git('rev-parse','HEAD'),'locked_at_ms':BASE+1000}
        lock_path=folder/'baselines.lock.json'
        lock_path.write_text(canonical(lock)+'\n',encoding='utf-8',newline='\n')
        with self.assertRaisesRegex(ValueError,'committed matching'):
            load_locked_baselines(path,locked_fixture())
        environment['GIT_AUTHOR_DATE']=environment['GIT_COMMITTER_DATE']='2026-01-01T00:00:02+00:00'
        git('add','protocol/baselines.lock.json')
        git('commit','-m','lock synthetic baseline choices')
        result=load_locked_baselines(path,locked_fixture())
        self.assertEqual(result.witness_commit,git('rev-parse','HEAD'))
        content=deepcopy(fixture.content)
        content['settings']['simulation_seed']+=1
        path.write_text(canonical(content)+'\n',encoding='utf-8',newline='\n')
        with self.assertRaises(ValueError):
            load_locked_baselines(path,locked_fixture())
