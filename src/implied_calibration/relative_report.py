"""The ETH extension rebuilds from retained responses and preserves BTC releases."""

import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import shutil

import matplotlib as mpl
from matplotlib.figure import Figure
import numpy as np

from .historical_data import load_study, read_study
from .relative_variance import (PAIRS, RATIO_MODELS, MARGINAL_MODELS, correlation, evaluate_pairs,
                                load_relative_spec, paired_cases, summarise_pairs)
from .review_models import utc_ms
from .store import digest
from .variance_study import DAY_MS, HOUR_MS


LABELS = {'completed_mean': 'Completed-label mean', 'trailing_ratio': 'Trailing ratio',
          'raw_dvol_ratio': 'Raw DVOL ratio', 'calibrated_dvol_ratio': 'Calibrated DVOL ratio',
          'ratio_har': 'Ratio HAR', 'ratio_har_dvol': 'Ratio HAR + DVOL',
          'dvol': 'Raw DVOL', 'log_har': 'Log-HAR', 'log_har_dvol': 'Log-HAR + DVOL'}


def date(stamp: int) -> str:
    return datetime.fromtimestamp(stamp/1000, timezone.utc).date().isoformat()


def source_audit(inputs, config: dict) -> dict:
    missing_prices = [t for t in range(config['price_start_ms'], config['price_end_ms'], HOUR_MS)
                      if t not in inputs.prices]
    missing_dvol = [t for t in range(config['dvol_start_ms'], config['dvol_end_ms'], DAY_MS)
                    if t not in inputs.dvol]
    alignment = []
    for day in config['alignment_check_days_ms']:
        bars = [inputs.alignment_dvol.get(day+h*HOUR_MS) for h in range(24)]
        candle = inputs.dvol.get(day)
        aggregate = None if any(b is None for b in bars) else [bars[0][0], max(b[1] for b in bars),
                                                               min(b[2] for b in bars), bars[-1][3]]
        alignment.append({'source': config['dvol_currency']+'_DVOL', 'date': date(day),
                          'daily': candle, 'hourly_aggregate': aggregate,
                          'close_passed': bool(candle and aggregate and math.isclose(candle[3], aggregate[3], rel_tol=1e-8, abs_tol=1e-8)),
                          'all_ohlc_passed': bool(candle and aggregate and np.allclose(candle, aggregate, rtol=1e-8, atol=1e-8))})
    for day, candle in sorted(inputs.alignment_prices.items()):
        bars = [inputs.prices.get(day+h*HOUR_MS) for h in range(24)]
        aggregate = None if any(b is None for b in bars) else [bars[0][0], max(b[1] for b in bars),
                                                               min(b[2] for b in bars), bars[-1][3]]
        alignment.append({'source': config['price_instrument'], 'open_ms': day,
                          'daily': candle, 'hourly_aggregate': aggregate,
                          'close_passed': bool(aggregate and math.isclose(candle[3], aggregate[3], rel_tol=1e-8, abs_tol=1e-8)),
                          'all_ohlc_passed': bool(aggregate and np.allclose(candle, aggregate, rtol=1e-8, atol=1e-8))})
    return {'asset': config['dvol_currency'], 'hourly_candles': len(inputs.prices),
            'daily_dvol_candles': len(inputs.dvol), 'missing_price_hours': missing_prices,
            'missing_dvol_days': missing_dvol, 'duplicate_candles': inputs.duplicate_candles,
            'retained_failed_attempts': inputs.failed_attempts, 'alignment': alignment,
            'required_close_alignment_passed': bool(alignment) and all(r['close_passed'] for r in alignment),
            'unused_ohlc_discrepancies': sum(not r['all_ohlc_passed'] for r in alignment)}


