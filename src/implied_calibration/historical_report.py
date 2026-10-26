"""Historical variance reports rebuild from hash-verified public responses."""

import csv
from datetime import datetime, timezone
import hashlib
from html import escape
import importlib.metadata
import json
import math
from pathlib import Path
import platform

import matplotlib as mpl
from matplotlib.figure import Figure

from .historical_data import DVOL, HistoricalInputs, load_study, read_study
from .client import validate_response
from .locking import digest
from .raw import read_raw
from .variance_study import DAY_MS, HOUR_MS, analyse_variance


def utc_date(stamp: int) -> str:
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime('%Y-%m-%d')


def alignment_checks(inputs: HistoricalInputs, days: list[int]) -> dict:
    checks = []
    intervals = [('DVOL', day, inputs.dvol, inputs.alignment_dvol) for day in days]
    price_days = sorted({day + offset * HOUR_MS for day in days for offset in (-16, 8)})
    intervals += [('BTC-PERPETUAL', day, inputs.alignment_prices, inputs.prices) for day in price_days]
    for name, day, daily, hourly in intervals:
        aggregate = None
        bars = [hourly.get(day + hour * HOUR_MS) for hour in range(24)]
        completed = [bar for bar in bars if bar is not None]
        if len(completed) == 24:
            aggregate = [completed[0][0], max(bar[1] for bar in completed),
                         min(bar[2] for bar in completed), completed[-1][3]]
        candle = daily.get(day)
        passed = bool(candle is not None and aggregate is not None and all(
            math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8) for a, b in zip(candle, aggregate)))
        checks.append({'source': name, 'day_ms': day, 'date': utc_date(day),
                       'daily_ohlc': candle, 'aggregate_hourly_ohlc': aggregate, 'passed': passed})
    return {'passed': all(row['passed'] for row in checks) and len(checks) == len(days) + len(price_days), 'checks': checks,
            'relative_tolerance': 1e-8, 'absolute_tolerance': 1e-8,
            'interpretation': "DVOL daily bars from 00:00 UTC and every retained perpetual daily bar from 08:00 UTC are checked against their 24 hourly bars. Original data vintages remain unverified."}


def read_alignment_audit(manifest_path: Path, inputs: HistoricalInputs, config: dict) -> dict:
    root = Path(manifest_path).resolve().parent
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if (manifest.get('kind') != 'historical_alignment_audit' or not isinstance(manifest.get('files'), dict)
            or type(manifest.get('captured_from_ms')) is not int or type(manifest.get('captured_until_ms')) is not int
            or not 0 <= manifest['captured_from_ms'] <= manifest['captured_until_ms']):
        raise ValueError('Invalid separate alignment audit manifest')
    inventory = {p.relative_to(root).as_posix() for p in (root / 'raw').rglob('*') if p.is_file()}
    if inventory != set(manifest['files']):
        raise ValueError('Alignment audit inventory differs')
    day = config['alignment_check_days_ms'][-1]
    daily: list = []
    hourly: list = []
    requests: list = []
    for relative, proof in sorted(manifest['files'].items()):
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Alignment audit path escapes its recording folder')
        body = path.read_bytes()
        if proof != {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}:
            raise ValueError('Alignment audit hash or size differs')
        issues: list[dict] = []
        for row in read_raw(path, on_issue=issues.append):
            params = row.get('params', {})
            stamp = row.get('fetched_at_ms')
            if (row.get('endpoint') != DVOL or row.get('ok') is not True
                    or set(params) != {'currency', 'resolution', 'start_timestamp', 'end_timestamp'}
                    or params['currency'] != 'BTC' or params['resolution'] not in ('1D', '3600')
                    or not params['start_timestamp'] <= day <= params['end_timestamp']
                    or params['end_timestamp'] - params['start_timestamp'] > 7 * DAY_MS
                    or type(stamp) is not int or not manifest['captured_from_ms'] <= stamp <= manifest['captured_until_ms']
                    or not 0 <= row['request_sent_ms'] <= row['response_received_ms'] == stamp):
                raise ValueError('Invalid separate alignment audit source or timing')
            result = validate_response(DVOL, row['http_status'], row['body'])
            matches = [values[1:] for values in result['data'] if values[0] == day]
            if len(matches) != 1:
                raise ValueError('Alignment audit lacks its comparison candle')
            (daily if params['resolution'] == '1D' else hourly).extend(matches)
            requests.append(params)
        if issues:
            raise ValueError('Corrupt alignment audit record')
    if len(daily) != 2 or len(hourly) != 1:
        raise ValueError('Alignment audit needs two daily queries and one hourly query')
    repeated = all(tuple(value) == inputs.dvol[day] for value in daily) and tuple(hourly[0]) == inputs.alignment_dvol[day]
    return {'manifest': manifest, 'requests': requests, 'day_ms': day,
            'daily_ohlc': daily, 'first_hour_ohlc': hourly, 'same_values_on_refetch': repeated,
            'role': "This audit diagnoses a retained source discrepancy after inspection of the first results. Original inputs and the failed alignment gate remain unchanged."}


