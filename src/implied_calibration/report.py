"""Offline reports read frozen research records without changing the database."""

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import base64
import hashlib
from html import escape
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
import sys

from .baseline_lock import LockedBaselines
from .baselines import BaselineCDF
from .cleaning import clean_database
from .coverage import coverage_report
from .locking import LockedProtocol
from .predictions import checked_prediction
from .report_stats import ReportObservation, report_statistics, tail_events
from .scoring import FrozenCDF
from .store import digest, score_available


def report_versions() -> dict:
    return {'python':'.'.join(map(str,sys.version_info[:3])), 'sqlite':sqlite3.sqlite_version,
            **{name:version(name) for name in ('numpy','scipy','matplotlib','arch')}}


def _records(connection: sqlite3.Connection, table: str, as_of_ms: int) -> list[dict]:
    allowed = {'predictions','baseline_predictions','score_records','baseline_scores'}
    if table not in allowed:
        raise ValueError('Unknown report table')
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:
        return []
    rows = connection.execute(f"SELECT payload_json FROM {table} WHERE json_extract(payload_json,'$.created_at_ms')<=?",
                               (as_of_ms,)).fetchall()
    output = [checked_prediction(row[0]) for row in rows]
    if table in ('score_records', 'baseline_scores'):
        output = [record for record in output if score_available(connection, table, record, as_of_ms)]
    assert all(r['created_at_ms'] <= as_of_ms for r in output)
    return output


