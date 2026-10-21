"""Paired variance forecasts are frozen before outcomes; missed dates are counted."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import numpy as np

from .client import ClientError, PublicClient, utc_ms, validate_response
from .collection import FileLease, write_status
from .historical_data import DVOL, PRICES, fetch_dvol, load_study, read_study
from .history import fetch_history
from .incremental import build_cases, features_at, fit_forecasts, risk_budget
from .raw import RawWriter, read_raw
from .research_plan import load_plan
from .store import digest
from .variance_study import DAY_MS, HOUR_MS, _metric_summary, hourly_variance, qlike


class ForecastUnavailable(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def write_immutable(path: Path, payload: dict) -> dict:
    record = {**payload, 'content_hash': digest(payload)}
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        claimed = old.pop('content_hash')
        if digest(old) != claimed or old != payload:
            raise ValueError('Refusing to change an immutable prospective record')
        return record
    with path.open('x',encoding='utf-8',newline='\n') as handle:
        handle.write(json.dumps(record,indent=2,sort_keys=True,allow_nan=False)+'\n')
        handle.flush()
        os.fsync(handle.fileno())
    return record


def read_immutable(path: Path) -> dict:
    record = json.loads(path.read_text(encoding='utf-8'))
    body = {k:v for k,v in record.items() if k != 'content_hash'}
    if record.get('content_hash') != digest(body):
        raise ValueError('Detected a changed frozen variance record')
    return record


def freeze_record(cases: list, features: tuple, plan: dict, origin: int, now: int, proof: dict, *, completed_now_ms=None) -> dict:
    future = plan['future']
    if (not future['origin_start_ms'] <= origin <= future['origin_end_ms']
            or (origin-future['origin_start_ms']) % DAY_MS
            or not origin-future['freeze_start_before_origin_ms'] <= now <= origin-future['freeze_end_before_origin_ms']):
        raise ValueError('Freezing requires the declared pre-target window; backfill is prohibited')
    cutoff = origin-future['feature_cutoff_before_origin_ms']
    if (proof.get('feature_cutoff_ms') != cutoff or proof.get('latest_fetch_ms',now+1) > now
            or proof.get('daily_close') != proof.get('last_hourly_close') or proof.get('daily_close') is None):
        raise ValueError('Expected retained matching DVOL closes before freezing')
    if not math.isclose(features[3],(proof['daily_close']/100)**2,rel_tol=1e-12):
        raise ValueError('Expected features matching the retained DVOL close')
    fitted = fit_forecasts(cases,cutoff,features,plan)
    if fitted is None:
        raise ValueError('Expected 365 completed matched training outcomes')
    completed = completed_now_ms() if completed_now_ms is not None else now
    if not now <= completed <= origin-future['freeze_end_before_origin_ms']:
        raise ForecastUnavailable('Cannot backdate a fit completed after the freeze window')
    return {'schema_version':1,'kind':'prospective_incremental_variance','plan_hash':digest(plan),
            'origin_ms':origin,'feature_cutoff_ms':cutoff,'target_end_ms':origin+30*DAY_MS,
            'frozen_at_ms':completed,'features':list(features),'source_proof':proof,**fitted}


def publish_forecast(directory: Path, payload: dict, plan: dict, now_ms) -> dict:
    origin = payload['origin_ms']
    record = write_immutable(directory/'forecasts'/f'{origin}.json',payload)
    completed = now_ms()
    receipt = {'origin_ms':origin,'forecast_hash':record['content_hash'],'durable_at_ms':completed,
               'status':'accepted' if payload['frozen_at_ms'] <= completed <= origin-plan['future']['freeze_end_before_origin_ms'] else 'late_durable_write'}
    write_immutable(directory/'receipts'/f'{origin}.json',receipt)
    return record


def recover_forecast_receipt(directory: Path, record: dict, plan: dict, now_ms) -> None:
    origin = record['origin_ms']
    now = now_ms()
    future = plan['future']
    if not origin-future['freeze_start_before_origin_ms'] <= now <= origin-future['freeze_end_before_origin_ms']:
        raise ForecastUnavailable('Cannot recover a missing receipt outside the freeze window')
    publish_forecast(directory,{k:v for k,v in record.items() if k != 'content_hash'},plan,now_ms)


def verify_source_proof(record: dict, directory: Path, expected_seed_hash: str) -> None:
    proof = record['source_proof']
    files = proof.get('raw_files')
    close = proof.get('daily_close')
    if (proof.get('seed_manifest_hash') != expected_seed_hash
            or proof.get('feature_cutoff_ms') != record['feature_cutoff_ms']
            or record['feature_cutoff_ms'] != record['origin_ms']-HOUR_MS
            or type(proof.get('latest_fetch_ms')) is not int or proof['latest_fetch_ms'] > record['frozen_at_ms']
            or not isinstance(proof.get('git_commit'),str) or re.fullmatch('[0-9a-f]{40}',proof['git_commit']) is None
            or not isinstance(close,(int,float)) or not math.isfinite(close) or close <= 0
            or close != proof.get('last_hourly_close') or not math.isclose(record['features'][3],(close/100)**2,rel_tol=1e-12)
            or not isinstance(files,dict) or not files):
        raise ValueError('Detected an invalid frozen source proof')
    for relative, expected in files.items():
        path = (directory/relative).resolve()
        if (not path.is_relative_to(directory.resolve()) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('Detected a missing or changed frozen raw source')


def refresh_start(mapping: dict, duration: int, end_ms: int) -> int:
    lower = max(min(mapping),end_ms-430*DAY_MS)
    first_gap = next((opened for opened in range(lower,end_ms,duration) if opened not in mapping),end_ms)
    return min(max(mapping)-2*duration,first_gap)


def forward_summary(directory: Path, plan: dict, now: int, guard: dict) -> dict:
    seed_path = Path(__file__).resolve().parents[2]/'recordings/historical-variance/manifest.json'
    expected_seed_hash = digest(json.loads(seed_path.read_text(encoding='utf-8')))
    forecasts = [read_immutable(p) for p in sorted((directory/'forecasts').glob('*.json'))]
    availability = min(now,plan['future']['analysis_at_ms'])
    forecasts = [r for r in forecasts if r['frozen_at_ms'] <= availability]
    scores = [read_immutable(p) for p in sorted((directory/'scores').glob('*.json'))]
    scores = [r for r in scores if r['scored_at_ms'] <= availability]
    retained_forecasts = forecasts
    accepted = []
    rejected = []
    for record in forecasts:
        receipt_path = directory/'receipts'/f"{record['origin_ms']}.json"
        receipt = read_immutable(receipt_path) if receipt_path.exists() else None
        if (receipt is not None and receipt['forecast_hash'] == record['content_hash'] and receipt['status'] == 'accepted'
                and record['frozen_at_ms'] <= receipt['durable_at_ms'] <= min(availability,record['origin_ms']-plan['future']['freeze_end_before_origin_ms'])):
            accepted.append(record)
        else:
            rejected.append({'origin_ms':record['origin_ms'],'reason':'missing_or_late_durability_receipt'})
    forecasts = accepted
    by_origin = {r['origin_ms']:r for r in forecasts}
    if len(by_origin) != len(forecasts):
        raise ValueError('Detected duplicate prospective origins')
    future = plan['future']
    for forecast in forecasts:
        origin = forecast['origin_ms']
        if (forecast['plan_hash'] != digest(plan) or not future['origin_start_ms'] <= origin <= future['origin_end_ms']
                or (origin-future['origin_start_ms']) % DAY_MS
                or not origin-future['freeze_start_before_origin_ms'] <= forecast['frozen_at_ms'] <= origin-future['freeze_end_before_origin_ms']
                or forecast['training']['latest_target_end_ms'] > forecast['feature_cutoff_ms']):
            raise ValueError('Detected an invalid prospective freeze or training cutoff')
        verify_source_proof(forecast,directory,expected_seed_hash)
    if len({r['origin_ms'] for r in scores}) != len(scores):
        raise ValueError('Detected duplicate prospective scores')
    durable_scores = []
    for score in scores:
        receipt_path = directory/'score-receipts'/f"{score['origin_ms']}.json"
        receipt = read_immutable(receipt_path) if receipt_path.exists() else None
        if (receipt is not None and receipt['score_hash'] == score['content_hash']
                and score['scored_at_ms'] <= receipt['durable_at_ms'] <= availability):
            durable_scores.append(score)
    scores = durable_scores
    for score in scores:
        linked_forecast = by_origin.get(score['origin_ms'])
        if (linked_forecast is None or score['forecast_hash'] != linked_forecast['content_hash']
                or score['scored_at_ms'] < linked_forecast['target_end_ms'] or linked_forecast['frozen_at_ms'] >= linked_forecast['origin_ms']):
            raise ValueError('Expected hash-linked forecasts frozen before outcomes')
    scheduled = list(range(future['origin_start_ms'],future['origin_end_ms']+DAY_MS,DAY_MS))
    due = [t for t in scheduled if now > t-future['freeze_end_before_origin_ms']]
    missing = [t for t in due if t not in by_origin]
    terminal = now >= future['analysis_at_ms']
    result = {'kind':'prospective_variance_scoreboard','as_of_ms':now,'plan_hash':digest(plan),
              'n_planned':len(scheduled),'n_due':len(due),'n_frozen':len(forecasts),'n_scored':len(scores),
              'n_retained_forecast_attempts':len(retained_forecasts),'rejected_forecasts':rejected,
              'missing_origins':missing,'n_missing':len(missing),'terminal':terminal,
              'formal_claim_eligible':False,'conclusion':'Fixed terminal date pending.',
              'inference_guard_hash':digest(guard),'frozen_hashes':[{'origin_ms':r['origin_ms'],'hash':r['content_hash']} for r in forecasts],
              'individual_scores':scores,'anchor_scope':'Local hashes are retained; an independent public timestamp is still needed.'}
    if terminal:
        result['conclusion'] = 'inconclusive: formal inference remains disabled under the validation amendment'
        if len(scores) == len(scheduled) and {r['origin_ms'] for r in scores} == set(scheduled):
            values = np.array([[r['losses']['har_dvol']-r['losses']['har']] for r in sorted(scores,key=lambda r:r['origin_ms'])])
            result['descriptive_terminal'] = _metric_summary(values,np.array(scheduled),['qlike_har_dvol_minus_har'],plan)
    return result


class VarianceForwardJob:
    def __init__(self, root: Path, directory: Path, *, client_factory=PublicClient, now_ms=utc_ms, sleep=time.sleep):
        self.root, self.directory = Path(root), Path(directory)
        self.plan = load_plan(self.root/'protocol/incremental-variance-v1.json')
        self.guard = load_plan(self.root/'protocol/inference-validation-v1.json')
        if self.guard['parent_plan_sha256'] != digest(self.plan) or self.guard['formal_claims_enabled'] is not False:
            raise ValueError('Expected the matching conservative inference amendment')
        historical = load_study(self.root/'protocol/historical-variance.json')
        inputs = read_study(self.root/'recordings/historical-variance/manifest.json',historical)
        self.prices, self.dvol = dict(inputs.prices), dict(inputs.dvol)
        self.seed_hash = digest(inputs.manifest)
        self.first_cutoff = historical['origin_start_ms']
        self.client_factory, self.now_ms, self.sleep = client_factory, now_ms, sleep
        self.hourly_dvol: dict = {}
        self.latest_fetch_ms = inputs.manifest['captured_until_ms']
        self.directory.mkdir(parents=True,exist_ok=True)
        self.writer: RawWriter | None = None
        self.state = {'status':'starting','interval_seconds':300,'cycles':0,'completed_cycles':0,'failed_cycles':0,
                      'started_at_ms':self.now_ms(),'plan_hash':digest(self.plan)}
        for path in sorted((self.directory/'captures').rglob('*.jsonl.gz')):
            issues: list = []
            for record in read_raw(path,on_issue=issues.append):
                self.ingest(record)
            if issues:
                raise ValueError('Detected a corrupt retained prospective response')

    def ingest(self, row: dict) -> None:
        if not row.get('ok'):
            return
        fetched = row['fetched_at_ms']
        if fetched > self.now_ms():
            raise ValueError('Cannot ingest a response from the future')
        result = validate_response(row['endpoint'],row['http_status'],row['body'])
        params = row['params']
        if row['endpoint'] == PRICES:
            if params.get('resolution') != '60' or params.get('instrument_name') != 'BTC-PERPETUAL':
                raise ValueError('Expected the declared perpetual hourly source')
            candles = zip(result.get('ticks',[]),result.get('open',[]),result.get('high',[]),result.get('low',[]),result.get('close',[]))
            destination, duration = self.prices, HOUR_MS
        elif row['endpoint'] == DVOL and params.get('currency') == 'BTC':
            candles = result['data']
            destination = self.dvol if params['resolution'] == '1D' else self.hourly_dvol
            duration = DAY_MS if params['resolution'] == '1D' else HOUR_MS
        else:
            raise ValueError('Detected an unexpected prospective source')
        for opened,*values in candles:
            if opened+duration > fetched:
                continue
            if opened % duration or not params['start_timestamp'] <= opened <= params['end_timestamp']:
                raise ValueError('Detected an invalid prospective candle clock')
            a,b,c,d = (float(v) for v in values)
            candle = (a,b,c,d)
            if opened in destination and destination[opened][3] != candle[3]:
                raise ValueError('Conflicting close revisions are a fatal source failure')
            destination[opened] = candle
        self.latest_fetch_ms = max(self.latest_fetch_ms,fetched)

    def retain(self, attempt) -> None:
        record = attempt.to_dict()
        if self.writer is None:
            raise ValueError('Expected a fresh immutable capture segment')
        self.writer.append(record)
        self.ingest(record)

    def cycle(self) -> dict:
        now = self.now_ms()
        cutoff = now//DAY_MS*DAY_MS
        self.writer = RawWriter(self.directory/'captures'/f'{now}-{uuid4().hex}'/'raw')
        client = self.client_factory(on_attempt=self.retain)
        try:
            fetch_history(client,refresh_start(self.prices,HOUR_MS,now//HOUR_MS*HOUR_MS),now//HOUR_MS*HOUR_MS,sleep=self.sleep)
            self.sleep(10.)
            fetch_dvol(client,refresh_start(self.dvol,DAY_MS,cutoff),cutoff,sleep=self.sleep)
            self.sleep(10.)
            fetch_dvol(client,cutoff-DAY_MS,cutoff,resolution='3600',sleep=self.sleep)
        finally:
            client.close()
        now = self.now_ms()
        cutoff = now//DAY_MS*DAY_MS
        origin = cutoff+HOUR_MS
        future = self.plan['future']
        path = self.directory/'forecasts'/f'{origin}.json'
        if (path.exists() and not (self.directory/'receipts'/path.name).exists()
                and origin-future['freeze_start_before_origin_ms'] <= now <= origin-future['freeze_end_before_origin_ms']):
            record = read_immutable(path)
            verify_source_proof(record,self.directory,self.seed_hash)
            recover_forecast_receipt(self.directory,record,self.plan,self.now_ms)
        if (future['origin_start_ms'] <= origin <= future['origin_end_ms'] and not path.exists()
                and origin-future['freeze_start_before_origin_ms'] <= now <= origin-future['freeze_end_before_origin_ms']):
            features = features_at(self.prices,self.dvol,cutoff)
            if features is None:
                raise ForecastUnavailable('Incomplete midnight features may be retried within the freeze window')
            daily, hourly = self.dvol.get(cutoff-DAY_MS), self.hourly_dvol.get(cutoff-HOUR_MS)
            if daily is None or hourly is None or daily[3] != hourly[3]:
                raise ForecastUnavailable('An unavailable or unmatched DVOL close may be retried within the freeze window')
            proof = {'feature_cutoff_ms':cutoff,'latest_fetch_ms':self.latest_fetch_ms,'seed_manifest_hash':self.seed_hash,
                     'daily_close':daily[3] if daily else None,'last_hourly_close':hourly[3] if hourly else None,
                     'git_commit':self.state.get('git_commit'),
                     'raw_files':{p.relative_to(self.directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted((self.directory/'captures').rglob('*.jsonl.gz'))}}
            cases = build_cases(self.prices,self.dvol,max(self.first_cutoff,cutoff-430*DAY_MS),cutoff,target_shift_hours=1)
            payload = freeze_record(cases,features,self.plan,origin,now,proof,completed_now_ms=self.now_ms)
            publish_forecast(self.directory,payload,self.plan,self.now_ms)
        for forecast_path in sorted((self.directory/'forecasts').glob('*.json')):
            forecast = read_immutable(forecast_path)
            receipt_path = self.directory/'receipts'/forecast_path.name
            if not receipt_path.exists() or read_immutable(receipt_path)['status'] != 'accepted':
                continue
            score_path = self.directory/'scores'/forecast_path.name
            score_receipt_path = self.directory/'score-receipts'/forecast_path.name
            if score_path.exists() and not score_receipt_path.exists():
                score = read_immutable(score_path)
                if score['forecast_hash'] != forecast['content_hash'] or score['scored_at_ms'] < forecast['target_end_ms']:
                    raise ValueError('Cannot recover a score with invalid provenance')
                write_immutable(score_receipt_path,{'origin_ms':forecast['origin_ms'],'score_hash':score['content_hash'],
                                                   'durable_at_ms':self.now_ms()})
            if now < forecast['target_end_ms'] or score_path.exists():
                continue
            actual,_ = hourly_variance(self.prices,forecast['origin_ms'],forecast['target_end_ms'],8760)
            if actual is None:
                continue
            losses = {name:qlike(actual,value) for name,value in forecast['forecasts'].items()}
            decisions = {name:risk_budget(actual,value,self.plan) for name,value in forecast['forecasts'].items()}
            scored_at = self.now_ms()
            score = write_immutable(score_path,{'origin_ms':forecast['origin_ms'],'forecast_hash':forecast['content_hash'],
                'scored_at_ms':scored_at,'actual':actual,'losses':losses,'decisions':decisions})
            write_immutable(self.directory/'score-receipts'/forecast_path.name,
                            {'origin_ms':forecast['origin_ms'],'score_hash':score['content_hash'],'durable_at_ms':self.now_ms()})
        result = forward_summary(self.directory,self.plan,self.now_ms(),self.guard)
        write_status(self.directory/'scoreboard.json',result)
        return result

    def run(self, seconds: int) -> dict:
        from .cli import provenance
        details = provenance()
        if details['working_tree_dirty'] is not False or details['git_commit'] is None:
            raise ValueError('Expected committed code before live collection')
        stop = time.monotonic()+seconds
        with FileLease(self.directory/'.variance-collector.lock'):
            try:
                while time.monotonic() < stop and not (self.directory/'STOP').exists():
                    self.state.update(status='collecting',updated_at_ms=self.now_ms(),git_commit=details['git_commit'])
                    write_status(self.directory/'status.json',self.state)
                    self.state['cycles'] += 1
                    try:
                        self.cycle()
                    except (ClientError,ForecastUnavailable) as exc:
                        self.state.update(failed_cycles=self.state['failed_cycles']+1,last_error=exc.reason)
                    else:
                        self.state['completed_cycles'] += 1
                    self.state.update(status='waiting',updated_at_ms=self.now_ms())
                    write_status(self.directory/'status.json',self.state)
                    until = min(stop,time.monotonic()+300)
                    while time.monotonic() < until and not (self.directory/'STOP').exists():
                        self.sleep(min(1.,until-time.monotonic()))
                self.state['status'] = 'stopped'
                return self.state
            except BaseException:
                self.state.update(status='failed',failed_cycles=self.state['failed_cycles']+1)
                raise
            finally:
                self.state['updated_at_ms'] = self.now_ms()
                write_status(self.directory/'status.json',self.state)
