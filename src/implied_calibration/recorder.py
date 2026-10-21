"""Recording preserves attempts, cycle outcomes and gaps without catching up missed slots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import math
from pathlib import Path
import time
from typing import Any, Callable
from uuid import uuid4

from .client import AttemptRecord, ClientError, PublicClient, utc_ms
from .raw import RawWriter, day_name
from .schedule import Anchor, boundary_ms, missed_slots


LOGGER = logging.getLogger(__name__)


class SystemClock:
    now_ms = staticmethod(utc_ms)
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


@dataclass
class RecordingSummary:
    run_id: str
    cycles: int = 0
    failed_cycles: int = 0
    attempts: int = 0
    failed_attempts: int = 0
    skipped_slots: int = 0
    clock_changes: int = 0


class RecordingSink:
    def __init__(self, output: Path, summary: RecordingSummary, clock: SystemClock) -> None:
        self.raw = RawWriter(output / "raw")
        self.heartbeat = output / "heartbeat"
        self.heartbeat.mkdir(parents=True, exist_ok=True)
        self.summary, self.clock = summary, clock

    def event(self, event: str, *, ok: bool, reason: str, **fields: Any) -> None:
        now = self.clock.now_ms()
        record = dict(event=event, fetched_at_ms=now, run_id=self.summary.run_id,
                      ok=ok, reason=reason, **fields)
        path = self.heartbeat / f"{day_name(now)}.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
            target.flush()

    def attempt(self, record: AttemptRecord) -> None:
        self.raw.append(record.to_dict())
        self.summary.attempts += 1
        self.summary.failed_attempts += int(not record.ok)
        self.event("attempt", ok=record.ok, reason=record.reason, endpoint=record.endpoint,
                   attempt=record.attempt, response_received_ms=record.response_received_ms,
                   detail=record.detail)

    def skip(self, slot_ms: int, now_ms: int, interval_ms: int, reason: str) -> int:
        count = missed_slots(slot_ms, now_ms, interval_ms)
        if count:
            self.summary.skipped_slots += count
            self.event("gap", ok=False, reason=reason, first_slot_ms=slot_ms,
                       last_slot_ms=slot_ms + (count - 1) * interval_ms, count=count)
            LOGGER.warning("%s: skipped_slots=%s first_slot_ms=%s", reason, count, slot_ms)
        return slot_ms + count * interval_ms


def record(
    output: Path,
    interval: int = 300,
    seconds: int | None = None,
    *,
    clock: SystemClock | None = None,
    client_factory: Callable[..., PublicClient] = PublicClient,
) -> RecordingSummary:
    if type(interval) is not int or interval < 10:
        raise ValueError("--interval must be an integer >= 10 seconds")
    if seconds is not None and (type(seconds) is not int or seconds <= 0):
        raise ValueError("--seconds must be a positive integer")
    clock = clock if clock is not None else SystemClock()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".record.lock"
    try:
        with lock.open("x", encoding="utf-8", newline="\n") as target:
            target.write("Recording session active.\n")
    except FileExistsError as exc:
        raise OSError("Output is locked; use a fresh --output directory after a crash") from exc
    client = None
    sink = None
    summary = RecordingSummary(uuid4().hex)
    reason = "error"
    try:
        sink = RecordingSink(output, summary, clock)
        wall, mono = clock.now_ms(), clock.monotonic()
        anchor = Anchor(wall, mono)
        stop = math.inf if seconds is None else mono + seconds
        interval_ms = interval * 1000
        slot = boundary_ms(wall, interval_ms, inclusive=True)
        sink.event("session_start", ok=True, reason="started", interval_ms=interval_ms,
                   seconds=seconds, first_slot_ms=slot, anchor_wall_ms=wall)
        sink.raw.preflight(wall)
        client = client_factory(on_attempt=sink.attempt, now_ms=clock.now_ms,
                                monotonic=clock.monotonic, sleep=clock.sleep)
        instrument_hour = None
        while True:
            now, current = clock.now_ms(), clock.monotonic()
            drift = anchor.drift_ms(now, current)
            if abs(drift) > 1000:
                summary.clock_changes += 1
                sink.event("clock_change", ok=False, reason="wall_monotonic_offset_changed",
                           drift_ms=drift)
                anchor = Anchor(now, current)
            if current >= stop:
                end_wall = min(now, anchor.wall_ms + round((stop - anchor.monotonic_s) * 1000) - 1)
                sink.skip(slot, end_wall, interval_ms, "duration_ended_with_gap")
                break
            if now >= slot + interval_ms:
                slot = sink.skip(slot, now, interval_ms, "sleep_or_delay")
            remaining = anchor.deadline(slot) - current
            if remaining > 0:
                clock.sleep(min(1.0, remaining, stop - current))
                continue
            sink.raw.preflight(now)
            cycle_started = clock.now_ms()
            failures: list[str] = []
            attempts_before = summary.failed_attempts
            endpoints = [
                ("public/get_book_summary_by_currency", {"currency": "BTC", "kind": "option"}),
                ("public/get_index_price", {"index_name": "btc_usd"}),
            ]
            cycle_reason = "interrupted_or_storage_error"
            completed = False
            try:
                for endpoint, params in endpoints:
                    try:
                        client.request(endpoint, params)
                    except ClientError as exc:
                        failures.append(f"{endpoint}:{exc.reason}")
                hour = clock.now_ms() // 3600000
                if instrument_hour != hour:
                    instrument_hour = hour
                    try:
                        client.request("public/get_instruments",
                                       {"currency": "BTC", "kind": "option", "expired": "false"})
                    except ClientError as exc:
                        failures.append(f"public/get_instruments:{exc.reason}")
                completed = True
                cycle_reason = "endpoint_failed" if failures else (
                    "recovered_retry" if summary.failed_attempts > attempts_before else "ok"
                )
            finally:
                summary.cycles += 1
                summary.failed_cycles += int(bool(failures) or not completed)
                sink.event("cycle", ok=completed and not failures, reason=cycle_reason,
                           scheduled_ms=slot, cycle_started_ms=cycle_started,
                           cycle_finished_ms=clock.now_ms(), failures=failures)
            slot += interval_ms
            # Skip any slot reached during the cycle, including an exact boundary finish.
            effective_now = max(clock.now_ms(), anchor.wall_ms + round(
                (clock.monotonic() - anchor.monotonic_s) * 1000))
            if stop != math.inf:
                effective_now = min(effective_now, anchor.wall_ms + round(
                    (stop - anchor.monotonic_s) * 1000) - 1)
            slot = sink.skip(slot, effective_now, interval_ms, "request_overrun")
        reason = "completed"
        return summary
    except KeyboardInterrupt:
        reason = "interrupted"
        LOGGER.warning("Recording interrupted")
        raise
    finally:
        try:
            if client is not None:
                client.close()
            if sink is not None:
                sink.event("session_end", ok=reason == "completed", reason=reason,
                           summary=asdict(summary))
        finally:
            lock.unlink()