def variance_figure(result: dict) -> Figure:
    figure = Figure(figsize=(11, 8), layout='constrained', facecolor='white')
    top, bottom = figure.subplots(2, 1, gridspec_kw={'height_ratios': [1.5, 1]})
    rows = result['observations']
    dates, implied, realised = [], [], []
    previous = None
    for row in rows:
        stamp = row['forecast']['origin_ms']
        if previous is not None and stamp != previous + DAY_MS:
            dates.append(datetime.fromtimestamp((previous + DAY_MS) / 1000, timezone.utc))
            implied.append(math.nan)
            realised.append(math.nan)
        dates.append(datetime.fromtimestamp(stamp / 1000, timezone.utc))
        implied.append(math.sqrt(row['forecast']['dvol_variance']) * 100)
        realised.append(math.sqrt(row['realised_variance']) * 100)
        previous = stamp
    top.plot(dates, implied,
             color='#235c99', linewidth=1, label='DVOL at the forecast origin')
    top.plot(dates, realised,
             color='#be5a27', linewidth=.9, alpha=.85, label='Subsequent 30-day realised volatility')
    top.set(title='BTC: option-implied volatility and subsequent realised volatility',
            ylabel='Annualised volatility (%)', xlabel='Daily forecast origin, UTC')
    top.legend(loc='upper right', fontsize=8)
    top.grid(alpha=.15)
    labels = list(result['annual']) + ['Full sample']
    summaries = list(result['annual'].values()) + [result['primary']]
    for index, summary in enumerate(summaries):
        metric = summary['metrics'].get('qlike_dvol_minus_trailing')
        if metric is None:
            continue
        mean = metric['mean']
        if metric['confidence_interval_95'] is not None:
            low, high = metric['confidence_interval_95']
            bottom.plot([index, index], [low, high], color='#235c99', linewidth=2)
        bottom.scatter([index], [mean], color='#235c99', s=35, zorder=3)
    bottom.axhline(0, color='#586472', linewidth=.8)
    bottom.set(xticks=range(len(labels)), xticklabels=labels, ylabel='Mean QLIKE difference',
               title='DVOL minus trailing realised variance: lower favours DVOL')
    bottom.grid(axis='y', alpha=.15)
    study = result['study']
    figure.suptitle('Real historical Deribit data | ' + utc_date(study['origin_start_ms']) + ' to ' + utc_date(study['origin_end_ms']), fontsize=14)
    return figure


def _table(result: dict) -> str:
    body = []
    for label, summary in [('Full sample', result['primary']), *result['annual'].items()]:
        metrics = summary['metrics']
        if not metrics:
            continue
        difference = metrics['qlike_dvol_minus_trailing']
        interval = difference['confidence_interval_95']
        interval_text = f'{interval[0]:+.4f} to {interval[1]:+.4f}' if interval else 'Withheld: calendar gaps'
        body.append(f'<tr><td>{escape(label)}</td><td>{summary["n"]}</td>'
                    f'<td>{metrics["qlike_dvol"]["mean"]:.4f}</td><td>{metrics["qlike_trailing"]["mean"]:.4f}</td>'
                    f'<td>{difference["mean"]:+.4f}</td><td>{interval_text}</td>'
                    f'<td>{difference["effective_sample_size"]:.1f}</td></tr>')
    return '<table><thead><tr><th>Origins</th><th>n</th><th>DVOL QLIKE</th><th>Trailing QLIKE</th>' \
           '<th>Difference</th><th>95% interval</th><th>ESS</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table>'