def independent_targets(inputs: dict, records: list[dict]) -> dict:
    maximum = 0.
    for row in records:
        for index, asset in enumerate(('BTC', 'ETH')):
            prices = inputs[asset].prices
            closes = [prices[t-HOUR_MS][3] for t in range(row['target_start_ms'], row['target_end_ms']+HOUR_MS, HOUR_MS)]
            returns = [math.log(b)-math.log(a) for a, b in zip(closes, closes[1:])]
            independent = math.fsum(r*r for r in returns)*8760/len(returns)
            error = abs(independent-row['actual'][index])/independent
            maximum = max(maximum, error)
    return {'checked_asset_targets': 2*len(records), 'maximum_relative_error': maximum,
            'tolerance': 1e-10, 'passed': maximum <= 1e-10,
            'method': 'scalar log returns and math.fsum; separate from vector target construction'}


def vintage_audit(old, new) -> dict:
    output = {}
    for name in ('prices', 'dvol'):
        previous, refreshed = getattr(old, name), getattr(new, name)
        common = sorted(set(previous) & set(refreshed))
        changed_close = [t for t in common if previous[t][3] != refreshed[t][3]]
        changed_ohlc = [t for t in common if previous[t] != refreshed[t]]
        output[name] = {'overlapping_candles': len(common), 'changed_closes': len(changed_close),
                        'changed_ohlc': len(changed_ohlc), 'missing_old_candles': len(set(previous)-set(refreshed)),
                        'maximum_relative_close_revision': max((abs(refreshed[t][3]/previous[t][3]-1) for t in common), default=0.),
                        'first_changed_close_ms': changed_close[:10]}
    return output


def hourly_return_correlation(inputs: dict, spec: dict) -> dict:
    start, end = utc_ms(spec['evaluation_start']), utc_ms(spec['evaluation_end'])+30*DAY_MS
    values = []
    for time in range(start, end, HOUR_MS):
        candles = [[inputs[asset].prices.get(time-HOUR_MS), inputs[asset].prices.get(time)] for asset in ('BTC', 'ETH')]
        if any(c is None for pair in candles for c in pair):
            continue
        values.append([math.log(pair[1][3])-math.log(pair[0][3]) for pair in candles])
    return {'n_unique_hours': len(values), 'correlation': correlation(*np.asarray(values).T)} if values else {'n_unique_hours': 0, 'correlation': None}


def figure_for(records: list[dict], summary: dict) -> Figure:
    figure = Figure(figsize=(10, 7), layout='constrained', facecolor='white')
    top, lower = figure.subplots(2, 1)
    dates = [datetime.fromtimestamp(r['origin_ms']/1000, timezone.utc) for r in records]
    top.plot(dates, [r['target_log_ratio'] for r in records], color='#55616d', linewidth=1, label='Subsequent realised log variance ratio')
    top.plot(dates, [r['ratio_forecasts']['raw_dvol_ratio'] for r in records], color='#c26e2b', linewidth=.8, label='Raw DVOL log variance ratio')
    top.set(ylabel='log(ETH variance / BTC variance)', title='Relative variance, separate from the common volatility level')
    top.grid(alpha=.15)
    top.legend(fontsize=8)
    for name, label in (('ratio_har_dvol', 'Ratio HAR + DVOL minus ratio HAR'), ('raw_dvol_ratio', 'Raw DVOL ratio minus ratio HAR')):
        diff = np.asarray([r['ratio_losses'][name]-r['ratio_losses']['ratio_har'] for r in records])
        lower.plot(dates, np.cumsum(diff), label=label, linewidth=1.2)
    lower.axhline(0, color='#55616d', linewidth=.8)
    lower.set(ylabel='Cumulative squared log-ratio error difference', xlabel='Daily forecast origin, UTC',
              title='Below zero favours the first forecast; descriptive paired comparisons')
    lower.legend(fontsize=8)
    lower.grid(alpha=.15)
    return figure


def interval(metric: dict) -> str:
    bounds = metric['confidence_interval_95']
    return 'withheld' if bounds is None else f'[{bounds[0]:+.5f}, {bounds[1]:+.5f}]'


