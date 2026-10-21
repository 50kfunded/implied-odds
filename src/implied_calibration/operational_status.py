"""Local operational reports include forecast counts, frozen hashes and freshness."""

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from .predictions import checked_prediction
from .store import canonical, digest, evaluation_store


FROZEN_TABLES = {'predictions': 'prediction_id', 'baseline_predictions': 'baseline_id',
                 'score_records': 'prediction_id', 'baseline_scores': 'baseline_id'}
GENESIS_HASH = '0' * 64


def liveness_check(state: dict, now_ms: int) -> dict:
    stamp = state.get('updated_at_ms', state.get('started_at_ms'))
    interval = state.get('interval_seconds', 300)
    threshold = max(60_000, interval * 2_000)
    age = None if type(stamp) is not int else now_ms - stamp
    if state.get('status') not in ('starting', 'collecting', 'waiting'):
        status = 'inactive'
    elif age is None:
        status = 'unknown'
    elif age < -1_000:
        status = 'clock_moved_backward'
    elif age > threshold:
        status = 'stale'
    else:
        status = 'fresh'
    return {'status': status, 'checked_at_ms': now_ms, 'last_update_ms': stamp,
            'age_ms': age, 'stale_after_ms': threshold,
            'interpretation': 'Heartbeat age measures freshness; a PID does not establish process health.'}


def _frozen_records(connection) -> dict[tuple[str, str], dict]:
    records = {}
    for table, key in FROZEN_TABLES.items():
        for row in connection.execute(f'SELECT {key},payload_json FROM {table} ORDER BY {key}'):
            record = checked_prediction(row[1])
            if record[key] != row[0]:
                raise ValueError('Detected a frozen record identity mismatch')
            records[(table, row[0])] = record
    return records


def _sync_ledger(connection, records: dict) -> tuple[list[dict], dict]:
    entries, recorded = [], set()
    previous = GENESIS_HASH
    for row in connection.execute('SELECT * FROM frozen_hash_ledger ORDER BY sequence'):
        entry = dict(row)
        claimed = entry.pop('entry_hash')
        identity = (entry['source_table'], entry['record_id'])
        record = records.get(identity)
        if (entry['sequence'] != len(entries) + 1 or entry['previous_hash'] != previous
                or digest(entry) != claimed or identity in recorded or record is None
                or record['content_hash'] != entry['payload_hash']
                or record['created_at_ms'] != entry['created_at_ms']):
            raise ValueError('Detected a frozen hash ledger mismatch')
        entry['entry_hash'] = claimed
        entries.append(entry)
        recorded.add(identity)
        previous = claimed
    pending = sorted((identity for identity in records if identity not in recorded),
                     key=lambda identity: (records[identity]['created_at_ms'], *identity))
    for identity in pending:
        record = records[identity]
        entry = {'sequence': len(entries) + 1, 'source_table': identity[0], 'record_id': identity[1],
                 'payload_hash': record['content_hash'], 'created_at_ms': record['created_at_ms'],
                 'previous_hash': previous}
        entry['entry_hash'] = digest(entry)
        connection.execute('INSERT INTO frozen_hash_ledger VALUES (?,?,?,?,?,?,?)',
                           tuple(entry[key] for key in ('sequence', 'source_table', 'record_id', 'payload_hash',
                                                       'created_at_ms', 'previous_hash', 'entry_hash')))
        entries.append(entry)
        previous = entry['entry_hash']
    return entries, {'schema_version': 1, 'entries': len(entries), 'genesis_hash': GENESIS_HASH,
                     'head_hash': previous}


def build_scoreboard(db: Path, protocol_hash: str, extension_hash: str, state: dict, now_ms: int) -> dict:
    store = evaluation_store(db)
    try:
        with store.connection:
            records = _frozen_records(store.connection)
            entries, anchor = _sync_ledger(store.connection, records)
        selected = {table: [record for (source, _), record in records.items()
                           if source == table and record['created_at_ms'] <= now_ms
                           and record.get('protocol_hash', protocol_hash) == protocol_hash
                           and record.get('extension_hash', extension_hash) == extension_hash]
                    for table in FROZEN_TABLES}
        forecasts = selected['predictions']
        baselines = selected['baseline_predictions']
        forecast_status = Counter(record['status'] for record in forecasts)
        baseline_status = Counter(record['status'] for record in baselines)
        reasons = Counter({key: value for key, value in forecast_status.items() if key != 'ok'})
        baseline_reasons = Counter({key: value for key, value in baseline_status.items() if key != 'ok'})
        return {'schema_version': 1, 'as_of_ms': now_ms, 'protocol_hash': protocol_hash,
                'extension_hash': extension_hash,
                'forecasts': {'frozen': len(forecasts), 'accepted': forecast_status.get('ok', 0),
                              'scored': len(selected['score_records']), 'status_counts': dict(sorted(forecast_status.items())),
                              'rejection_reason_counts': dict(sorted(reasons.items()))},
                'baselines': {'frozen': len(baselines), 'accepted': baseline_status.get('ok', 0),
                              'scored': len(selected['baseline_scores']), 'status_counts': dict(sorted(baseline_status.items())),
                              'rejection_reason_counts': dict(sorted(baseline_reasons.items()))},
                'collection': {key: state[key] for key in ('status', 'cycles', 'completed_cycles', 'failed_cycles',
                    'attempts', 'failed_attempts', 'skipped_slots', 'settlement_capture_attempts',
                    'settlement_capture_failures') if key in state},
                'last_cycle_failures': state.get('last_cycle', {}).get('failures', []),
                'liveness': liveness_check(state, now_ms), 'frozen_anchor': anchor, 'frozen_ledger': entries,
                'anchor_scope': 'The local ledger anchors all retained frozen forecasts and scores.',
                'anchor_limit': 'This local anchor has no independent timestamp or external witness.'}
    finally:
        store.close()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n',
                         encoding='utf-8', newline='\n')
    temporary.replace(path)