def build_report(db: Path, parent: LockedProtocol, extension: LockedBaselines, as_of_ms: int | None,
                 git_commit: str, *, clean_cache_path: Path | None = None) -> dict:
    extension.validate(parent)
    connection = sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if as_of_ms is None:
            times = [0]
            for table in ('raw_records','heartbeat','delivery_prices','history_sources'):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    times.append(connection.execute(f'SELECT COALESCE(MAX(fetched_at_ms),0) FROM {table}').fetchone()[0])
            for table in ('predictions','baseline_predictions','score_records','baseline_scores'):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    times.append(connection.execute(f"SELECT COALESCE(MAX(json_extract(payload_json,'$.created_at_ms')),0) FROM {table}").fetchone()[0])
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='score_availability'").fetchone():
                times.append(connection.execute("SELECT COALESCE(MAX(json_extract(payload_json,'$.available_at_ms')),0) FROM score_availability").fetchone()[0])
            as_of_ms = max(times)
        if type(as_of_ms) is not int or not 0 <= as_of_ms <= 9223372036854775807:
            raise ValueError('Expected a nonnegative integer report cutoff')
        from .research_plan import stopping_choices
        decision_choices = stopping_choices(parent.protocol.content['evaluation'], parent.protocol.content_hash,
                                             extension.content_hash, as_of_ms)
        if decision_choices['formal_decision_allowed']:
            as_of_ms = min(as_of_ms, decision_choices['final_analysis_at_ms'])
        predictions = {r['prediction_id']:r for r in _records(connection,'predictions',as_of_ms)
                       if r['protocol_hash'] == parent.protocol.content_hash}
        baselines = {r['baseline_id']:r for r in _records(connection,'baseline_predictions',as_of_ms)
                     if r['extension_hash'] == extension.content_hash}
        svi_scores = {r['prediction_id']:r for r in _records(connection,'score_records',as_of_ms)
                      if r['protocol_hash'] == parent.protocol.content_hash}
        base_scores = [r for r in _records(connection,'baseline_scores',as_of_ms)
                       if r['extension_hash'] == extension.content_hash and r['prediction_id'] in svi_scores]
        deliveries = {r[0] for r in connection.execute('SELECT expiry_ms FROM delivery_prices WHERE fetched_at_ms<=?',(as_of_ms,))}
        issues = dict(connection.execute('SELECT i.reason,COUNT(*) FROM issues i LEFT JOIN raw_records r ON r.record_id=i.source_id '
            'WHERE i.fetched_at_ms<=? OR (i.fetched_at_ms IS NULL AND r.fetched_at_ms<=?) GROUP BY i.reason ORDER BY i.reason',
            (as_of_ms,as_of_ms)).fetchall())
    finally:
        connection.close()
    cleaned = clean_database(db,parent.protocol,as_of_ms,cache_path=clean_cache_path)
    if any(q['status'] == 'rejected' and not q['reasons'] for g in cleaned['groups'] for q in g['quotes']):
        raise ValueError('Expected a recorded reason for every rejected report quote')
    observations, skipped = [], Counter()
    baseline_models = {}
    for identity, baseline in baselines.items():
        prediction = predictions.get(baseline['prediction_id'])
        if (prediction is None or baseline['prediction_content_hash'] != prediction['content_hash']
                or any(baseline[key] != prediction[key] for key in ('expiry_ms','lead_ms','snapshot_id'))):
            raise ValueError('Baseline report forecast provenance differs')
        if baseline['status'] == 'ok':
            distribution = BaselineCDF(**baseline['distribution'])
            support = prediction['scoring_support']
            if (prediction['status'] != 'ok' or distribution.forward_usd != prediction['forward_usd']
                    or distribution.lower_usd != support['lower_usd'] or distribution.upper_usd != support['upper_usd']
                    or distribution.support_record() != baseline['scoring_support']):
                raise ValueError('Baseline report scoring bounds differ')
            baseline_models[identity] = distribution
    for identity, score in sorted(svi_scores.items()):
        prediction = predictions.get(identity)
        if prediction is None or prediction['status'] != 'ok' or score['status'] != 'ok':
            raise ValueError('Expected the matching accepted frozen prediction for every SVI score')
        if (score['prediction_content_hash'] != prediction['content_hash']
                or any(score[key] != prediction[key] for key in ('expiry_ms','lead_ms')) or score['expiry_ms'] > as_of_ms
                or score['delivery']['fetched_at_ms'] > as_of_ms):
            raise ValueError('SVI score and forecast provenance or delivery cutoff differ')
        distribution = FrozenCDF.from_record(prediction)
        observations.append(ReportObservation(identity,score['expiry_ms'],score['lead_ms'],score['score']['expiry_class'],
            'svi',score['score']['metrics'],score['score']['diagnostics'],tail_events(distribution,score['delivery']['delivery_price'])))
    for score in sorted(base_scores,key=lambda r:r['baseline_id']):
        baseline = baselines.get(score['baseline_id'])
        prediction = predictions.get(score['prediction_id'])
        svi_score = svi_scores.get(score['prediction_id'])
        if baseline is None or prediction is None or svi_score is None:
            raise ValueError('Expected all hash-linked records for a baseline report score')
        if (baseline['status'] != 'ok' or score['status'] != 'ok'
                or baseline['prediction_content_hash'] != prediction['content_hash']
                or score['baseline_content_hash'] != baseline['content_hash']
                or score['svi_score_content_hash'] != svi_score['content_hash'] or score['delivery'] != svi_score['delivery']
                or score['baseline_name'] != baseline['baseline_name']
                or any(score[key] != prediction[key] for key in ('expiry_ms','lead_ms'))
                or score['expiry_ms'] > as_of_ms or score['delivery']['fetched_at_ms'] > as_of_ms):
            raise ValueError('Baseline report provenance differs')
        distribution = baseline_models[score['baseline_id']]
        observations.append(ReportObservation(score['prediction_id'],score['expiry_ms'],score['lead_ms'],score['score']['expiry_class'],
            score['baseline_name'],score['score']['metrics'],score['score']['diagnostics'],tail_events(distribution,score['delivery']['delivery_price'])))
    for baseline in baselines.values():
        if baseline['baseline_id'] not in {r['baseline_id'] for r in base_scores}:
            skipped[baseline['status'] if baseline['status'] != 'ok' else 'unscored_baseline'] += 1
    lead = parent.protocol.content['evaluation']['primary_lead_ms']
    candidates = sorted((r for r in predictions.values() if r['status'] == 'ok' and r['lead_ms'] == lead),
        key=lambda r:(r['prediction_id'] not in svi_scores,r['expiry_ms'],r['prediction_id']))
    chart = None
    if candidates:
        selected = candidates[0]
        group = next((g for g in cleaned['groups'] if g['snapshot_id'] == selected['snapshot_id']
                      and g['expiry_ms'] == selected['expiry_ms']),None)
        quotes = [q for q in group['quotes'] if q['status'] == 'usable'] if group else []
        chart = {'prediction_id':selected['prediction_id'],'snapshot_id':selected['snapshot_id'],
                 'expiry_ms':selected['expiry_ms'],'lead_ms':selected['lead_ms'],'forward_usd':selected['forward_usd'],
                 't_years':selected['t_years'],'params':selected['params'],
                 'distribution':selected['distribution'],'scoring_support':selected['scoring_support'],
                 'k':[q['k'] for q in quotes],'iv_bid':[q['iv_bid'] for q in quotes],'iv_ask':[q['iv_ask'] for q in quotes],
                 'delivery_usd':svi_scores[selected['prediction_id']]['delivery']['delivery_price'] if selected['prediction_id'] in svi_scores else None,
                 'baselines':{r['baseline_name']:r['distribution'] for r in baselines.values()
                              if r['prediction_id'] == selected['prediction_id'] and r['status'] == 'ok'}}
    summary = {k:v for k,v in cleaned.items() if k not in ('groups','protocol')}
    missing_svi = Counter('not_settled' if r['expiry_ms'] > as_of_ms else 'no_delivery_price'
        if r['expiry_ms'] not in deliveries else 'score_not_computed' for r in predictions.values()
        if r['status'] == 'ok' and r['prediction_id'] not in svi_scores)
    statistics = report_statistics(observations,decision_choices,parent.protocol.content['scoring']['pit_transform_epsilon'])
    return {'schema_version':1,'synthetic':False,'dataset_label':'Retained public-market recording and frozen research records',
            'status':statistics['status'],'as_of_ms':as_of_ms,'protocol_hash':parent.protocol.content_hash,
            'extension_hash':extension.content_hash,'provenance':{'git_commit':git_commit,'versions':report_versions()},
            'capture':summary,'coverage':coverage_report(db,as_of_ms),'ingestion_issue_counts':issues,
            'undated_issue_policy':"Undated issues are attributed only when their retained source has a bounded fetch timestamp.",
            'prediction_status_counts':dict(sorted(Counter(r['status'] for r in predictions.values()).items())),
            'baseline_status_counts':dict(sorted(Counter(r['status'] for r in baselines.values()).items())),
            'missing_baseline_score_counts':dict(sorted(skipped.items())), 'n_svi_scores':len(svi_scores),
            'missing_svi_score_counts':dict(sorted(missing_svi.items())),
            'n_accepted_frozen_forecasts':sum(r['status'] == 'ok' for r in predictions.values()),
            'n_baseline_scores':len(base_scores),'statistics':statistics,'chart':chart,
            'observations':[asdict(r) for r in sorted(observations,key=lambda r:(r.expiry_ms,r.lead_ms,r.model))],
            'checklist':{'protocol_lock_precedes_first_decision':parent.witness_at_ms+999 < parent.lock['first_expiry_ms']-max(parent.protocol.content['evaluation']['lead_times_ms']),
                         'baseline_lock_precedes_first_decision':extension.witness_at_ms+999 < extension.content['first_expiry_ms']-max(parent.protocol.content['evaluation']['lead_times_ms']),
                         'raw_retention':'Retained files are verified separately; SQLite alone cannot prove raw retention.',
                         'quote_reasons_counted':sum(summary['quote_status_counts'].values()) == summary['n_quotes']}}