def render_note(result: dict) -> str:
    stats = result['main']['summary']
    metrics = stats['metrics']
    focus = metrics['ratio_har_dvol_minus_ratio_har']
    raw_gap = metrics['raw_dvol_ratio_minus_ratio_har']
    relative_penalty = 100*raw_gap['mean']/metrics['mse_ratio_har']['mean']
    incremental_gain = -100*focus['mean']/metrics['mse_ratio_har']['mean']
    asset_changes = {asset: 100*metrics[asset+'_log_har_dvol_minus_log_har']['mean']/metrics[asset+'_qlike_log_har']['mean']
                     for asset in ('BTC', 'ETH')}
    bounds = focus['confidence_interval_95']
    interval_statement = ('The primary interval includes zero.' if bounds and bounds[0] <= 0 <= bounds[1]
                          else 'The primary interval remains descriptive.')
    lines = ['# Does DVOL predict ETH risk relative to BTC?', '',
             f"Adding DVOL to separate log-HAR models changes mean QLIKE by {asset_changes['BTC']:+.2f}% for BTC and {asset_changes['ETH']:+.2f}% for ETH. "
             f"Raw DVOL's direct ETH/BTC log-ratio error is {relative_penalty:+.2f}% relative to ratio-HAR's. "
             f"That paired relative-risk difference is {raw_gap['mean']:+.5f}, descriptive 95% interval {interval(raw_gap)}, "
             f"with effective sample size (ESS) {raw_gap['effective_sample_size']:.2f}. "
             'The individual-asset and relative-risk targets give different forecast rankings in this sample.', '',
             f"I compare {stats['n']:,} matched daily origins from {result['first_origin']} to {result['last_origin']}. "
             'The target is the log ratio of subsequent 30-day ETH variance to BTC variance, measured from hourly perpetual returns. '
             'This asks whether options distinguish relative risk, after removing a common multiplicative variance level.', '',
             f"Adding the DVOL log ratio to a direct ratio-HAR model changes mean squared log-ratio error by {focus['mean']:+.5f}, "
             f"a {incremental_gain:.2f}% point reduction, with descriptive 95% interval {interval(focus)} and effective sample size {focus['effective_sample_size']:.2f}. "
             f'This is the primary comparison. {interval_statement}', '',
             '## Matched relative-risk forecasts', '',
             'Ratio HAR fits the next 30-day log variance ratio to the trailing 1-, 7- and 30-day log realised-variance ratios. '
             'Ratio HAR + DVOL adds the current log ratio of DVOL variances. Both regressions use the same 730 completed labels and OLS, '
             'which matches the squared-error objective. Calibrated DVOL adds the mean completed log error from those labels. '
             'The completed-label mean uses their average target; the raw DVOL and trailing ratios have no fitted coefficients.', '',
             '| Forecast | Mean squared log-ratio error |', '| --- | ---: |']
    for name in RATIO_MODELS:
        lines.append(f"| {LABELS[name]} | {metrics['mse_'+name]['mean']:.5f} |")
    lines += ['', '| Paired comparison | Mean difference | Descriptive 95% interval | ESS |', '| --- | ---: | --- | ---: |']
    for first, second in PAIRS:
        name = first+'_minus_'+second
        metric = metrics[name]
        lines.append(f"| {LABELS[first]} minus {LABELS[second]} | {metric['mean']:+.5f} | {interval(metric)} | {metric['effective_sample_size']:.2f} |")
    lines += ['', '![Matched relative-variance forecasts and cumulative error differences](eth-relative-variance.svg)', '',
              '## Marginal forecasts use the same origins', '',
              '| Asset | Raw DVOL QLIKE | Log-HAR QLIKE | Log-HAR + DVOL QLIKE | Added DVOL difference and interval |',
              '| --- | ---: | ---: | ---: | --- |']
    for asset in ('BTC', 'ETH'):
        metric = metrics[asset+'_log_har_dvol_minus_log_har']
        means = [metrics[asset+'_qlike_'+name]['mean'] for name in MARGINAL_MODELS]
        lines.append(f"| {asset} | {means[0]:.5f} | {means[1]:.5f} | {means[2]:.5f} | {metric['mean']:+.5f} {interval(metric)} |")
    interaction = metrics['ETH_minus_BTC_incremental_qlike']
    lines += ['', f"ETH minus BTC's incremental QLIKE difference is {interaction['mean']:+.5f}, interval {interval(interaction)}. "
              'Both assets share bootstrap dates; their difference uses paired dates rather than two independent samples.', '',
              '## Common and relative log errors', '',
              'For each marginal forecast, I split log errors into their average across assets and their ETH-minus-BTC difference. '
              'A common multiplicative forecast error cancels from the difference. These diagnostics use squared log errors, which differ from QLIKE.', '',
              '| Marginal forecast | Shared-component mean squared error | Relative-component mean squared error | Log-error correlation |',
              '| --- | ---: | ---: | ---: |']
    for name in MARGINAL_MODELS:
        corr = result['main']['marginal_log_error_decomposition'][name]['log_error_correlation']
        corr_text = 'undefined' if corr is None else f'{corr:.3f}'
        lines.append(f"| {LABELS[name]} | {metrics[name+'_common_squared_log_error']['mean']:.5f} | "
                     f"{metrics[name+'_relative_squared_log_error']['mean']:.5f} | {corr_text} |")
    for name, label in (('marginal_common_increment', 'shared'), ('marginal_relative_increment', 'relative')):
        metric = metrics[name]
        lines += ['', f"Log-HAR + DVOL minus log-HAR changes the {label} component by {metric['mean']:+.5f}, interval {interval(metric)}."]
    common_change = 100*metrics['marginal_common_increment']['mean']/metrics['log_har_common_squared_log_error']['mean']
    relative_change = 100*metrics['marginal_relative_increment']['mean']/metrics['log_har_relative_squared_log_error']['mean']
    lines += ['', f"The shared component changes by {common_change:+.2f}% and the relative component by {relative_change:+.2f}%. "
              'These averages distinguish the shared volatility-level error from the error in relative risk sizing.']
    lines += ['', 'This is an error decomposition, not an identified causal market factor. '
              'The ratio of two conditional variance means can differ from the conditional mean log ratio.', '',
              '## Time and alignment checks', '',
              '| Year | Origins | Ratio HAR + DVOL minus ratio HAR | Raw DVOL ratio minus ratio HAR |', '| --- | ---: | ---: | ---: |']
    for year, row in result['main']['annual'].items():
        lines.append(f"| {year} | {row['n']} | {row['means']['ratio_har_dvol_minus_ratio_har']:+.5f} | {row['means']['raw_dvol_ratio_minus_ratio_har']:+.5f} |")
    lines += ['', '| Fixed sample half | Origins | Ratio HAR + DVOL minus ratio HAR | Raw DVOL ratio minus ratio HAR |',
              '| --- | ---: | ---: | ---: |']
    for label, row in result['main']['halves'].items():
        lines.append(f"| {label} | {row['n']} | {row['means']['ratio_har_dvol_minus_ratio_har']:+.5f} | {row['means']['raw_dvol_ratio_minus_ratio_har']:+.5f} |")
    lines += ['', '| Check | Origins | Ratio HAR + DVOL minus ratio HAR | Descriptive 95% interval |', '| --- | ---: | ---: | --- |']
    for label, row in [('Main', result['main']), *result['sensitivities'].items()]:
        metric = row['summary']['metrics']['ratio_har_dvol_minus_ratio_har']
        lines.append(f"| {label} | {row['summary']['n']} | {metric['mean']:+.5f} | {interval(metric)} |")
    lines += ['', 'The JSON also retains both fixed sample halves, all 30 dependent nonoverlap phases, '
              'the largest absolute loss contributions and 30-, 90- and 180-day bootstrap blocks.', '',
              '## Why this matters and what it leaves open', '',
              'For a forecast log ratio z, the standalone variance-budget weights are '
              '`w_ETH = 1/(1+exp(z/2))` and `w_BTC = 1-w_ETH`. '
              'Their squared log realised variance-contribution imbalance equals the forecast loss in the first table. '
              'This gives the error a risk-sizing interpretation; it excludes covariance contributions and trading performance.', '',
              '**Research choices.** I fixed the local specification after seeing BTC results and the literature, before collecting or scoring ETH. '
              'It has no independent pre-evaluation timestamp. Every result is exploratory. This extension preserves the BTC paper and prospective locks.', '',
              '**Inference.** The intervals use 5,000 stationary bootstrap resamples with a mean block length of 60 days and shared dates across assets. '
              'The earlier bootstrap size failures still apply; clearing the ESS reporting rule alone cannot justify a formal claim. '
              'ESS is the smaller estimate from the initial positive monotone autocorrelation pairs and a 59-lag Newey-West long-run variance, capped at the origin count. '
              'The inherited threshold of 60 is a reporting convention, not a guarantee of calibrated test size.', '',
              '**Data.** Two correlated crypto assets add less evidence than two independent markets. '
              'Hourly perpetual variance is a proxy for option-index risk. Taking logs can change measurement bias even when each variance proxy is unbiased. '
              'A synthetic test demonstrates this gap; the empirical target remains the observed hourly-variance ratio, rather than an unobserved true variance ratio.', '',
              '## Data and reproduction', '',
              f"I retain {sum(r['hourly_candles'] for r in result['source_audits'].values()):,} hourly candles across the two assets. "
              f"Independent target reconstruction checks {result['independent_targets']['checked_asset_targets']:,} asset targets; "
              f"the maximum relative difference is {result['independent_targets']['maximum_relative_error']:.2e}.", '',
              '```sh', 'python scripts/build_relative.py', '```', '',
              '[Fixed specifications](../protocol/eth-relative-variance-v1.json) · [Full computed results](eth-relative-results.json)', '',
              'I refresh BTC and ETH in separate hashed recording folders. `source_audits`, `btc_vintage_audit` and '
              '`btc_vintage_scores` report gaps, close alignment, unused OHLC discrepancies and revisions relative to the old BTC inputs. '
              f"Across {result['btc_vintage_audit']['prices']['overlapping_candles']:,} overlapping BTC price candles and "
              f"{result['btc_vintage_audit']['dvol']['overlapping_candles']:,} DVOL candles, "
              f"{result['btc_vintage_audit']['prices']['changed_closes']+result['btc_vintage_audit']['dvol']['changed_closes']:,} closes changed. "
              + ('The three reused BTC model means also match the original v2 cohort.'
               if result['btc_vintage_scores']['published_v2_cohort_matched'] else
               'This run uses a different cohort from the original v2 table.'), '',
              '## Research context', '',
              'ETH volatility forecasting with HAR and cross-market inputs already appears in '
              '[Chen (2024)](https://doi.org/10.1080/23322039.2023.2300925). '
              '[Chevallier and Sanhaji (2023)](https://doi.org/10.3390/stats6040082) study implied-volatility inputs for BTC and ETH forecasts. '
              '[Amberdata (2023)](https://blog.amberdata.io/fresh-new-features-relative-volatility-charts) already charts relative implied volatility. '
              'The distinct question here is a matched direct forecast of the realised ETH/BTC variance ratio. '
              'I make no claim to the first relative-volatility study. '
              '[Patton (2011)](https://doi.org/10.1016/j.jeconom.2010.03.034) explains why proxy noise and the choice of loss matter for variance forecast rankings. '
              'Its conditions for QLIKE do not automatically carry over to squared log-ratio error.', '',
              'DVOL construction and data fields follow [Deribit’s methodology](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/) '
              'and [API documentation](https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data), accessed 27 October 2026.', '']
    return '\n'.join(lines)


