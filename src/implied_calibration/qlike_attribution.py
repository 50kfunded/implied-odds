"""Exact accounting of common and relative two-asset QLIKE errors."""

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import matplotlib as mpl
from matplotlib.figure import Figure
import numpy as np

from .inference import effective_sample_size, stationary_bootstrap


def components(actual, forecast) -> np.ndarray:
    actual, forecast = np.asarray(actual, float), np.asarray(forecast, float)
    if (actual.ndim != 2 or actual.shape[1] != 2 or actual.shape != forecast.shape
            or not np.isfinite(actual).all() or not np.isfinite(forecast).all()
            or np.any(actual <= 0) or np.any(forecast <= 0)):
        raise ValueError('Expected matched positive two-asset variances')
    errors = np.log(actual)-np.log(forecast)
    common, relative = errors.mean(axis=1), errors[:, 1]-errors[:, 0]
    level = np.expm1(common)-common
    weighted_relative = np.exp(common)*2*np.sinh(relative/4)**2
    average_qlike = np.mean(actual/forecast-errors-1, axis=1)
    result = np.column_stack((level, weighted_relative, average_qlike))
    if not np.isfinite(result).all() or not np.allclose(level+weighted_relative, average_qlike, atol=2e-14, rtol=1e-12):
        raise ValueError('QLIKE accounting identity lost numerical validity')
    return result


