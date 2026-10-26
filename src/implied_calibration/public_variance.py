"""A narrow public-state adapter retains immutable evidence and deadline checks."""

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time

from .client import PublicClient, utc_ms
from .collection import FileLease
from .historical_data import DVOL, PRICES
from .raw import RawWriter, read_raw
from .research_plan import load_plan
from .store import digest
from .variance_forward import (ForecastUnavailable, VarianceForwardJob, forward_summary,
                               read_immutable, verify_source_proof, write_immutable)
from .variance_study import DAY_MS, HOUR_MS


def allowed_public_path(relative: str) -> bool:
    patterns = (r'(forecasts|receipts|scores|score-receipts)/[0-9]+\.json',
                r'captures/[0-9]+-[0-9a-f]+/raw/[0-9-]+\.jsonl\.gz',
                r'publications/[0-9]+-[0-9]+\.json', r'observations/[0-9]+-[0-9]+\.json',
                r'anchors/[0-9]+\.json\.ots', r'failures/[0-9]+-[0-9]+\.json',
                r'failed-captures/[0-9]+-[0-9a-f]+/raw/[0-9-]+\.jsonl\.gz')
    return any(re.fullmatch(pattern, relative) is not None for pattern in patterns)


def transfer_state(source: Path, target: Path, *, maximum_bytes: int = 500_000_000) -> dict:
    if Path(source).is_symlink() or Path(target).is_symlink():
        raise ValueError('Public state roots cannot be symlinks')
    source, target = Path(source).resolve(), Path(target).resolve()
    files = []
    for path in source.rglob('*'):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError('Public state cannot contain symlinks')
        if path.is_file() and allowed_public_path(relative):
            if not path.resolve().is_relative_to(source):
                raise ValueError('Public state escapes its root')
            files.append((relative, path))
    return _transfer_files(source, target, files, maximum_bytes)


def _transfer_files(source: Path, target: Path, files: list, maximum_bytes: int) -> dict:
    for relative, path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('Public source evidence escapes its root')
        if not allowed_public_path(relative):
            raise ValueError('Expected a whitelisted public evidence path')
    total = sum(path.stat().st_size for _, path in files)
    if total > maximum_bytes:
        raise ValueError('Public state reached its declared size budget')
    copied = 0
    for relative, path in files:
        destination = target/relative
        if not destination.resolve().is_relative_to(target):
            raise ValueError('Public state destination escapes its root')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != path.read_bytes():
                raise ValueError('Refusing to replace a public immutable state file')
        else:
            shutil.copyfile(path, destination)
            copied += 1
    return {'files': len(files), 'bytes': total, 'new_files': copied}


def verify_publication(record: dict, receipt: dict, plan: dict) -> None:
    origin = record['origin_ms']
    created = datetime.fromisoformat(receipt['created_at'].replace('Z', '+00:00'))
    if created.tzinfo is None:
        raise ValueError('Expected a GitHub server timestamp with an explicit offset')
    stamp = int(created.timestamp()*1000)
    if (receipt['forecast_hash'] != record['content_hash']
            or record['frozen_at_ms'] > stamp+999
            or stamp+999 > origin-plan['future']['freeze_end_before_origin_ms']
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', receipt['artifact_digest'])
            or not re.fullmatch(r'https://github\.com/50kfunded/implied-odds/actions/runs/[0-9]+', receipt['run_url'])):
        raise ValueError('GitHub witness is late, incomplete or bound to another forecast')


