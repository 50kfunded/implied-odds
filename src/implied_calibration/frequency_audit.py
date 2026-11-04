"""Rebuild nested variance proxies and refit the same completed-label models."""

import csv
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from .historical_data import load_study, read_study
from .relative_variance import PairedCase, RATIO_MODELS, evaluate_pairs, load_relative_spec
from .review_models import utc_ms
from .variance_study import DAY_MS, HOUR_MS


STEP_MS = 300000


def read_five_minute(root: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    folder = root/'recordings/frequency-2026-10-28'
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    ticks = np.arange(manifest['start_ms'], manifest['end_exclusive_ms'], STEP_MS, dtype=np.int64)
    close = np.full((len(ticks), 2), np.nan)
    audit = {}
    for index, asset in enumerate(('BTC', 'ETH')):
        record = manifest['files'][asset]
        path = folder/record['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Five-minute capture hash differs')
        seen: set[int] = set()
        responses = 0
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            for line in stream:
                page = json.loads(line)
                raw = page['response_text'].encode('utf-8')
                if hashlib.sha256(raw).hexdigest() != page['sha256'] or page['receipt']['response_sha256'] != page['sha256']:
                    raise ValueError('Original response hash differs')
                receipt = page['receipt']
                if (receipt['http_status'] != 200 or receipt['params']['instrument_name'] != asset+'-PERPETUAL'
                        or receipt['params']['resolution'] != '5'):
                    raise ValueError('Unexpected five-minute source')
                body = json.loads(raw)
                if 'error' in body:
                    raise ValueError('API failure in accepted source page')
                data = body['result']
                time = np.array(data['ticks'], dtype=np.int64)
                position = (time-ticks[0])//STEP_MS
                if (np.any(position < 0) or np.any(position >= len(ticks))
                        or not np.array_equal(ticks[position], time)
                        or len(set(time.tolist())) != len(time) or seen.intersection(time.tolist())):
                    raise ValueError('Duplicate, conflicting or misaligned five-minute ticks')
                ohlcv = np.array([data[k] for k in ('open', 'high', 'low', 'close', 'volume')], float).T
                if (ohlcv.shape != (len(time), 5) or not np.isfinite(ohlcv).all()
                        or np.any(ohlcv[:, :4] <= 0) or np.any(ohlcv[:, 4] < 0)
                        or np.any(ohlcv[:, 1] < ohlcv[:, :4].max(axis=1))
                        or np.any(ohlcv[:, 2] > ohlcv[:, :4].min(axis=1))):
                    raise ValueError('Invalid five-minute OHLCV')
                close[position, index] = ohlcv[:, 3]
                seen.update(time.tolist())
                responses += 1
        if responses != record['responses'] or len(seen) != len(ticks):
            raise ValueError('Incomplete five-minute history')
        audit[asset] = {'candles': len(seen), 'responses': responses, 'missing': 0, 'sha256': record['sha256']}
    return ticks, close, audit


def nested_daily(close: np.ndarray, minutes: int) -> np.ndarray:
    if (minutes not in (5, 15, 60) or close.ndim != 2 or close.shape[1] != 2
            or (len(close)-1) % 288 or not np.isfinite(close).all() or np.any(close <= 0)):
        raise ValueError('Expected complete two-asset five-minute boundary closes')
    returns = np.diff(np.log(close[::minutes//5]), axis=0)
    return np.sum(returns.reshape(-1, 1440//minutes, 2)**2, axis=1)*365


def cases_from_daily(daily: np.ndarray, first_boundary: int, origins: np.ndarray, dvol: np.ndarray) -> list[PairedCase]:
    positions = (origins-first_boundary)//DAY_MS
    if (not np.array_equal(first_boundary+positions*DAY_MS, origins)
            or positions.min() < 30 or positions.max()+30 > len(daily)
            or dvol.shape != (len(origins), 2) or np.any(daily <= 0) or not np.isfinite(daily).all()):
        raise ValueError('Invalid feature and target boundaries')
    prefix = np.vstack((np.zeros(2), np.cumsum(daily, axis=0)))
    features = np.stack([(prefix[positions]-prefix[positions-days])/days for days in (1, 7, 30)], axis=2)
    features = np.concatenate((features, dvol[:, :, None]), axis=2)
    actual = (prefix[positions+30]-prefix[positions])/30
    return [PairedCase(int(time), int(time), int(time+30*DAY_MS),
                       (tuple(x[0]), tuple(x[1])), tuple(y)) for time, x, y in zip(origins, features, actual)]


def write_frequency_report(root: Path, forecasts: Path, output: Path, *, publish: bool = False, check: bool = False) -> dict:
    spec, _ = load_relative_spec(root)
    ticks, close, source_audit = read_five_minute(root)
    inputs = {a: read_study(root/spec['inputs'][a], load_study(root/spec['source_configs'][a])) for a in ('BTC', 'ETH')}
    for index, asset in enumerate(('BTC', 'ETH')):
        for time, candle in inputs[asset].prices.items():
            position = (time+HOUR_MS-STEP_MS-int(ticks[0]))//STEP_MS
            if close[position, index] != candle[3]:
                raise ValueError('Nested hourly close differs from the earlier retained source')
        source_audit[asset]['matching_original_hourly_closes'] = len(inputs[asset].prices)
    origins = np.arange(utc_ms(spec['case_start']), utc_ms(spec['evaluation_end'])+DAY_MS, DAY_MS, dtype=np.int64)
    dvol = np.array([[(inputs[a].dvol[int(t)-DAY_MS][3]/100)**2 for a in ('BTC', 'ETH')] for t in origins])
    with forecasts.open(encoding='utf-8', newline='') as stream:
        old = list(csv.DictReader(stream))
    old_by_origin = {int(row['origin_ms']): row for row in old}
    result: dict = {'kind': 'exploratory_sampling_resolution_refit', 'formal_claim_eligible': False,
              'source_audit': source_audit, 'n': len(old), 'frequencies': {},
              'scope': 'same 730 completed labels; all selected frequencies retained; no frequency chosen by results'}
    for minutes in (5, 15, 60):
        cases = cases_from_daily(nested_daily(close, minutes), int(ticks[0])+STEP_MS, origins, dvol)
        rows, excluded = evaluate_pairs(cases, spec)
        if excluded or [r['origin_ms'] for r in rows] != list(old_by_origin):
            raise ValueError('Frequency cohorts do not match')
        frozen_losses: dict[str, list[float]] = {name: [] for name in RATIO_MODELS}
        for row in rows:
            for name in RATIO_MODELS:
                frozen_losses[name].append((row['target_log_ratio']-float(old_by_origin[row['origin_ms']]['forecast_'+name]))**2)
        means = {name: float(np.mean([r['ratio_losses'][name] for r in rows])) for name in RATIO_MODELS}
        frozen = {name: float(np.mean(values)) for name, values in frozen_losses.items()}
        result['frequencies'][str(minutes)] = {'refitted_ratio_mse': means, 'unchanged_hourly_forecast_mse': frozen,
            'refitted_dvol_increment': means['ratio_har_dvol']-means['ratio_har'],
            'unchanged_hourly_dvol_increment': frozen['ratio_har_dvol']-frozen['ratio_har']}
    lines = ['# The apparent frequency reversal does not survive refitting', '',
             f"I rebuild {result['n']:,} matched origins using nested 5-, 15- and 60-minute closes from the same "
             'Deribit perpetual histories. Each frequency gets its own past variance features, 30-day targets and '
             '730 completed training labels. I also score unchanged hourly forecasts against each target.', '',
             '| Return grid, minutes | Refitted ratio HAR | Refitted ratio HAR + DVOL | Added DVOL difference | Unchanged hourly difference |',
             '| --- | ---: | ---: | ---: | ---: |']
    for minutes, row in result['frequencies'].items():
        lines.append(f"| {minutes} | {row['refitted_ratio_mse']['ratio_har']:.8f} | "
                     f"{row['refitted_ratio_mse']['ratio_har_dvol']:.8f} | {row['refitted_dvol_increment']:+.8f} | "
                     f"{row['unchanged_hourly_dvol_increment']:+.8f} |")
    lines += ['', 'Changing the target while holding hourly forecasts fixed changes the relative-risk ranking. '
              'After refitting the entire pipeline, adding DVOL lowers point error at all three frequencies. '
              'The tempting frequency-induced sign reversal therefore fails this check. These are inspected '
              'historical outcomes, and neither sign supplies a calibrated significance claim.', '',
              'Hourly nesting matches every retained hourly close exactly. The source retains trade candles, '
              'rather than a certified efficient-price series; five-minute variance still misses the intrabar path. '
              'No returns are clipped or replaced. The archived research also examined hourly grid phases and '
              'selected event days; those inspected diagnostics do not establish a new forecast effect.', '',
              '[Computed results and source counts](frequency-results.json) · '
              '[Retained response hashes](../recordings/frequency-2026-10-28/manifest.json) · '
              '[Main method](progressive-evidence.md)', '']
    note = '\n'.join(lines)
    reference = root/'report/frequency-results.json'
    note_path = root/'report/frequency-results.md'
    if check:
        previous = json.loads(reference.read_text(encoding='utf-8'))
        if result['source_audit'] != previous['source_audit'] or result['n'] != previous['n']:
            raise ValueError('Frequency source counts differ')
        for minutes, row in result['frequencies'].items():
            if not np.allclose(list(row['refitted_ratio_mse'].values()), list(previous['frequencies'][minutes]['refitted_ratio_mse'].values()), rtol=1e-8, atol=1e-10):
                raise ValueError('Frequency forecasts differ')
        if note != note_path.read_text(encoding='utf-8'):
            raise ValueError('Frequency manuscript numbers differ')
    output.mkdir(parents=True, exist_ok=True)
    data = json.dumps(result, indent=2, allow_nan=False)+'\n'
    (output/'summary.json').write_text(data, encoding='utf-8')
    (output/'results.md').write_text(note, encoding='utf-8')
    if publish:
        reference.write_text(data, encoding='utf-8')
        note_path.write_text(note, encoding='utf-8')
    return result
