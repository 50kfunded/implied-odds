"""A local job collects prospective inputs, freezes due forecasts and scores deliveries."""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .baseline_evaluation import score_baselines
from .baseline_lock import LockedBaselines, load_locked_baselines
from .client import ClientError, PublicClient
from .evaluation import score_database
from .history import HOUR_MS, fetch_history, ingest_history, trailing_returns
from .ingest import ingest, validate_heartbeat
from .locking import LockedProtocol, load_locked_protocol
from .operational_status import build_scoreboard, write_scoreboard
from .predictions import freeze_predictions
from .raw import RawWriter, day_name
from .recorder import SystemClock
from .report import build_report, write_report
from .schedule import Anchor, boundary_ms
from .settlement import RecoverableDeliveryCaptureError, fetch_delivery_pages, ingest_deliveries
from .store import Store, canonical, digest, evaluation_store


LOGGER = logging.getLogger(__name__)


def _render_report(config: 'CollectionConfig', parent: LockedProtocol, extension: LockedBaselines,
                   as_of_ms: int, git_commit: str) -> dict:
    data = build_report(config.db, parent, extension, as_of_ms, git_commit,
                        clean_cache_path=config.report_output / 'clean-cache.sqlite')
    return write_report(data, config.report_output)


class FileLease:
    """Let the OS release a crashed job's lease without deleting its lock file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if self.file.seek(0, 2) == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise OSError('Collection is locked by another process') from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


def write_status(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n',
                         encoding='utf-8', newline='\n')
    temporary.replace(path)


@dataclass(frozen=True)
class CollectionConfig:
    output: Path
    db: Path
    report_output: Path
    protocol: Path = Path('protocol/protocol.json')
    baselines: Path = Path('protocol/baselines.json')
    interval: int = 300
    seconds: int | None = None

    def validate(self) -> None:
        if type(self.interval) is not int or self.interval < 10:
            raise ValueError('Collection interval must be an integer >= 10 seconds')
        if self.seconds is not None and (type(self.seconds) is not int or self.seconds <= 0):
            raise ValueError('Collection duration must be a positive integer')
        if self.report_output.resolve().is_relative_to(self.output.resolve()):
            raise ValueError('Report output must stay outside the retained collection folder')


class CollectionJob:
    def __init__(self, config: CollectionConfig, parent: LockedProtocol, extension: LockedBaselines,
                 git_commit: str, *, clock=None, client_factory=PublicClient) -> None:
        config.validate()
        self.config, self.parent, self.extension, self.git_commit = config, parent, extension, git_commit
        self.clock = clock if clock is not None else SystemClock()
        self.client_factory = client_factory
        self.session = uuid4().hex
        self.history_hour: int | None = None
        self.metadata_hour: int | None = None
        self.delivery_hour: int | None = None
        self.attempts = self.failed_attempts = self.skipped_slots = self.clock_changes = 0
        self.settlement_capture_attempts = self.settlement_capture_failures = 0
        self.report_executor = None
        self.report_future = None
        self.report_requested_ms = None
        self.report_state: dict[str, Any] = {'status': 'not_requested'}

    def poll_report(self) -> dict:
        if self.report_future is not None and self.report_future.done():
            try:
                self.report_state = {'status': 'completed', 'result': self.report_future.result()}
            except Exception as exc:
                self.report_state = {'status': 'failed', 'error_type': type(exc).__name__}
                LOGGER.warning('Local report rendering failed: %s', exc)
            self.report_future = None
        if self.report_future is None and self.report_requested_ms is not None:
            if self.report_executor is None:
                self.report_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-report')
            cutoff = self.report_requested_ms
            self.report_requested_ms = None
            self.report_future = self.report_executor.submit(_render_report, self.config, self.parent,
                                                            self.extension, cutoff, self.git_commit)
            self.report_state = {'status': 'rendering', 'as_of_ms': cutoff}
        return dict(self.report_state)

    def close_reports(self) -> dict:
        pending = self.report_requested_ms
        self.report_requested_ms = None
        if self.report_executor is not None:
            self.report_executor.shutdown(wait=True)
            self.report_executor = None
        self.poll_report()
        if pending is not None:
            try:
                self.report_state = {'status': 'completed', 'result': _render_report(
                    self.config, self.parent, self.extension, pending, self.git_commit)}
            except Exception as exc:
                self.report_state = {'status': 'failed', 'error_type': type(exc).__name__}
                LOGGER.warning('Final local report rendering failed: %s', exc)
        return dict(self.report_state)

    def event(self, event: str, **fields: Any) -> None:
        record = {'event': event, 'fetched_at_ms': self.clock.now_ms(), 'run_id': self.session,
                  'ok': fields.pop('ok', event != 'gap'), 'reason': fields.pop('reason', event), **fields}
        if event == 'gap':
            self.skipped_slots += record['count']
            record['last_slot_ms'] = record['first_slot_ms'] + (record['count'] - 1) * self.config.interval * 1000
        if event == 'clock_change':
            self.clock_changes += 1
        validate_heartbeat(record)
        path = self.config.output / 'heartbeat' / (day_name(record['fetched_at_ms']) + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as target:
            target.write(canonical(record) + '\n')
        store = Store(self.config.db)
        try:
            with store.connection:
                store.connection.execute('INSERT OR IGNORE INTO heartbeat VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (digest(record), self.session, record['fetched_at_ms'], event, int(record['ok']),
                     record['reason'], canonical(record)))
        finally:
            store.close()

    def _client(self, output: Path):
        writer = RawWriter(output / 'raw')
        def retain(row):
            self.attempts += 1
            self.failed_attempts += int(not row.ok)
            writer.append(row.to_dict())
        return self.client_factory(on_attempt=retain,
                                   now_ms=self.clock.now_ms, monotonic=self.clock.monotonic,
                                   sleep=self.clock.sleep)

    def _history(self, segment: Path) -> dict:
        end = self.clock.now_ms() // HOUR_MS * HOUR_MS
        hours = 2162 if self.history_hour is None else min(2162, max(3, end // HOUR_MS - self.history_hour + 2))
        output = segment / 'history'
        client = self._client(output)
        try:
            captured = fetch_history(client, max(0, end - hours * HOUR_MS), end, sleep=self.clock.sleep)
        finally:
            client.close()
        result = ingest_history(output, self.config.db, self.clock.now_ms())
        store = evaluation_store(self.config.db)
        try:
            readiness = trailing_returns(store.connection, self.clock.now_ms(), 2160, 2 * HOUR_MS)
        finally:
            store.close()
        if readiness['status'] == 'ok':
            self.history_hour = end // HOUR_MS
        return {'capture': captured, 'ingestion': result, 'readiness_status': readiness['status']}

    def cycle(self, scheduled_ms: int) -> dict:
        from .cli import provenance

        segment = self.config.output / 'sessions' / self.session / str(scheduled_ms)
        segment.mkdir(parents=True, exist_ok=False)
        started = self.clock.now_ms()
        result: dict[str, Any] = {'scheduled_ms': scheduled_ms, 'failures': []}
        hour = self.clock.now_ms() // HOUR_MS
        if self.history_hour != hour:
            try:
                result['history'] = self._history(segment)
                if result['history']['readiness_status'] != 'ok':
                    result['failures'].append('history:' + result['history']['readiness_status'])
            except ClientError as exc:
                result['failures'].append('history:' + exc.reason)
                LOGGER.warning('History capture failed: %s', exc)
        output = segment / 'options'
        client = self._client(output)
        try:
            endpoints = []
            if self.metadata_hour != hour:
                endpoints.append(('public/get_instruments', {'currency': 'BTC', 'kind': 'option', 'expired': 'false'}))
            endpoints.extend([('public/get_index_price', {'index_name': 'btc_usd'}),
                              ('public/get_book_summary_by_currency', {'currency': 'BTC', 'kind': 'option'})])
            for endpoint, parameters in endpoints:
                try:
                    client.request(endpoint, parameters)
                    if endpoint == 'public/get_instruments':
                        self.metadata_hour = hour
                except ClientError as exc:
                    result['failures'].append(endpoint + ':' + exc.reason)
                    LOGGER.warning('Option input failed: %s %s', endpoint, exc)
        finally:
            client.close()
        store = Store(self.config.db)
        try:
            result['ingestion'] = ingest(output, store)
        finally:
            store.close()
        details = provenance()
        if details['git_commit'] != self.git_commit or details['working_tree_dirty'] is not False:
            result['failures'].append('research_checkout_changed')
            self.event('research_skipped', reason='research_checkout_changed')
            self.event('cycle', ok=False, scheduled_ms=scheduled_ms, cycle_started_ms=started,
                       cycle_finished_ms=self.clock.now_ms(), result=result)
            return result
        now = self.clock.now_ms()
        predictions = freeze_predictions(self.config.db, self.parent, now, now, self.git_commit,
                                         now_ms=self.clock.now_ms, baseline_extension=self.extension)
        result['predictions'] = {k: v for k, v in predictions.items() if k not in ('predictions', 'baselines')}
        if self.delivery_hour != hour:
            output = segment / 'settlements'
            client = self._client(output)
            self.settlement_capture_attempts += 1
            try:
                captured = fetch_delivery_pages(client, sleep=self.clock.sleep)
                result['settlements'] = {'capture': captured,
                    'ingestion': ingest_deliveries(output, self.config.db, self.clock.now_ms())}
                self.delivery_hour = hour
            except (ClientError, RecoverableDeliveryCaptureError) as exc:
                self.settlement_capture_failures += 1
                result['failures'].append('settlements:' + exc.reason)
                result['settlements'] = {'status': 'retry_next_cycle', 'reason': exc.reason}
                LOGGER.warning('Settlement capture will retry: %s', exc)
            except (ValueError, sqlite3.Error, OSError):
                self.settlement_capture_failures += 1
                raise
            finally:
                client.close()
        now = self.clock.now_ms()
        scores = score_database(self.config.db, self.parent, now, now, self.git_commit, now_ms=self.clock.now_ms)
        now = self.clock.now_ms()
        baselines = score_baselines(self.config.db, self.extension, self.parent, now, now, self.git_commit,
                                    now_ms=self.clock.now_ms)
        result['scores'] = {k: v for k, v in scores.items() if k != 'scores'}
        result['baseline_scores'] = {k: v for k, v in baselines.items() if k != 'scores'}
        if scores['added'] or baselines['added']:
            self.report_requested_ms = self.clock.now_ms()
        result['report'] = self.poll_report()
        self.event('cycle', ok=not result['failures'], scheduled_ms=scheduled_ms,
                   cycle_started_ms=started, cycle_finished_ms=self.clock.now_ms(), result=result)
        return result


def request_stop(output: Path) -> dict:
    path = Path(output) / 'STOP'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Graceful stop requested.\n', encoding='utf-8', newline='\n')
    return {'status': 'stop_requested', 'output': str(output)}


def collect(config: CollectionConfig, *, clock=None, client_factory=PublicClient) -> dict:
    from .cli import provenance

    config.validate()
    details = provenance()
    if details['git_commit'] is None or details['working_tree_dirty'] is not False:
        raise ValueError('Expected a clean committed checkout before starting collection')
    parent = load_locked_protocol(config.protocol)
    extension = load_locked_baselines(config.baselines, parent)
    job = CollectionJob(config, parent, extension, details['git_commit'], clock=clock, client_factory=client_factory)
    config.output.mkdir(parents=True, exist_ok=True)
    status_path = config.output / 'status.json'
    previous = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
    with ExitStack() as stack:
        stack.enter_context(FileLease(config.output / '.collector.lock'))
        stack.enter_context(FileLease(config.db.with_name(config.db.name + '.collector.lock')))
        (config.output / 'STOP').unlink(missing_ok=True)
        state = {'status': 'starting', 'pid': os.getpid(), 'session': job.session,
                 'db': str(config.db.resolve()), 'git_commit': job.git_commit,
                 'interval_seconds': config.interval, 'seconds': config.seconds,
                 'started_at_ms': job.clock.now_ms(), 'cycles': 0, 'completed_cycles': 0, 'failed_cycles': 0,
                 'attempts': 0, 'failed_attempts': 0, 'skipped_slots': 0, 'clock_changes': 0,
                 'last_scheduled_ms': previous.get('last_scheduled_ms')}
        write_status(status_path, state)
        stop = math.inf if config.seconds is None else job.clock.monotonic() + config.seconds
        interval_ms = config.interval * 1000
        anchor = Anchor(job.clock.now_ms(), job.clock.monotonic())
        slot = boundary_ms(job.clock.now_ms(), interval_ms, inclusive=True)
        last = state['last_scheduled_ms']
        if type(last) is int and slot <= last:
            job.event('clock_change', ok=False, reason='restart_before_last_slot', last_scheduled_ms=last)
            slot = boundary_ms(last, interval_ms)
        job.event('session_start', first_slot_ms=slot, interval_ms=interval_ms,
                   seconds=config.seconds, anchor_wall_ms=state['started_at_ms'])
        if type(last) is int and slot - last > interval_ms:
            job.event('gap', reason='restart_or_downtime', first_slot_ms=last + interval_ms,
                      count=(slot - last) // interval_ms - 1)
        try:
            cycle_in_progress = False
            while job.clock.monotonic() < stop and not (config.output / 'STOP').exists():
                now = job.clock.now_ms()
                drift = anchor.drift_ms(now, job.clock.monotonic())
                if abs(drift) > 1000:
                    job.event('clock_change', ok=False, reason='wall_monotonic_offset_changed', drift_ms=drift)
                    anchor = Anchor(now, job.clock.monotonic())
                if now < slot:
                    job.clock.sleep(min(1., (slot - now) / 1000, stop - job.clock.monotonic()))
                    continue
                skipped = (now - slot) // interval_ms
                if skipped:
                    job.event('gap', reason='sleep_or_delay', first_slot_ms=slot, count=skipped)
                    slot += skipped * interval_ms
                state['status'] = 'collecting'
                state['updated_at_ms'] = now
                state['cycles'] += 1
                state['last_scheduled_ms'] = slot
                cycle_in_progress = True
                write_status(status_path, state)
                result = job.cycle(slot)
                cycle_in_progress = False
                state.update(completed_cycles=state['completed_cycles'] + 1,
                             failed_cycles=state['failed_cycles'] + int(bool(result['failures'])),
                             attempts=job.attempts, failed_attempts=job.failed_attempts,
                             skipped_slots=job.skipped_slots, clock_changes=job.clock_changes,
                             last_cycle=result, updated_at_ms=job.clock.now_ms(), status='waiting')
                state.update(settlement_capture_attempts=job.settlement_capture_attempts,
                             settlement_capture_failures=job.settlement_capture_failures)
                write_status(status_path, state)
                write_scoreboard(build_scoreboard(config.db, parent.protocol.content_hash, extension.content_hash,
                                                  state, job.clock.now_ms()), config.report_output)
                slot += interval_ms
                finished = job.clock.now_ms()
                if finished >= slot:
                    skipped = (finished - slot) // interval_ms + 1
                    job.event('gap', reason='cycle_overrun', first_slot_ms=slot, count=skipped)
                    slot += skipped * interval_ms
            state['status'] = 'stopped' if (config.output / 'STOP').exists() else 'completed'
            return state
        except KeyboardInterrupt:
            state['status'] = 'interrupted'
            raise
        except (OSError, ValueError, sqlite3.Error, ClientError) as exc:
            if cycle_in_progress:
                state['failed_cycles'] += 1
            state.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            state['report'] = job.close_reports()
            state['updated_at_ms'] = job.clock.now_ms()
            state.update(attempts=job.attempts, failed_attempts=job.failed_attempts,
                         skipped_slots=job.skipped_slots, clock_changes=job.clock_changes,
                         settlement_capture_attempts=job.settlement_capture_attempts,
                         settlement_capture_failures=job.settlement_capture_failures)
            write_status(status_path, state)
            write_scoreboard(build_scoreboard(config.db, parent.protocol.content_hash, extension.content_hash,
                                              state, job.clock.now_ms()), config.report_output)
            job.event('session_end', ok=state['status'] in ('completed', 'stopped') and state['failed_cycles'] == 0
                      and job.failed_attempts == 0 and job.skipped_slots == 0 and job.clock_changes == 0,
                      status=state['status'])