def validate_current_forecast(root: Path, directory: Path, origin: int, plan: dict, now: int) -> dict:
    record = read_immutable(directory/'forecasts'/f'{origin}.json')
    receipt = read_immutable(directory/'receipts'/f'{origin}.json')
    future = plan['future']
    cutoff = origin-future['feature_cutoff_before_origin_ms']
    forecasts = record['forecasts']
    if (record['origin_ms'] != origin or record['plan_hash'] != digest(plan)
            or not future['origin_start_ms'] <= origin <= future['origin_end_ms']
            or (origin-future['origin_start_ms']) % DAY_MS
            or record['feature_cutoff_ms'] != cutoff
            or record['target_end_ms'] != origin+plan['horizon_hours']*HOUR_MS
            or not origin-future['freeze_start_before_origin_ms'] <= record['frozen_at_ms']
                   <= receipt['durable_at_ms'] <= min(now, origin-future['freeze_end_before_origin_ms'])
            or receipt['origin_ms'] != origin or receipt['status'] != 'accepted'
            or receipt['forecast_hash'] != record['content_hash']
            or record['training']['latest_target_end_ms'] > cutoff
            or record['training']['n'] != plan['training_completed_origins']
            or record['training']['same_rows_for_both_models'] is not True
            or set(forecasts) != {'trailing', 'dvol', 'har', 'har_dvol'}
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in forecasts.values())):
        raise ValueError('Current forecast failed the pre-publication validation')
    seed = json.loads((root/'recordings/historical-variance/manifest.json').read_text(encoding='utf-8'))
    verify_source_proof(record, directory, digest(seed))
    return record


def quarantine_unreferenced_corruption(directory: Path) -> list[str]:
    referenced = {name for path in (directory/'forecasts').glob('*.json')
                  for name in read_immutable(path)['source_proof']['raw_files']}
    moved = []
    for path in (directory/'captures').rglob('*.jsonl.gz'):
        issues: list = []
        try:
            for _ in read_raw(path, on_issue=issues.append):
                pass
        except OSError:
            issues.append({'reason': 'corrupt_gzip'})
        if issues:
            relative = path.relative_to(directory).as_posix()
            if relative in referenced:
                raise ValueError('An accepted forecast references corrupt raw evidence')
            destination = directory/'failed-captures'/path.relative_to(directory/'captures')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError('Refusing to replace quarantined evidence')
            path.rename(destination)
            moved.append(relative)
    return moved


def preflight(directory: Path, *, client_factory=PublicClient, now_ms=utc_ms, sleep=time.sleep) -> dict:
    now = now_ms()
    cutoff = now//DAY_MS*DAY_MS
    writer = RawWriter(Path(directory)/'raw')
    client = client_factory(on_attempt=lambda row: writer.append(row.to_dict()))
    try:
        daily = client.request(DVOL, {'currency': 'BTC', 'resolution': '1D',
                               'start_timestamp': cutoff-DAY_MS, 'end_timestamp': cutoff-1}).result
        sleep(10.)
        hourly = client.request(DVOL, {'currency': 'BTC', 'resolution': '3600',
                                'start_timestamp': cutoff-HOUR_MS, 'end_timestamp': cutoff-1}).result
        sleep(10.)
        prices = client.request(PRICES, {'instrument_name': 'BTC-PERPETUAL', 'resolution': '60',
                                'start_timestamp': cutoff-2*HOUR_MS, 'end_timestamp': cutoff-1}).result
    finally:
        client.close()
    a = [row[4] for row in daily['data'] if row[0] == cutoff-DAY_MS]
    b = [row[4] for row in hourly['data'] if row[0] == cutoff-HOUR_MS]
    if len(a) != 1 or len(b) != 1 or a[0] != b[0] or prices['status'] != 'ok' or not prices['ticks']:
        raise ForecastUnavailable('Hosted-runner source check did not produce matching completed closes')
    return {'status': 'passed', 'checked_at_ms': now_ms(), 'feature_cutoff_ms': cutoff,
            'matching_dvol_close': a[0], 'price_candles': len(prices['ticks']),
            'scope': 'This bounded API check does not establish future scheduler delivery or forecast coverage.'}


