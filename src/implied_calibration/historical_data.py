"""I retain a separate historical DVOL study without creating prospective records."""

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Callable

from .client import PublicClient, utc_ms, validate_response
from .collection import FileLease, write_status
from .history import HOUR_MS, fetch_history
from .raw import RawWriter, read_raw
from .store import digest


DAY_MS = 24 * HOUR_MS
DVOL = 'public/get_volatility_index_data'
PRICES = 'public/get_tradingview_chart_data'
LOGGER = logging.getLogger(__name__)


def load_study(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('kind') != 'retrospective_dvol_variance' or value.get('schema_version') != 1:
        raise ValueError('Invalid historical variance study kind or version')
    for field in ('origin_start_ms', 'origin_end_ms', 'price_start_ms', 'price_end_ms', 'dvol_start_ms',
                  'dvol_end_ms', 'recalibration_start_ms', 'nonoverlap_anchor_ms'):
        if type(value.get(field)) is not int or value[field] < 0:
            raise ValueError('Invalid historical UTC millisecond field: ' + field)
    if (value['origin_start_ms'] % DAY_MS or value['origin_end_ms'] % DAY_MS
            or value['origin_start_ms'] >= value['origin_end_ms'] or value['horizon_hours'] != 720
            or value['annual_hours'] != 8760 or value['origin_spacing_ms'] != DAY_MS
            or value['price_start_ms'] > value['origin_start_ms'] - 721 * HOUR_MS
            or value['price_end_ms'] < value['origin_end_ms'] + 721 * HOUR_MS
            or value['dvol_start_ms'] > value['origin_start_ms'] - 2 * DAY_MS
            or value['dvol_end_ms'] < value['origin_end_ms']
            or value['price_resolution'] != '60' or value['dvol_resolution'] != '1D'
            or value['price_instrument'] != 'BTC-PERPETUAL' or value['dvol_currency'] != 'BTC'
            or value['recalibration_completed_origins'] != 365 or value['bootstrap_resamples'] < 2000
            or value['expected_block_days'] < 30 or value['hac_lags_days'] < 29
            or value['minimum_effective_sample_size'] < 60):
        raise ValueError('Historical study timing, source, horizon or inference choices differ')
    return value


def fetch_dvol(client: PublicClient, start_ms: int, end_ms: int, *, resolution: str = '1D',
               sleep: Callable[[float], None] = time.sleep, max_pages: int = 512) -> dict:
    if (type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms
            or resolution not in ('1D', '3600') or type(max_pages) is not int or max_pages < 1):
        raise ValueError('Invalid historical DVOL interval, resolution or page limit')
    cursor = end_ms - 1
    rows = 0
    for page in range(max_pages):
        if page:
            sleep(10.)
        response = client.request(DVOL, {'currency': 'BTC', 'start_timestamp': start_ms,
                                        'end_timestamp': cursor, 'resolution': resolution})
        result = response.result
        if any(not start_ms <= row[0] <= cursor for row in result['data']):
            raise ValueError('DVOL candle falls outside its retained request')
        rows += len(result['data'])
        continuation = result['continuation']
        if continuation is None or continuation < start_ms:
            return {'pages': page + 1, 'candle_rows': rows, 'resolution': resolution}
        if continuation >= cursor or not result['data']:
            raise ValueError('DVOL pagination made no progress')
        cursor = continuation
    raise ValueError('DVOL pagination reached its declared page limit')


def capture_study(output: Path, config_path: Path, *, client_factory=PublicClient,
                  now_ms=utc_ms, sleep=time.sleep) -> dict:
    from .cli import provenance

    config = load_study(config_path)
    details = provenance()
    if details['git_commit'] is None or details['working_tree_dirty'] is not False:
        raise ValueError('I need a clean committed historical study before downloading inputs')
    if config['price_end_ms'] > now_ms():
        raise ValueError('Historical price capture must end before actual capture time')
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('I retain each historical capture in a fresh directory')
    output.mkdir(parents=True, exist_ok=True)
    state = {'status': 'capturing', 'started_at_ms': now_ms(), 'attempts': 0, 'failed_attempts': 0,
             'config_hash': digest(config), 'git_commit': details['git_commit']}
    state_path = output / 'capture.json'
    write_status(state_path, state)

    def client_for(folder):
        writer = RawWriter(output / folder / 'raw')
        def retain(row):
            writer.append(row.to_dict())
            state['attempts'] += 1
            state['failed_attempts'] += int(not row.ok)
            state['last_endpoint'] = row.endpoint
            state['updated_at_ms'] = row.fetched_at_ms
            write_status(state_path, state)
            LOGGER.info('Retained historical attempt=%s endpoint=%s ok=%s', state['attempts'], row.endpoint, row.ok)
        return client_factory(on_attempt=retain)

    def run(folder, callback):
        client = client_for(folder)
        try:
            return callback(client)
        finally:
            client.close()

    try:
        with FileLease(output / '.capture.lock'):
            state['dvol'] = run('dvol', lambda c: fetch_dvol(c, config['dvol_start_ms'], config['dvol_end_ms'], sleep=sleep))
            sleep(10.)
            state['prices'] = run('prices', lambda c: fetch_history(c, config['price_start_ms'], config['price_end_ms'], sleep=sleep))
            for day in config['alignment_check_days_ms']:
                sleep(10.)
                run('alignment', lambda c, day=day: fetch_dvol(c, day, day + DAY_MS, resolution='3600', sleep=sleep))
                sleep(10.)
                run('alignment', lambda c, day=day: c.request(PRICES, {'instrument_name': 'BTC-PERPETUAL',
                    'resolution': '1D', 'start_timestamp': day, 'end_timestamp': day + DAY_MS - 1}))
        files = {p.relative_to(output).as_posix(): {'bytes': p.stat().st_size,
                  'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.rglob('*.jsonl.gz'))}
        manifest = {'schema_version': 1, 'kind': 'historical_dvol_variance_inputs', 'config_hash': digest(config),
                    'git_commit': details['git_commit'], 'captured_from_ms': state['started_at_ms'],
                    'captured_until_ms': now_ms(), 'files': files,
                    'vintage': config['vintage'], 'capture_attempts': state['attempts'],
                    'failed_attempts': state['failed_attempts']}
        write_status(output / 'manifest.json', manifest)
        state['status'] = 'completed'
        return manifest
    except (OSError, ValueError, RuntimeError) as exc:
        state.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    except KeyboardInterrupt:
        state['status'] = 'interrupted'
        raise
    finally:
        state['updated_at_ms'] = now_ms()
        write_status(state_path, state)


@dataclass(frozen=True)
class HistoricalInputs:
    prices: dict[int, tuple[float, float, float, float]]
    dvol: dict[int, tuple[float, float, float, float]]
    alignment_dvol: dict[int, tuple[float, float, float, float]]
    alignment_prices: dict[int, tuple[float, float, float, float]]
    manifest: dict
    duplicate_candles: int
    failed_attempts: int


def read_study(manifest_path: Path, config: dict) -> HistoricalInputs:
    root = Path(manifest_path).resolve().parent
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if (manifest.get('kind') != 'historical_dvol_variance_inputs' or manifest.get('schema_version') != 1
            or manifest.get('config_hash') != digest(config) or not isinstance(manifest.get('files'), dict)
            or type(manifest.get('captured_from_ms')) is not int or type(manifest.get('captured_until_ms')) is not int
            or not 0 <= manifest['captured_from_ms'] <= manifest['captured_until_ms']):
        raise ValueError('Historical input manifest differs from the declared study')
    inventory = {p.relative_to(root).as_posix() for name in ('dvol', 'prices', 'alignment')
                 for p in (root / name).rglob('*') if p.is_file()}
    if inventory != set(manifest['files']):
        raise ValueError('Historical input inventory differs; I reject unlisted files')
    mappings: dict[str, dict] = {name: {} for name in ('prices', 'dvol', 'alignment_dvol', 'alignment_prices')}
    duplicates = failed = 0
    for relative, proof in sorted(manifest['files'].items()):
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or Path(relative).is_absolute():
            raise ValueError('Historical input path escapes the recording folder')
        body = path.read_bytes()
        if proof != {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}:
            raise ValueError('Historical input hash or size differs: ' + relative)
        issues = []
        for row in read_raw(path, on_issue=issues.append):
            stamp = row.get('fetched_at_ms')
            if (type(stamp) is not int or not manifest['captured_from_ms'] <= stamp <= manifest['captured_until_ms']
                    or row.get('response_received_ms') != stamp or type(row.get('request_sent_ms')) is not int
                    or not 0 <= row['request_sent_ms'] <= stamp or type(row.get('ok')) is not bool):
                raise ValueError('Invalid retained historical response timing')
            if not row['ok']:
                failed += 1
                continue
            endpoint, params = row.get('endpoint'), row.get('params')
            if endpoint not in (DVOL, PRICES) or not isinstance(params, dict):
                raise ValueError('Unexpected historical endpoint or parameters')
            result = validate_response(endpoint, row.get('http_status'), row.get('body'))
            resolution = params.get('resolution')
            is_dvol = endpoint == DVOL
            expected = {'currency', 'resolution', 'start_timestamp', 'end_timestamp'} if is_dvol else {'instrument_name', 'resolution', 'start_timestamp', 'end_timestamp'}
            if (set(params) != expected or params.get('currency', 'BTC') != 'BTC'
                    or params.get('instrument_name', 'BTC-PERPETUAL') != 'BTC-PERPETUAL'
                    or any(type(params[k]) is not int for k in ('start_timestamp', 'end_timestamp'))
                    or not 0 <= params['start_timestamp'] < params['end_timestamp']):
                raise ValueError('Unexpected retained historical source or interval')
            folder = Path(relative).parts[0]
            if folder == 'dvol' and is_dvol and resolution == '1D':
                destination, duration = mappings['dvol'], DAY_MS
            elif folder == 'prices' and not is_dvol and resolution == '60':
                destination, duration = mappings['prices'], HOUR_MS
            elif folder == 'alignment' and is_dvol and resolution == '3600':
                destination, duration = mappings['alignment_dvol'], HOUR_MS
            elif folder == 'alignment' and not is_dvol and resolution == '1D':
                destination, duration = mappings['alignment_prices'], DAY_MS
            else:
                raise ValueError('Historical source folder or resolution differs')
            candles = result['data'] if is_dvol else list(zip(result.get('ticks', []), result.get('open', []),
                result.get('high', []), result.get('low', []), result.get('close', [])))
            for opened, *values in candles:
                alignment_daily_price = folder == 'alignment' and not is_dvol
                clock_offset = 8 * HOUR_MS if alignment_daily_price else 0
                in_request = (opened <= params['end_timestamp'] and opened + duration > params['start_timestamp']
                              if alignment_daily_price else params['start_timestamp'] <= opened <= params['end_timestamp'])
                if ((opened - clock_offset) % duration or not in_request
                        or opened + duration > stamp):
                    raise ValueError('Historical candle timing or completed-bar rule differs')
                values = tuple(float(v) for v in values)
                if opened in destination:
                    if destination[opened] != values:
                        raise ValueError('Conflicting historical candle revision')
                    duplicates += 1
                destination[opened] = values
        if issues:
            raise ValueError('Historical raw input has corrupt or truncated records')
    if failed != manifest.get('failed_attempts'):
        raise ValueError('Historical failed-attempt count differs')
    return HistoricalInputs(**mappings, manifest=manifest, duplicate_candles=duplicates, failed_attempts=failed)