def write_relative_report(root: Path, output: Path, *, publish: bool = False, input_root: Path | None = None) -> dict:
    root, output = Path(root).resolve(), Path(output).resolve()
    spec, declaration = load_relative_spec(root)
    configs = {asset: load_study(root/relative) for asset, relative in spec['source_configs'].items()}
    manifests = {asset: root/relative if input_root is None else Path(input_root)/asset.lower()/'manifest.json'
                 for asset, relative in spec['inputs'].items()}
    if any(output.is_relative_to(path.parent.resolve()) for path in manifests.values()):
        raise ValueError('Report output must stay outside retained input folders')
    inputs = {asset: read_study(manifests[asset], configs[asset]) for asset in ('BTC', 'ETH')}
    end = utc_ms(spec['evaluation_end'])
    records, excluded = evaluate_pairs(paired_cases(inputs, utc_ms(spec['case_start']), end), spec)
    main = summarise_pairs(records, spec)
    sensitivities = {}
    for label, kwargs in (('one_day_DVOL_lag', {'lag_days': 1}), ('one_hour_target_shift', {'target_shift_hours': 1})):
        rows, failures = evaluate_pairs(paired_cases(inputs, utc_ms(spec['case_start']), end, **kwargs), spec)
        sensitivities[label] = {**summarise_pairs(rows, spec), 'excluded': failures}
    old_config = load_study(root/'protocol/historical-variance.json')
    old_btc = read_study(root/'recordings/historical-variance/manifest.json', old_config)
    old_spec = {**spec, 'evaluation_end': date(old_config['origin_end_ms'])+'T00:00:00Z'}
    old_rows, old_excluded = evaluate_pairs(paired_cases({**inputs, 'BTC': old_btc}, utc_ms(spec['case_start']),
                                                       old_config['origin_end_ms']), old_spec)
    old_by_time = {r['origin_ms']: r for r in old_rows}
    common = [r for r in records if r['origin_ms'] in old_by_time]
    vintage_scores: dict = {'n': len(common), 'old_excluded': old_excluded,
                      'old': {name: float(np.mean([old_by_time[r['origin_ms']]['marginal_losses']['BTC'][name] for r in common])) for name in MARGINAL_MODELS},
                      'new': {name: float(np.mean([r['marginal_losses']['BTC'][name] for r in common])) for name in MARGINAL_MODELS}}
    published = json.loads((root/'report/v2-results.json').read_text(encoding='utf-8'))['benchmarks']['summary']
    model_map = {'dvol': 'dvol', 'log_har': 'log_har_smear', 'log_har_dvol': 'log_har_dvol_smear'}
    matched_v2_cohort = (len(common) == published['n'] and not old_excluded
                         and old_spec['evaluation_start'] == '2023-07-01T00:00:00Z'
                         and old_spec['evaluation_end'] == '2026-09-18T00:00:00Z')
    vintage_scores['published_v2_cohort_matched'] = matched_v2_cohort
    vintage_scores['published_v2_replication'] = {
        name: {'recomputed': vintage_scores['old'][name],
               'published': published['metrics']['qlike_'+mapped]['mean'],
               'matched': math.isclose(vintage_scores['old'][name], published['metrics']['qlike_'+mapped]['mean'], rel_tol=1e-8, abs_tol=1e-10)}
        for name, mapped in model_map.items()} if matched_v2_cohort else {}
    if matched_v2_cohort and not all(r['matched'] for r in vintage_scores['published_v2_replication'].values()):
        raise ValueError('Reused marginal BTC forecasts do not reproduce the published v2 cohort')
    result: dict = {'kind': 'exploratory_eth_relative_variance', 'synthetic': False, 'formal_claim_eligible': False,
              'specification': spec, 'declaration': declaration, 'n': len(records), 'excluded': excluded,
              'first_origin': date(records[0]['origin_ms']), 'last_origin': date(records[-1]['origin_ms']),
              'main': main, 'sensitivities': sensitivities,
              'source_audits': {asset: source_audit(inputs[asset], configs[asset]) for asset in ('BTC', 'ETH')},
              'independent_targets': independent_targets(inputs, records), 'btc_vintage_audit': vintage_audit(old_btc, inputs['BTC']),
              'btc_vintage_scores': vintage_scores, 'hourly_return_correlation': hourly_return_correlation(inputs, spec),
              'manifest_hashes': {asset: digest(inputs[asset].manifest) for asset in ('BTC', 'ETH')},
              'capture_provenance': {asset: {key: inputs[asset].manifest.get(key) for key in
                  ('git_commit', 'config_hash', 'captured_from_ms', 'captured_until_ms', 'capture_attempts', 'failed_attempts')}
                  for asset in ('BTC', 'ETH')},
              'analysis_source_sha256': {name: hashlib.sha256((root/'src/implied_calibration'/name).read_text(encoding='utf-8').encode('utf-8')).hexdigest()
                  for name in ('relative_variance.py', 'relative_report.py', 'incremental.py', 'review_models.py',
                               'variance_study.py', 'inference.py', 'historical_data.py', 'client.py', 'raw.py', 'store.py')},
              'fit_diagnostics': {name: {'minimum_rank': min(r['ratio_fits'][name]['rank'] for r in records),
                  'maximum_condition_number': max((r['ratio_fits'][name]['condition_number'] for r in records
                                                   if r['ratio_fits'][name]['condition_number'] is not None), default=None)}
                  for name in ('ratio_har', 'ratio_har_dvol')},
              'environment': {'python': platform.python_version(), **{name: importlib.metadata.version(name) for name in ('numpy', 'scipy', 'matplotlib', 'requests')}}}
    if not result['independent_targets']['passed']:
        raise ValueError('Independent target reconstruction failed')
    result['data_checks_passed'] = all(r['required_close_alignment_passed'] and not r['missing_price_hours'] and not r['missing_dvol_days']
                                     for r in result['source_audits'].values()) and not excluded
    output.mkdir(parents=True, exist_ok=True)
    (output/'summary.json').write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+'\n', encoding='utf-8', newline='\n')
    (output/'results.md').write_text(render_note(result), encoding='utf-8', newline='\n')
    fields = ['origin_ms', 'target_start_ms', 'target_end_ms', 'target_log_ratio', 'training_n', 'calibration_log_offset',
              'first_training_origin_ms', 'last_training_origin_ms', 'latest_training_target_end_ms']
    with (output/'forecasts.csv').open('w', newline='', encoding='utf-8') as stream:
        extra = ['BTC_actual_variance', 'ETH_actual_variance']
        extra += ['forecast_'+name for name in RATIO_MODELS]+['loss_'+name for name in RATIO_MODELS]
        extra += [model+'_'+field for model in ('ratio_har', 'ratio_har_dvol') for field in ('rank', 'condition_number')]
        extra += [asset+'_'+name+'_'+field for asset in ('BTC', 'ETH') for name in MARGINAL_MODELS for field in ('forecast', 'qlike')]
        writer = csv.DictWriter(stream, fieldnames=fields+extra)
        writer.writeheader()
        for record in records:
            row = {field: record[field] for field in fields}
            row.update({asset+'_actual_variance': record['actual'][i] for i, asset in enumerate(('BTC', 'ETH'))})
            row.update({model+'_'+field: record['ratio_fits'][model][field]
                        for model in ('ratio_har', 'ratio_har_dvol') for field in ('rank', 'condition_number')})
            row.update({'forecast_'+name: record['ratio_forecasts'][name] for name in RATIO_MODELS})
            row.update({'loss_'+name: record['ratio_losses'][name] for name in RATIO_MODELS})
            row.update({asset+'_'+name+'_'+field: record['marginal_forecasts' if field == 'forecast' else 'marginal_losses'][asset][name]
                        for asset in ('BTC', 'ETH') for name in MARGINAL_MODELS for field in ('forecast', 'qlike')})
            writer.writerow(row)
    figure = figure_for(records, main)
    with mpl.rc_context({'svg.hashsalt': 'implied-odds-eth-relative-v1'}):
        for extension in ('svg', 'pdf', 'png'):
            figure.savefig(output/('eth-relative-variance.'+extension), dpi=150,
                           metadata={'Date': None} if extension == 'svg' else {'CreationDate': None, 'ModDate': None} if extension == 'pdf' else None)
    if publish:
        for source, target in (('summary.json', 'eth-relative-results.json'), ('results.md', 'eth-relative-results.md'),
                               ('eth-relative-variance.svg', 'eth-relative-variance.svg')):
            shutil.copyfile(output/source, root/'report'/target)
    proofs = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output.iterdir()) if path.is_file() and path.name != 'artifacts.json'}
    (output/'artifacts.json').write_text(json.dumps(proofs, indent=2)+'\n', encoding='utf-8', newline='\n')
    return result