def freeze_once(root: Path, directory: Path, *, now_ms=utc_ms, sleep=time.sleep) -> dict:
    from .cli import provenance
    details = provenance()
    if details['working_tree_dirty'] is not False or details['git_commit'] is None:
        raise ValueError('Expected committed code in a clean checkout')
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    now = now_ms()
    origin = now//DAY_MS*DAY_MS+HOUR_MS
    deadline = origin-plan['future']['freeze_end_before_origin_ms']
    start = origin-plan['future']['freeze_start_before_origin_ms']
    if not start <= now <= deadline-120_000:
        raise ForecastUnavailable('No current freeze window with a two-minute publication margin; backfill is prohibited')
    with FileLease(directory/'.variance-collector.lock'):
        quarantine_unreferenced_corruption(directory)
        path = directory/'forecasts'/f'{origin}.json'
        if path.exists():
            record = validate_current_forecast(root, directory, origin, plan, now_ms())
            return {'status': 'already_frozen', 'origin_ms': origin, 'forecast_hash': record['content_hash']}
        job = VarianceForwardJob(root, directory, now_ms=now_ms, sleep=sleep)
        job.state['git_commit'] = details['git_commit']
        job.cycle(include_summary=False, score_outcomes=False)
        record = validate_current_forecast(root, directory, origin, plan, now_ms())
        if now_ms() > deadline-60_000:
            raise ForecastUnavailable('Freeze finished without an accepted record or sufficient publication margin')
        return {'status': 'frozen', 'origin_ms': origin, 'forecast_hash': record['content_hash']}


def retain_server_receipt(directory: Path, metadata_path: Path, run_id: int, attempt: int, now_ms=utc_ms) -> dict:
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if (metadata.get('name') != f'variance-forecast-{run_id}-{attempt}'
            or metadata.get('workflow_run', {}).get('id') != run_id):
        raise ValueError('Expected the artifact created by this forecast workflow run')
    origin = now_ms()//DAY_MS*DAY_MS+HOUR_MS
    record = read_immutable(directory/'forecasts'/f'{origin}.json')
    receipt = {'origin_ms': origin, 'forecast_hash': record['content_hash'],
               'artifact_id': metadata['id'], 'artifact_digest': metadata['digest'],
               'created_at': metadata['created_at'],
               'timestamp_precision_ms': 1000,
               'run_url': f'https://github.com/50kfunded/implied-odds/actions/runs/{run_id}'}
    root = Path(__file__).resolve().parents[2]
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    if now_ms() > origin-plan['future']['freeze_end_before_origin_ms']:
        raise ForecastUnavailable('The server witness was not retained before the freeze deadline')
    verify_publication(record, receipt, plan)
    return write_immutable(directory/'publications'/f'{run_id}-{attempt}.json', receipt)