def write_report(data: dict, output: Path) -> dict:
    from .report_plots import report_figures

    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    from matplotlib import rc_context, rcdefaults
    hashes, panels = {}, []
    with rc_context():
        rcdefaults()
        figures = report_figures(data)
        for name, (title,figure) in figures.items():
            path = output/f'{name}.png'
            try:
                figure.savefig(path,format='png',dpi=130,metadata={'Software':'implied-odds offline report'})
            finally:
                figure.clear()
            image = path.read_bytes()
            hashes[path.name] = hashlib.sha256(image).hexdigest()
            panels.append(f'<section><h2>{escape(title)}</h2><img alt="{escape(title)}" src="data:image/png;base64,{base64.b64encode(image).decode()}" /></section>')
    payload = {**data,'figure_sha256':hashes}
    rendered = json.dumps(payload,indent=2,sort_keys=True,allow_nan=False)+'\n'
    (output/'report.json').write_text(rendered,encoding='utf-8',newline='\n')
    prefix = 'Synthetic illustration' if data['synthetic'] else 'Implied odds'
    capture = data.get('capture',{})
    status = 'Synthetic illustration only' if data['synthetic'] else data['status']
    headline = ('Generated inputs exercise the reporting code. These are not market results.' if data['synthetic'] else
                'Settled research forecasts are insufficient for a calibration conclusion.' if data['status'] != 'ok' else
                'The declared primary comparison is SVI against ATM at the 24-hour lead on USD CRPS.')
    crps = data['statistics']['crps']['primary']
    primary_text = 'insufficient data' if crps['status'] != 'ok' else crps['conclusion']
    reasons = ''.join(f'<tr><td>{escape(name)}</td><td>{count}</td></tr>' for name,count in capture.get('reason_counts',{}).items())
    time_text = datetime.fromtimestamp(data['as_of_ms']/1000,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{prefix}</title><style>
body{{font:16px/1.55 system-ui,sans-serif;color:#183042;background:#eef2f4;margin:0}}main{{max-width:1120px;margin:auto;padding:40px 24px}}
h1{{font-size:42px;margin:8px 0}}h2{{font-size:21px}}.status{{display:inline-block;background:#e2eaf0;padding:6px 12px;border-radius:4px}}
section,.summary{{background:white;border:1px solid #d4dee4;border-radius:8px;padding:20px;margin:18px 0}}img{{width:100%;height:auto}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}.metric{{background:#f3f6f8;padding:15px}}strong{{display:block;font-size:24px}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px 10px;border-bottom:1px solid #dce3e8;text-align:left}}code{{overflow-wrap:anywhere;font-size:13px}}small{{color:#435d6f}}
</style></head><body><main><small>BTC option probability study · {time_text}</small><h1>{prefix}</h1>
<p class="status">{escape(status)}</p>
<p>Stochastic volatility inspired (SVI) fits an option variance smile. Its cumulative distribution function (CDF), the probability of settlement at or below a price, is compared with at-the-money (ATM), trailing-variance and generalised autoregressive conditional heteroskedasticity (GARCH) forecasts. The continuous ranked probability score (CRPS) measures distribution forecast error in USD; lower is better.</p>
<p>{headline}</p><p>{escape(data['dataset_label'])}</p>
<div class="summary"><div class="grid"><div class="metric">Captured quotes<strong>{capture.get('n_quotes',0):,}</strong></div>
<div class="metric">Scored SVI forecasts<strong>{data.get('n_svi_scores',0):,}</strong></div>
<div class="metric">Primary paired ESS<strong>{crps['effective_sample_size']:.1f}</strong></div></div>
<p>Primary result: {escape(primary_text)}</p><p>ESS is a dependence-adjusted effective sample size.</p></div>
{''.join(panels)}<section><h2>Reasons and limits</h2><p>Quote rejection counts and ingestion issues remain in report.json. A quote can have several reasons.</p>
<table><thead><tr><th>Quote reason</th><th>Count</th></tr></thead><tbody>{reasons or '<tr><td>No quote reasons recorded</td><td>0</td></tr>'}</tbody></table>
<p>Risk-neutral probabilities reflect option prices and risk premia; realised frequencies can differ without a trading opportunity. This study uses one venue and a short sample. Overlapping forecasts reduce effective sample size. Settlement is a 30-minute time-weighted average price. Thin wings need extrapolation, snapshots aren't atomic, and crypto funding, jumps and market structure can affect comparisons.</p>
<p>The smile and CDF use one frozen forecast and its exact quote snapshot. CDF shading marks prices beyond observed strikes. The probability integral transform, or PIT, evaluates the forecast CDF at settlement. Its binomial bands are pointwise references under independent uniform outcomes, rather than calibration tests for overlapping observations.</p>
<p>Tail events include quantile boundaries. Their stationary-bootstrap intervals require ESS &gt;= 60. Cumulative-mean CRPS intervals are pointwise, with an ESS check at each prefix, rather than simultaneous bands or extra headline tests. Coverage retains every model, lead and expiry class; infinite and undefined log losses remain counted in report.json. The GARCH CDF is a finite simulation approximation.</p></section>
<section><h2>Provenance</h2><p>Protocol: <code>{escape(data['protocol_hash'])}</code><br>Baseline extension: <code>{escape(data['extension_hash'])}</code><br>
Commit: <code>{escape(str(data['provenance']['git_commit']))}</code></p><p>Complete scores, figure hashes and the checklist remain in report.json.</p></section>
</main></body></html>'''
    (output/'report.html').write_text(html,encoding='utf-8',newline='\n')
    return {'status':data['status'],'synthetic':data['synthetic'],'report_files':['report.html','report.json',*hashes],
            'report_content_hash':digest(payload),'n_svi_scores':data.get('n_svi_scores',0)}
