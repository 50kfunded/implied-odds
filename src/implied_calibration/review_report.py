"""Separate v2 outputs retain the original v1 results and all declared failures."""

import csv
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from .historical_data import load_study, read_study
from .incremental import build_cases
from .incremental_report import write_incremental_report
from .inference_reference import calibration_report, empirical_power, reference_table
from .research_plan import load_plan
from .review_models import benchmark_records, load_review_spec, utc_ms, window_forecasts
from .store import digest
from .variance_study import DAY_MS, _metric_summary, qlike


def validate_v1_rebuild(reference, rebuilt) -> dict:
    """Require exact structure and permit only reported floating-point round-off."""
    differences = []
    def compare(a, b, path):
        if type(a) is not type(b):
            raise ValueError('The original v1 rebuild changed type at '+path)
        if isinstance(a, dict):
            if a.keys() != b.keys():
                raise ValueError('The original v1 rebuild changed keys at '+path)
            for key in a:
                compare(a[key], b[key], path+'.'+key)
        elif isinstance(a, list):
            if len(a) != len(b):
                raise ValueError('The original v1 rebuild changed length at '+path)
            for i, (left, right) in enumerate(zip(a, b)):
                compare(left, right, path+'['+str(i)+']')
        elif a != b:
            if (isinstance(a, float) and math.isfinite(a) and math.isfinite(b)
                    and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-14)):
                differences.append({'path': path, 'reference': a, 'rebuilt': b, 'absolute_difference': abs(a-b)})
            else:
                raise ValueError('The original v1 numerical rebuild changed at '+path+': '+repr(a)+' versus '+repr(b))
    compare(reference, rebuilt, '$')
    return {'exact_equal': not differences, 'relative_tolerance': 1e-12, 'absolute_tolerance': 1e-14,
            'floating_roundoff_differences': differences}


def window_check(cases: list, plan: dict, spec: dict) -> dict:
    config = spec['window_check_cohort']
    start, end = utc_ms(config['start']), utc_ms(config['end'])
    records = []
    for row in cases:
        if start <= row.feature_cutoff_ms <= end and row.actual is not None:
            fitted = window_forecasts(cases, row, plan)
            records.append({'origin_ms': row.feature_cutoff_ms, 'forecasts': fitted['forecasts'],
                            'training_counts': {name: fitted['diagnostics'][name]['training']['n']
                                                for name in fitted['diagnostics']},
                            'losses': {name: qlike(row.actual, value) for name, value in fitted['forecasts'].items()}})
    return {'n': len(records), 'first_origin_ms': records[0]['origin_ms'], 'last_origin_ms': records[-1]['origin_ms'],
            'means': {name: float(np.mean([r['losses'][name] for r in records])) for name in config['models']},
            'training_count_range': {name: [min(r['training_counts'][name] for r in records),
                                            max(r['training_counts'][name] for r in records)]
                                     for name in records[0]['training_counts']}, 'records': records}