def current_bundle(root: Path, directory: Path, target: Path, *, now_ms=utc_ms) -> dict:
    now = now_ms()
    origin = now//DAY_MS*DAY_MS+HOUR_MS
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    record = validate_current_forecast(root, directory, origin, plan, now)
    for name in ('forecasts', 'receipts'):
        destination = target/name/f'{origin}.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory/name/f'{origin}.json', destination)
    manifest = {'origin_ms': origin, 'forecast_hash': record['content_hash'],
                'raw_source_sha256': record['source_proof']['raw_files'],
                'bundle_files_sha256': bundle_manifest(target)}
    (target/'bundle-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    return manifest


def score_existing(root: Path, directory: Path) -> dict:
    job = VarianceForwardJob(root, directory)
    job.score_existing()
    return {'status': 'scored_completed_available_targets', 'backfill': False}


def refresh_and_score(root: Path, directory: Path, *, now_ms=utc_ms, job_factory=VarianceForwardJob) -> dict:
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    if now_ms() > plan['future']['analysis_at_ms']:
        return {'status': 'terminal_cutoff_passed', 'backfill': False}
    with FileLease(directory/'.variance-collector.lock'):
        quarantine_unreferenced_corruption(directory)
        job = job_factory(root, directory, now_ms=now_ms)
        job.cycle(include_summary=False, freeze_forecasts=False)
    return {'status': 'refreshed_and_scored', 'backfill': False}


def retain_failure(directory: Path, run_id: int, attempt: int, *, now_ms=utc_ms) -> dict:
    return write_immutable(directory/'failures'/f'{run_id}-{attempt}.json', {
        'run_id': run_id, 'attempt': attempt, 'observed_at_ms': now_ms(),
        'status': 'workflow_failed', 'timely_public_witness': False,
        'scope': 'This failure record does not accept a forecast; raw attempt responses remain separate.'})


def export_failure(directory: Path, target: Path) -> dict:
    if directory.is_symlink() or target.is_symlink():
        raise ValueError('Public failure roots cannot be symlinks')
    directory, target = directory.resolve(), target.resolve()
    files = []
    referenced = set()
    for path in (directory/'forecasts').glob('*.json'):
        try:
            referenced.update(read_immutable(path)['source_proof']['raw_files'])
        except (ValueError, KeyError):
            continue
    for name in ('captures', 'failed-captures', 'failures'):
        # Retain only whitelisted failed-attempt evidence, never a failed forecast.
        for path in (directory/name).rglob('*'):
            relative = path.relative_to(directory).as_posix()
            if path.is_file() and allowed_public_path(relative):
                if path.is_symlink():
                    raise ValueError('Public failure evidence cannot contain symlinks')
                public_relative = ('failed-'+relative if name == 'captures' and relative not in referenced else relative)
                files.append((public_relative, path))
    return _transfer_files(directory, target, files, 500_000_000)


def observation(root: Path, directory: Path, run_id: int, attempt: int, now_ms=utc_ms) -> dict:
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    guard = load_plan(root/'protocol/inference-validation-v1.json')
    report = forward_summary(directory, plan, now_ms(), guard)
    report['publications'] = [read_immutable(path) for path in sorted((directory/'publications').glob('*.json'))]
    report['anchor_scope'] = 'GitHub server artifact metadata is retained separately; pending OpenTimestamps proofs are not confirmed Bitcoin anchors.'
    return write_immutable(directory/'observations'/f'{run_id}-{attempt}.json', report)


def bundle_manifest(directory: Path) -> dict:
    return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()
            and allowed_public_path(path.relative_to(directory).as_posix())}


def validate_staged_changes(changes: bytes) -> list[str]:
    fields = changes.decode('utf-8').split('\0')
    paths = []
    for i in range(0, len(fields)-1, 2):
        if i+1 >= len(fields)-1 or fields[i] != 'A' or not allowed_public_path(fields[i+1]):
            raise ValueError('Only newly added public evidence paths may be committed')
        paths.append(fields[i+1])
    return paths


def check_staged_state(directory: Path) -> dict:
    changes = subprocess.check_output(['git', 'diff', '--cached', '--name-status', '-z'], cwd=directory)
    return {'new_public_files': validate_staged_changes(changes)}


def check_liveness(directory: Path, *, now_ms=utc_ms, maximum_age_hours: int = 36,
                   require_latest_forecast: bool = False) -> dict:
    records = [read_immutable(path) for path in (directory/'observations').glob('*.json')]
    if not records:
        raise ValueError('No public collection observation is available')
    latest = max(record['as_of_ms'] for record in records)
    age = (now_ms()-latest)/HOUR_MS
    if not 0 <= age <= maximum_age_hours:
        raise ValueError('Public collection observation is stale or from the future')
    if require_latest_forecast:
        root = Path(__file__).resolve().parents[2]
        plan = load_plan(root/'protocol/incremental-variance-v1.json')
        future = plan['future']
        due = [origin for origin in range(future['origin_start_ms'], future['origin_end_ms']+DAY_MS, DAY_MS)
               if origin-future['freeze_end_before_origin_ms'] < now_ms()]
        if due:
            origin = due[-1]
            record = read_immutable(directory/'forecasts'/f'{origin}.json')
            candidates = [read_immutable(path) for path in (directory/'publications').glob('*.json')]
            witnesses = [receipt for receipt in candidates if receipt['origin_ms'] == origin]
            if not witnesses:
                raise ValueError('Latest due origin has no retained public witness')
            for receipt in witnesses:
                verify_publication(record, receipt, plan)
    return {'status': 'recent_public_observation', 'age_hours': age, 'maximum_age_hours': maximum_age_hours}