def _check_anchor_prefix(anchor: dict, ledger: list[dict]) -> None:
    count = anchor.get('entries')
    if type(count) is not int or not 0 <= count <= len(ledger):
        raise ValueError('Detected a frozen anchor rollback or changed prefix')
    expected = GENESIS_HASH if count == 0 else ledger[count - 1]['entry_hash']
    if (anchor.get('head_hash') != expected or anchor.get('genesis_hash') != GENESIS_HASH
            or anchor.get('schema_version') != 1):
        raise ValueError('Detected a frozen anchor rollback or changed prefix')


def write_scoreboard(data: dict, output: Path) -> None:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    anchor = data['frozen_anchor']
    previous_path = output / 'frozen-anchor.json'
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        _check_anchor_prefix(previous, data['frozen_ledger'])
    archives = output / 'frozen-anchors'
    archives.mkdir(exist_ok=True)
    existing = sorted(archives.glob('*.json'))
    if existing:
        retained_anchor = json.loads(existing[-1].read_text(encoding='utf-8'))
        _check_anchor_prefix(retained_anchor, data['frozen_ledger'])
    archive = archives / f"{anchor['entries']:012d}-{anchor['head_hash']}.json"
    retained = canonical(anchor) + '\n'
    if archive.exists():
        if archive.read_text(encoding='utf-8') != retained:
            raise ValueError('Detected a changed retained frozen anchor')
    else:
        with archive.open('x', encoding='utf-8', newline='\n') as target:
            target.write(retained)
    _write_json(output / 'scoreboard.json', data)
    _write_json(output / 'frozen-anchor.json', anchor)
    counts = data['forecasts']
    observed_at = datetime.fromtimestamp(data['as_of_ms']/1000,timezone.utc).isoformat()
    rows = ''.join(f'<tr><td>{escape(reason)}</td><td>{count}</td></tr>'
                   for reason, count in counts['rejection_reason_counts'].items())
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>Local forecast counts</title>
<style>body{{font:16px system-ui;max-width:900px;margin:40px auto;padding:20px;background:#f4f7fa;color:#172c40}}
table{{border-collapse:collapse}}td,th{{padding:8px 20px;border-bottom:1px solid #ccd6df}}code{{overflow-wrap:anywhere}}</style>
<h1>Local forecast counts</h1><p>Frozen: {counts['frozen']}; accepted: {counts['accepted']}; scored: {counts['scored']}.</p>
<p>Published at {escape(observed_at)}. Separate HAR counts appear in <a href="variance-scoreboard.json">variance-scoreboard.json</a>.</p>
<p>Collector freshness: <strong id="freshness">{escape(data['liveness']['status'])}</strong>.</p>
<p>This age check refreshes every second while the page stays open.</p>
<table><tr><th>Forecast rejection reason</th><th>Count</th></tr>{rows}</table>
<p>Retained frozen hash entries: {anchor['entries']}.</p><p>Head hash: <code>{anchor['head_hash']}</code></p>
<p>{escape(data['anchor_limit'])}</p><p>Baseline counts, cycle failures and the complete ledger appear in scoreboard.json.</p>
<script>const stamp={json.dumps(data['liveness']['last_update_ms'])};const threshold={data['liveness']['stale_after_ms']};
const active={json.dumps(data['collection'].get('status') in ('starting','collecting','waiting'))};
setInterval(()=>{{document.getElementById('freshness').textContent=!active?'inactive':stamp===null?'unknown':Date.now()-stamp>threshold?'stale':Date.now()-stamp < -1000?'clock_moved_backward':'fresh';}},1000);</script></html>'''
    temporary = output / 'scoreboard.html.tmp'
    temporary.write_text(html, encoding='utf-8', newline='\n')
    temporary.replace(output / 'scoreboard.html')


def check_local_liveness(output: Path, now_ms: int) -> dict:
    state = json.loads((Path(output) / 'status.json').read_text(encoding='utf-8'))
    return liveness_check(state, now_ms)