def phase_summary(records: list[dict]) -> dict:
    contrasts = [('dvol', 'har'), ('har_dvol', 'har'), ('dvol', 'har_dvol')]
    output = {}
    first = records[0]['origin_ms']
    for model, baseline in contrasts:
        means, counts = [], []
        for phase in range(30):
            selected = [r for r in records if (r['origin_ms']-first)//DAY_MS % 30 == phase]
            means.append(float(np.mean([r['losses'][model]-r['losses'][baseline] for r in selected])))
            counts.append(len(selected))
        output[model+'_minus_'+baseline] = {'phase_means': means, 'phase_counts': counts,
                                            'n_negative': sum(value < 0 for value in means),
                                            'minimum': min(means), 'median': float(np.median(means)), 'maximum': max(means)}
    return {'comparisons': output, 'scope': 'All phases reuse the same history. Favourable counts are a dependent stability display; no binomial test or independence claim applies.'}


def model_confidence_set(records: list[dict], spec: dict) -> dict:
    from arch.bootstrap import MCS
    config = spec['mcs']
    if len(records) < 61 or any(b['origin_ms']-a['origin_ms'] != DAY_MS for a, b in zip(records, records[1:])):
        return {'status': 'withheld', 'reason': 'insufficient consecutive balanced daily forecasts', 'settings': config}
    losses = np.asarray([[r['losses'][name] for name in spec['models']] for r in records])
    procedure = MCS(losses, size=config['size'], reps=config['reps'], block_size=config['block_size'],
                    method=config['method'], bootstrap=config['bootstrap'], seed=config['seed'])
    procedure.compute()
    names = spec['models']
    return {'status': 'exploratory', 'settings': config, 'n': len(records),
            'included': [names[int(str(i))] for i in procedure.included],
            'excluded': [names[int(str(i))] for i in procedure.excluded],
            'pvalues': {names[int(i)]: float(row.iloc[0]) for i, row in procedure.pvalues.iterrows()},
            'scope': config['scope']}


def benchmark_summary(records: list[dict], spec: dict, plan: dict) -> dict:
    names = ['qlike_'+name for name in spec['models']]+[name+'_minus_reference' for name in spec['models']]
    names += ['focus_qlike_har_dvol_minus_qlike_har']
    values = np.asarray([[*[r['losses'][name] for name in spec['models']],
                          *[r['losses'][name]-r['losses'][spec['paired_reference']] for name in spec['models']],
                          r['losses']['qlike_har_dvol']-r['losses']['qlike_har']] for r in records])
    return _metric_summary(values, np.asarray([r['origin_ms'] for r in records]), names, plan)


def format_interval(value) -> str:
    return 'withheld' if value is None else '['+', '.join(f'{v:+.5f}' for v in value)+']'


def markdown(result: dict) -> str:
    lines = ['# Declared exploratory v2 comparisons', '',
             "I added these comparisons after inspecting v1 and the review. The public declaration and amendment precede this evaluation. I preserve the v1 results and prospective locks.", '',
             '## Matched window check', '',
             f"All rows use the same {result['window_check']['n']:,} origins. Raw DVOL has no estimation window.", '',
             '| Window | HAR mean QLIKE | HAR + DVOL mean QLIKE | Raw DVOL mean QLIKE |',
             '| --- | ---: | ---: | ---: |']
    means = result['window_check']['means']
    for window in ('365', '730', 'expanding'):
        lines.append(f"| {window} | {means['har_'+window]:.5f} | {means['har_dvol_'+window]:.5f} | {means['dvol']:.5f} |")
    lines += ['', '## Declared benchmark universe', '',
              f"I scheduled {result['benchmarks']['n_scheduled']:,} origins; {result['benchmarks']['summary']['n']:,} have all declared forecasts. Intervals are descriptive and are withheld across calendar gaps. The table uses the balanced cohort; coverage counts also retain failed fits.", '',
              '| Model | Coverage | Mean QLIKE | Difference from HAR + DVOL 365 | 95% interval | ESS |',
              '| --- | ---: | ---: | ---: | --- | ---: |']
    metrics = result['benchmarks']['summary']['metrics']
    for name in result['specification']['models']:
        level, difference = metrics.get('qlike_'+name), metrics.get(name+'_minus_reference')
        if level is None or difference is None:
            lines.append(f"| {name} | {result['benchmarks']['coverage'][name]} | unavailable | unavailable | withheld | unavailable |")
        elif name == result['specification']['paired_reference']:
            lines.append(f"| {name} | {result['benchmarks']['coverage'][name]} | {level['mean']:.5f} | reference | — | — |")
        else:
            lines.append(f"| {name} | {result['benchmarks']['coverage'][name]} | {level['mean']:.5f} | {difference['mean']:+.5f} | {format_interval(difference['confidence_interval_95'])} | {difference['effective_sample_size']:.2f} |")
    focal = metrics.get('focus_qlike_har_dvol_minus_qlike_har')
    lines += ['', ('The focal QLIKE HAR + DVOL minus QLIKE HAR comparison has mean '
                  f"{focal['mean']:+.5f}, interval {format_interval(focal['confidence_interval_95'])} and ESS {focal['effective_sample_size']:.2f}."
                  if focal is not None else 'The focal comparison has no balanced observations.'), '',
              f"I retain {len(result['benchmarks']['failures'])} failed origin/model fits in the JSON. A failure is not silently replaced by another forecast.", '',
              '## Inference references on the original cohort', '',
              '| Contrast | Mean | DM-HLN p, lag 29 | DM-HLN p, lag 59 | Subsampling interval |',
              '| --- | ---: | ---: | ---: | --- |']
    for row in result['v1_inference_references']:
        lines.append(f"| {row['contrast']} | {row['mean']:+.5f} | {row['dm_hln']['29']['p_value']:.4f} | {row['dm_hln']['59']['p_value']:.4f} | {format_interval(row['subsampling']['confidence_interval_95'])} |")
    if isinstance(result.get('focal_inference_references'), list):
        row = result['focal_inference_references'][0]
        lines.append(f"| {row['contrast']} on the separate v2 cohort | {row['mean']:+.5f} | {row['dm_hln']['29']['p_value']:.4f} | {row['dm_hln']['59']['p_value']:.4f} | {format_interval(row['subsampling']['confidence_interval_95'])} |")
    lines += ['', 'I report these p-values as references. HLN, subsampling and MCS do not remove the inspected-sample, dependence or generated-forecast limitations.', '',
              'The Bates-Granger row uses reconstructed past-only HAR forecasts from the retained data vintage. These are not original unrevised historical vintages.', '',
              '## Synthetic calibration', '',
              '| n | Process | Stationary percentile | DM-HLN 29 | DM-HLN 59 | Subsampling |',
              '| ---: | --- | ---: | ---: | ---: | ---: |']
    for row in result['inference_calibration']['rows']:
        values = [f"{100*row['methods'][method]['rejection_rate']:.1f}%" for method in result['specification']['calibration']['tested_methods']]
        lines.append('| '+str(row['n'])+' | '+row['dgp']+' | '+' | '.join(values)+' |')
    lines += ['', 'These are null rejection rates at nominal 5% in the declared synthetic processes. Monte Carlo intervals remain in the JSON. They do not estimate the false-positive rate in the market sample.', '',
              '## Conditional power planning', '',
              '| Expected block days | n | Assumed mean | DM-HLN rejection | Subsampling rejection |',
              '| ---: | ---: | ---: | ---: | ---: |']
    for row in result['empirical_power']['rows']:
        lines.append(f"| {row['expected_block_days']} | {row['n']} | {row['population_shift']:+.5f} | {100*row['methods']['dm_hln_lag59']['rejection_rate']:.1f}% | {100*row['methods']['subsampling']['rejection_rate']:.1f}% |")
    lines += ['', result['empirical_power']['limit'], '', '## Dependent phase display', '',
              '| Contrast | Negative phases out of 30 | Minimum mean | Median mean | Maximum mean |',
              '| --- | ---: | ---: | ---: | ---: |']
    for name, row in result['phases']['comparisons'].items():
        lines.append(f"| {name} | {row['n_negative']} | {row['minimum']:+.5f} | {row['median']:+.5f} | {row['maximum']:+.5f} |")
    lines += ['', result['phases']['scope'], '', '## Model Confidence Set', '',
              '```json', json.dumps(result['mcs'], indent=2), '```', '',
              'I report this set conditional on the inspected candidate universe and bootstrap assumptions. Membership does not establish a best model or a trading result.', '']
    return '\n'.join(lines)


def headline(result: dict, output: Path) -> None:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    models = result['specification']['models']
    metrics = result['benchmarks']['summary']['metrics']
    if not metrics:
        fig, ax = plt.subplots(figsize=(9, 3))
        ax.text(.5, .5, 'No balanced forecasts are available.\nCoverage and failures remain in the results.', ha='center', va='center')
        ax.axis('off')
        for extension in ('svg', 'png', 'pdf'):
            fig.savefig(output/('review-variance.'+extension))
        plt.close(fig)
        return
    names = {'dvol': 'Raw DVOL', 'har_365': 'HAR, 365 labels', 'har_dvol_365': 'HAR + DVOL, 365 labels',
             'har_730': 'HAR, 730 labels', 'har_dvol_730': 'HAR + DVOL, 730 labels',
             'har_expanding': 'HAR, expanding', 'har_dvol_expanding': 'HAR + DVOL, expanding',
             'log_har_smear': 'Log-HAR, smearing', 'log_har_dvol_smear': 'Log-HAR + DVOL, smearing',
             'qlike_har': 'QLIKE-fitted HAR', 'qlike_har_dvol': 'QLIKE-fitted HAR + DVOL',
             'ridge_qlike_har_dvol': 'QLIKE-fitted HAR + DVOL, ridge', 'ewma_094': 'EWMA',
             'garch_11': 'GARCH', 'egarch_111': 'EGARCH', 'bates_granger': 'Bates-Granger style', 'mz_dvol': 'MZ-corrected DVOL'}
    plt.rcParams.update({'svg.hashsalt': 'declared-review-v2', 'font.family': 'DejaVu Sans', 'font.size': 10})
    fig, axes = plt.subplots(2, 1, figsize=(9.5, 8.6), gridspec_kw={'height_ratios': [4, 1]}, constrained_layout=True)
    means = [metrics['qlike_'+name]['mean'] for name in models]
    colors = ['#2c7f8d' if name in ('dvol', 'log_har_dvol_smear') else '#8b98a1' for name in models]
    axes[0].barh([names[name] for name in models], means, color=colors)
    axes[0].invert_yaxis()
    axes[0].set_xlim(0, max(means)*1.14)
    axes[0].set_xlabel('Mean QLIKE loss; lower is better')
    axes[0].set_title(f"Model choice changes the point ranking\n{len(models)} matched BTC models, n = {result['benchmarks']['summary']['n']:,}", loc='left')
    for i, value in enumerate(means):
        axes[0].text(value+.002, i, f'{value:.5f}', va='center', fontsize=9)
    focal = metrics['focus_qlike_har_dvol_minus_qlike_har']
    sub = result['focal_inference_references'][0]['subsampling']
    for i, (label, interval) in enumerate((('Stationary bootstrap', focal['confidence_interval_95']),
                                         ('Subsampling, failed size checks', sub['confidence_interval_95']))):
        axes[1].plot(interval, [i, i], lw=2, color=f'C{i}')
        axes[1].plot(focal['mean'], i, 'o', color=f'C{i}')
    axes[1].set_yticks([0, 1], ['Stationary bootstrap', 'Subsampling, failed size checks'])
    axes[1].set_ylim(1.5, -.5)
    axes[1].axvline(0, color='#666', lw=.7)
    axes[1].set_xlabel('Descriptive 95% interval for the mean paired QLIKE difference')
    axes[1].set_title('QLIKE-fitted HAR + DVOL minus QLIKE-fitted HAR', loc='left', fontsize=10)
    for ax in axes:
        ax.spines[['top', 'right']].set_visible(False)
        ax.grid(axis='x', alpha=.18)
        ax.set_axisbelow(True)
    for extension in ('svg', 'png', 'pdf'):
        metadata = {'Date': None} if extension == 'svg' else {'CreationDate': None, 'ModDate': None} if extension == 'pdf' else None
        fig.savefig(output/('review-variance.'+extension), metadata=metadata, dpi=160)
    plt.close(fig)


def write_review_report(root: Path, output: Path, *, progress=print) -> dict:
    started = time.monotonic()
    spec, declaration = load_review_spec(root)
    output.mkdir(parents=True, exist_ok=True)
    v1 = write_incremental_report(root, output/'v1-rebuild')
    original = json.loads((root/'report/incremental-results.json').read_text(encoding='utf-8'))
    rebuilt = json.loads((output/'v1-rebuild/summary.json').read_text(encoding='utf-8'))
    v1_validation = validate_v1_rebuild(original, rebuilt)
    historical = load_study(root/'protocol/historical-variance.json')
    inputs = read_study(root/spec['inputs'], historical)
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    cases = build_cases(inputs.prices, inputs.dvol, historical['origin_start_ms'], historical['origin_end_ms'])
    windows = window_check(cases, plan, spec)
    benchmarks = benchmark_records(cases, inputs.prices, plan, spec, progress=progress)
    common = benchmarks['common_records']
    summary = benchmark_summary(common, spec, plan)
    result = {'kind': 'exploratory_review_v2_results', 'formal_claim_eligible': False, 'specification': spec,
              'declaration': declaration, 'input_manifest_hash': digest(inputs.manifest), 'v1_summary_unchanged': True,
              'v1_rebuild_validation': v1_validation,
              'window_check': {k: v for k, v in windows.items() if k != 'records'},
              'benchmarks': {**{k: v for k, v in benchmarks.items() if k not in ('records', 'common_records')}, 'summary': summary},
              'v1_inference_references': reference_table(v1['records'], [('dvol', 'har'), ('har_dvol', 'har'), ('dvol', 'har_dvol')], spec),
              'phases': phase_summary(v1['records']), 'mcs': model_confidence_set(common, spec)}
    result['focal_inference_references'] = (
        reference_table(common, [('qlike_har_dvol', 'qlike_har')], spec)
        if len(common) > 60 and summary['calendar_gaps'] == 0 else
        {'status': 'withheld', 'reason': 'insufficient consecutive balanced origins'})
    progress({'stage': 'calibration_and_power', 'seconds': round(time.monotonic()-started, 1)})
    result['inference_calibration'] = calibration_report(spec)
    primary = [r['losses']['har_dvol']-r['losses']['har'] for r in v1['records']]
    result['empirical_power'] = empirical_power(primary, spec)
    result['implementation_sha256'] = {name: hashlib.sha256((root/'src/implied_calibration'/name).read_bytes()).hexdigest()
                                       for name in ('review_models.py', 'inference_reference.py', 'review_report.py')}
    (output/'execution.json').write_text(json.dumps({'runtime_seconds': round(time.monotonic()-started, 2)})+'\n', encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+'\n', encoding='utf-8')
    (output/'records.json').write_text(json.dumps(benchmarks['records'], sort_keys=True, allow_nan=False)+'\n', encoding='utf-8')
    with (output/'forecasts.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['origin_ms', 'actual']+['forecast_'+name for name in spec['models']])
        writer.writeheader()
        for row in benchmarks['records']:
            writer.writerow({'origin_ms': row['origin_ms'], 'actual': row['actual'],
                             **{'forecast_'+name: row['forecasts'].get(name) for name in spec['models']}})
    (output/'results.md').write_text(markdown(result), encoding='utf-8')
    headline(result, output)
    return result