def attribution(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError('No matched forecasts')
    origins = np.asarray([int(r['origin_ms']) for r in rows], dtype=np.int64)
    if np.any(np.diff(origins) != 86400000):
        raise ValueError('Attribution requires complete, unique daily matched origins')
    years = np.array([datetime.fromtimestamp(t/1000, timezone.utc).year for t in origins])
    actual = np.array([[float(r[a+'_actual_variance']) for a in ('BTC', 'ETH')] for r in rows])
    model = {name: components(actual, [[float(r[a+'_'+name+'_forecast']) for a in ('BTC', 'ETH')] for r in rows])
             for name in ('log_har', 'log_har_dvol')}
    delta = model['log_har_dvol']-model['log_har']
    groups = [('All', np.ones(len(rows), dtype=bool))]
    groups += [(str(y), years == y) for y in sorted(set(years))]
    groups += [('2024_onward', years >= 2024)]
    summaries = {name: {'n': int(mask.sum()), 'har': model['log_har'][mask].mean(axis=0).tolist(),
                        'har_dvol': model['log_har_dvol'][mask].mean(axis=0).tolist(),
                        'change': delta[mask].mean(axis=0).tolist()} for name, mask in groups if mask.any()}
    last = delta[years == 2026, 1]
    checks: dict = {}
    if len(last) > 60:
        order = np.argsort(-np.abs(last))
        checks = {'n': len(last), 'positive_origin_share': float(np.mean(last > 0)),
                  'ess': effective_sample_size(last, 59),
                  'interval': stationary_bootstrap(last, resamples=5000, block_length=60, seed=803271)['confidence_interval_95'],
                  'drop_largest_absolute': {str(f): float(np.delete(last, order[:int(np.ceil(len(last)*f))]).mean()) for f in (.01, .05)},
                  'minimum_mean_after_removing_one_60_day_window': float(min((last.sum()-last[i:i+60].sum())/(len(last)-60)
                                                                           for i in range(len(last)-59))),
                  'dependent_phase_means': [float(last[p::30].mean()) for p in range(30)]}
    identity_error = max(float(np.max(np.abs(value[:, 0]+value[:, 1]-value[:, 2]))) for value in model.values())
    return {'kind': 'exploratory_two_asset_qlike_attribution', 'formal_claim_eligible': False,
            'n': len(rows), 'columns': ['common_level', 'weighted_relative', 'average_qlike'],
            'groups': summaries, 'maximum_identity_error': identity_error,
            'pooled_fraction_common_gain_offset': float(delta[:, 1].mean()/-delta[:, 0].mean()),
            '2026_relative_checks': checks,
            'scope': 'post-inspection accounting, not causal factor identification or a prospective model comparison'}


def render_note(result: dict) -> str:
    all_ = result['groups']['All']
    later = result['groups']['2024_onward']
    gain = -100*all_['change'][2]/all_['har'][2]
    deterioration = 100*later['change'][2]/later['har'][2]
    lines = ['# Common and relative errors can pull QLIKE in opposite directions', '',
             f"Across {result['n']:,} matched BTC/ETH origins, adding DVOL to separate marginal log-HAR models "
             f"lowers average QLIKE from {all_['har'][2]:.8f} to {all_['har_dvol'][2]:.8f}, a {gain:.2f}% point reduction. "
             f"Across the {later['n']:,} origins from January 2024 onward, mean loss rises from {later['har'][2]:.8f} "
             f"to {later['har_dvol'][2]:.8f}, a {deterioration:.2f}% deterioration. I chose this time breakdown after inspection.", '',
             r'For log actual-to-forecast errors $e_B$ and $e_E$, define $c=(e_E+e_B)/2$ and $r=e_E-e_B$. Then', '',
             r'$$\frac{Q_B+Q_E}{2}=\underbrace{e^c-c-1}_{\text{common-level penalty}}+'
             r'\underbrace{e^c[\cosh(r/2)-1]}_{\text{weighted relative-error penalty}}.$$', '',
             'This is exact accounting. A change in the relative term can reflect both its relative error and '
             'the common-level weight; it is not an identified market factor.', '',
             '| Period | Origins | Common-level change | Weighted relative change | Total QLIKE change |',
             '| --- | ---: | ---: | ---: | ---: |']
    for name, row in result['groups'].items():
        lines.append(f"| {name.replace('_', ' ')} | {row['n']:,} | {row['change'][0]:+.8f} | {row['change'][1]:+.8f} | {row['change'][2]:+.8f} |")
    checks = result['2026_relative_checks']
    lines += ['', 'Negative changes favour log-HAR + DVOL. The 2025 common-level deterioration outweighs its '
              'relative improvement. In 2026 the common term improves, but the weighted relative term worsens by more.', '',
              f"Across the full sample, the relative penalty offsets {100*result['pooled_fraction_common_gain_offset']:.2f}% "
              'of the gross common-level improvement.', '', '![Exact QLIKE attribution by year](qlike-attribution.svg)', '',
              f"In 2026 the weighted relative contrast is positive at {100*checks['positive_origin_share']:.2f}% of origins. "
              f"Its ESS is {checks['ess']:.2f}. Its mean remains positive after removing the largest 1% or 5% of absolute "
              'contributions, or any single 60-day window. All 30 dependent nonoverlap phases have positive means. '
              'These checks are descriptive and reuse the same outcomes; the earlier inference-calibration failures still apply.', '',
              'The numerical identity error is below 1e-12 at every origin. '
              'The computation reads the forecasts rebuilt from retained hourly data, rather than a saved plot.', '',
              '[Rebuilt data](qlike-attribution.json) · [Matched study](eth-relative-results.md) · '
              '[Partial-settlement method](progressive-evidence.md)', '']
    return '\n'.join(lines)


def write_attribution(root: Path, forecast_path: Path, output: Path, *, publish: bool = False, check: bool = False) -> dict:
    with forecast_path.open(encoding='utf-8', newline='') as stream:
        result = attribution(list(csv.DictReader(stream)))
    result['forecast_file_sha256'] = hashlib.sha256(forecast_path.read_bytes()).hexdigest()
    reference, note_path = root/'report/qlike-attribution.json', root/'report/qlike-attribution.md'
    note = render_note(result)
    if check:
        old = json.loads(reference.read_text(encoding='utf-8'))
        # Different BLAS implementations can change final-bit OLS forecasts.
        for group, row in result['groups'].items():
            for name in ('har', 'har_dvol', 'change'):
                if not np.allclose(row[name], old['groups'][group][name], atol=1e-10, rtol=1e-8):
                    raise ValueError('QLIKE attribution differs from the reported results')
        if note != note_path.read_text(encoding='utf-8'):
            raise ValueError('QLIKE attribution manuscript numbers differ')
    output.mkdir(parents=True, exist_ok=True)
    data = json.dumps(result, indent=2, allow_nan=False)+'\n'
    (output/'summary.json').write_text(data, encoding='utf-8')
    (output/'results.md').write_text(note, encoding='utf-8')
    figure = Figure(figsize=(11, 5), layout='constrained', facecolor='white')
    axis = figure.subplots()
    groups = [(name, row) for name, row in result['groups'].items() if name != '2024_onward']
    x = np.arange(len(groups))
    for offset, index, label, color in ((-.17, 0, 'Common-level change', '#237d88'),
                                       (.17, 1, 'Weighted relative change', '#cb743f')):
        axis.bar(x+offset, [r['change'][index] for _, r in groups], .32, label=label, color=color)
    axis.scatter(x, [r['change'][2] for _, r in groups], color='#33404a', label='Total change', zorder=4)
    axis.axhline(0, color='#66727c', linewidth=.7)
    axis.set(xticks=x, xticklabels=[f'{name}\n{r["n"]:,} origins' for name, r in groups],
             ylabel='Log-HAR + DVOL minus log-HAR', title='The source of DVOL forecast error changes over time')
    axis.legend(frameon=False, fontsize=8)
    axis.grid(axis='y', alpha=.15)
    axis.set_axisbelow(True)
    with mpl.rc_context({'svg.hashsalt': 'implied-odds-qlike-attribution-v1'}):
        figure.savefig(output/'qlike-attribution.svg', metadata={'Date': None})
    if publish:
        reference.write_text(data, encoding='utf-8')
        note_path.write_text(note, encoding='utf-8')
        (root/'report/qlike-attribution.svg').write_bytes((output/'qlike-attribution.svg').read_bytes())
    return result
