"""Official BTC delivery pages are retained and ingested as settlement observations."""

from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable

from .client import ClientError, PublicClient, validate_response
from .raw import RawWriter, read_raw
from .store import canonical, digest, evaluation_store


class RecoverableDeliveryCaptureError(ValueError):
    """Retry an inconsistent page sequence in a fresh capture."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def delivery_expiry_ms(value: str) -> int:
    day = date.fromisoformat(value)
    return int(datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_delivery_pages(client: PublicClient, *, count: int = 1000, max_pages: int = 1000,
                         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    if type(count) is not int or not 1 <= count <= 1000 or type(max_pages) is not int or max_pages < 1:
        raise ValueError('Expected count 1..1000 and a positive page limit')
    offset, expected_total, seen, pages = 0, None, set(), 0
    while pages < max_pages:
        response = client.request('public/get_delivery_prices', {'index_name': 'btc_usd', 'offset': offset, 'count': count})
        result = response.result
        total, rows = result['records_total'], result['data']
        if expected_total is not None and total != expected_total:
            raise RecoverableDeliveryCaptureError('pagination_total_changed',
                'Raw pages are retained; the changed total requires a fresh capture')
        expected_total = total
        if len(rows) > count or offset + len(rows) > total or not rows and offset < total:
            raise RecoverableDeliveryCaptureError('pagination_no_progress',
                'Delivery pagination retry required after no progress or an exceeded count')
        dates = [row['date'] for row in rows]
        if len(set(dates)) != len(dates) or seen.intersection(dates):
            raise RecoverableDeliveryCaptureError('pagination_overlap',
                'Delivery pagination retry required after repeated or overlapping dates')
        seen.update(dates)
        offset += len(rows)
        pages += 1
        if offset >= total:
            return {'status': 'ok', 'pages': pages, 'records': offset, 'records_total': total}
        sleep(10.0)
    raise RecoverableDeliveryCaptureError('pagination_page_limit',
        'Delivery pagination retry required after reaching the page limit')


def capture_deliveries(output: Path) -> dict[str, Any]:
    writer = RawWriter(Path(output) / 'raw')
    client = PublicClient(on_attempt=lambda attempt: writer.append(attempt.to_dict()))
    try:
        return fetch_delivery_pages(client)
    finally:
        client.close()


def ingest_deliveries(directory: Path, db: Path, as_of_ms: int) -> dict[str, Any]:
    if type(as_of_ms) is not int or as_of_ms < 0:
        raise ValueError('Expected integer UTC milliseconds for settlement ingestion')
    paths = sorted(Path(directory).rglob('*.jsonl.gz'))
    if not paths:
        raise ValueError('Expected retained gzip JSON-lines delivery pages')
    store = evaluation_store(db)
    reasons: Counter[str] = Counter()
    pages = added = unchanged = 0
    try:
        with store.connection:
            for path in paths:
                for row in read_raw(path, on_issue=lambda issue: reasons.update([issue['reason']])):
                    if row.get('endpoint') != 'public/get_delivery_prices':
                        reasons['other_endpoint'] += 1
                        continue
                    params = row.get('params')
                    stamp = row.get('fetched_at_ms')
                    if (type(stamp) is not int or stamp < 0 or not isinstance(params, dict)
                            or set(params) != {'index_name', 'offset', 'count'} or params['index_name'] != 'btc_usd'
                            or type(params['offset']) is not int or params['offset'] < 0
                            or type(params['count']) is not int or not 1 <= params['count'] <= 1000):
                        raise ValueError('Invalid retained delivery timing or pagination parameters')
                    if stamp > as_of_ms:
                        reasons['future_page'] += 1
                        continue
                    if not row.get('ok'):
                        reasons['failed_attempt'] += 1
                        continue
                    try:
                        result = validate_response(row['endpoint'], row.get('http_status'), row.get('body'))
                    except ClientError as exc:
                        raise ValueError(f'Retained delivery schema failed: {exc}') from exc
                    if len(result['data']) > params['count'] or params['offset'] + len(result['data']) > result['records_total']:
                        raise ValueError('Delivery page exceeds requested count')
                    source_id = digest(row)
                    if store.connection.execute('SELECT 1 FROM delivery_sources WHERE source_id=?', (source_id,)).fetchone() is None:
                        store.connection.execute('INSERT INTO delivery_sources VALUES (?, ?, ?, ?)',
                                                 (source_id, stamp, canonical(params), digest(row['body'])))
                    pages += 1
                    for entry in result['data']:
                        expiry = delivery_expiry_ms(entry['date'])
                        if expiry > stamp:
                            raise ValueError('Official delivery observation predates its 08:00 UTC expiry')
                        old = store.connection.execute('SELECT * FROM delivery_prices WHERE expiry_ms=?', (expiry,)).fetchone()
                        if old is not None:
                            if old['delivery_price'] != entry['delivery_price']:
                                raise ValueError('Conflicting official delivery price; the first observation remains unchanged')
                            unchanged += 1
                            continue
                        store.connection.execute('INSERT INTO delivery_prices VALUES (?, ?, ?)',
                                                 (expiry, entry['delivery_price'], stamp))
                        store.connection.execute('INSERT INTO delivery_observations VALUES (?, ?, ?)',
                                                 (expiry, source_id, digest(entry)))
                        added += 1
    finally:
        store.close()
    return {'status': 'ok', 'as_of_ms': as_of_ms, 'pages': pages, 'added': added, 'unchanged': unchanged,
            'reason_counts': dict(sorted(reasons.items()))}