def write_historical_report(manifest_path: Path, study_path: Path, output: Path,
                            *, provenance: dict | None = None, audit_manifest_path: Path | None = None) -> dict:
    config = load_study(study_path)
    output = Path(output).resolve()
    if output.is_relative_to(Path(manifest_path).resolve().parent):
        raise ValueError('Historical report output must stay outside the input recording folder')
    if audit_manifest_path is not None and output.is_relative_to(Path(audit_manifest_path).resolve().parent):
        raise ValueError('Historical report output must stay outside the audit recording folder')
    inputs = read_study(manifest_path, config)
    result = analyse_variance(inputs.prices, inputs.dvol, config)
    result['alignment'] = alignment_checks(inputs, config['alignment_check_days_ms'])
    result['primary']['formal_claim_eligible'] = result['primary']['status'] == 'ok' and result['alignment']['passed']
    if not result['alignment']['passed']:
        result['primary']['status'] = 'alignment check failed'
        result['primary']['conclusion'] = 'The failed candle-alignment check withholds the primary claim.'
    if audit_manifest_path is not None:
        result['alignment_audit'] = read_alignment_audit(audit_manifest_path, inputs, config)
    result['study'] = config
    result['study_hash'] = digest(config)
    result['input_manifest'] = inputs.manifest
    result['input_counts'] = {'price_candles': len(inputs.prices), 'dvol_candles': len(inputs.dvol),
                              'duplicate_candles': inputs.duplicate_candles,
                              'failed_attempts': inputs.failed_attempts}
    result['provenance'] = {**(provenance or {}), 'python': platform.python_version(),
        'dependencies': {name: importlib.metadata.version(name) for name in ('numpy', 'scipy', 'matplotlib')}}
    output.mkdir(parents=True, exist_ok=True)
    with mpl.rc_context():
        mpl.rcdefaults()
        mpl.rcParams.update({'svg.hashsalt': 'implied-odds-historical-variance', 'svg.fonttype': 'none'})
        figure = variance_figure(result)
        figure.savefig(output / 'historical-variance.svg', metadata={'Date': None, 'Creator': 'implied-odds'}, bbox_inches='tight')
        figure.clear()
    result['figure_sha256'] = hashlib.sha256((output / 'historical-variance.svg').read_bytes()).hexdigest()
    with (output / 'observations.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['origin_utc', 'target_end_utc', 'dvol_variance', 'trailing_variance', 'realised_variance',
                         'correction_factor', 'latest_training_outcome_utc'])
        for row in result['observations']:
            correction = row['correction']
            writer.writerow([utc_date(row['forecast']['origin_ms']), utc_date(row['target_end_ms']),
                row['forecast']['dvol_variance'], row['forecast']['trailing_variance'], row['realised_variance'],
                correction['factor'] if correction else '', utc_date(correction['latest_training_outcome_ms']) if correction else ''])
    (output / 'report.json').write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8', newline='\n')
    primary = result['primary']
    correction = result['correction']
    secondary = ''
    if correction['metrics']:
        cm = correction['metrics']
        secondary = f'''<h2>Past-only correction</h2><p>The correction comparison uses the same {correction['n']} origins from 2023 onward.
Mean QLIKE is {cm['qlike_corrected']['mean']:.4f} after correction, {cm['qlike_dvol']['mean']:.4f} for raw DVOL
and {cm['qlike_trailing']['mean']:.4f} for trailing variance. The scale uses the latest 365 completed origin outcomes.
That rolling window can create persistence beyond the primary 60-day block; this is a secondary diagnostic.</p>'''
    svg = (output / 'historical-variance.svg').read_text(encoding='utf-8')
    svg = svg[svg.index('<svg'):]
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC historical variance research</title><style>body{{font:17px/1.6 system-ui,sans-serif;max-width:1080px;margin:45px auto;padding:24px;color:#183042}}
table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:8px;text-align:right;border-bottom:1px solid #ccd5df}}td:first-child,th:first-child{{text-align:left}}svg{{width:100%;height:auto}}</style></head><body>
<h1>Does BTC option-implied variance beat trailing realised variance?</h1>
<p>Historical BTC DVOL, Deribit's option-implied volatility index, is compared with subsequent 30-day realised variance from hourly BTC-PERPETUAL returns.
The sample contains {primary['n']} daily origins from {utc_date(config['origin_start_ms'])} through {utc_date(config['origin_end_ms'])}, with {len(result['excluded_origins'])} exclusions.
The historical candles were retrieved on {utc_date(inputs.manifest['captured_from_ms'])}.</p>{svg}
<h2>Paired forecast loss</h2><p>QLIKE = actual / forecast − log(actual / forecast) − 1 measures annualised-variance forecast error. Lower is better.
The primary interval uses 5,000 paired stationary-bootstrap draws, resampling adjacent loss blocks to retain dependence, with 60-day expected blocks and seed 314159.
Effective sample size, or ESS, is checked against 60. Heteroskedasticity and autocorrelation consistent, or HAC, variance uses 59 daily lags.
Sensitivity checks use 30/90-day blocks and non-overlapping 30-day targets.</p>{_table(result)}{secondary}
<h2>Scope and source limits</h2><p><strong>{escape(primary['status'])}: {escape(primary['conclusion'])}</strong></p>
<p>All open/high/low/close mismatches remain in report.json. A failed alignment check keeps every interval descriptive even when ESS passes; annual intervals are also descriptive.
A separate refetch diagnoses the discrepancy while preserving the original inputs and gate. Historical downloads do not establish original data vintages or prospective forecasts.</p>
<p>{escape(config['claim_scope'])} {escape(config['vintage'])}
DVOL includes an option risk premium and methodology choices. Perpetual returns are a proxy for spot-index returns.
An implied/realised gap alone doesn't establish mispricing, a causal risk premium or trading profit.</p>
<p>Every requested origin and exclusion reason is retained in report.json, along with alignment checks, sensitivity results,
input hashes, capture times and software versions. Individual outcomes appear in observations.csv.</p>
</body></html>'''
    (output / 'report.html').write_text(html, encoding='utf-8', newline='\n')
    return {'status': 'reproduced', 'research_status': primary['status'], 'n_origins': primary['n'],
            'report_files': ['report.json', 'report.html', 'observations.csv', 'historical-variance.svg']}
