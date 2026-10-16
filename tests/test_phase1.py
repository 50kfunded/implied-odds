"""I use synthetic responses and injected clocks; these tests make no network calls."""

from copy import deepcopy
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from implied_calibration.client import ClientError, PublicClient, validate_response
from implied_calibration.raw import RawWriter, read_raw
from implied_calibration.recorder import record
from implied_calibration.schedule import Anchor, boundary_ms, missed_slots


BOOK = "public/get_book_summary_by_currency"
INDEX = "public/get_index_price"
INSTRUMENTS = "public/get_instruments"


def envelope(result=None, **extra):
    return {"jsonrpc": "2.0", "testnet": False, "usIn": 1000, "usOut": 2000,
            "usDiff": 1000, "result": [] if result is None else result, **extra}


class SyntheticResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self, **kwargs):
        if isinstance(self.body, str):
            return json.loads(self.body, **kwargs)
        return deepcopy(self.body)


class SyntheticClock:
    def __init__(self, wall=0):
        self.wall, self.mono = wall, 0.0

    def now_ms(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.wall += round(seconds * 1000)
        self.mono += seconds


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.network = patch("requests.sessions.Session.request",
                             side_effect=AssertionError("Network calls are forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def client(self, responses, **kwargs):
        session = Mock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = responses
        seen, sleeps = [], []
        client = PublicClient(session=session, on_attempt=seen.append, sleep=sleeps.append, **kwargs)
        self.addCleanup(client.close)
        return client, session, seen, sleeps


class ClientTests(OfflineTest):
    def test_429_then_success_records_both_attempts(self):
        body = envelope([{"bid_price": None, "mark_iv": 55.3}])
        client, session, seen, sleeps = self.client([
            SyntheticResponse("busy", 429), SyntheticResponse(body)])
        response = client.request(BOOK, {"currency": "BTC", "kind": "option"})
        self.assertEqual(response.result, body["result"])
        self.assertEqual([row.reason for row in seen], ["http_error", "ok"])
        self.assertEqual(seen[1].body, body)
        self.assertIsNone(seen[1].body["result"][0]["bid_price"])
        self.assertEqual(seen[0].response_text, "busy")
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 1)
        self.assertIn("implied-odds", session.headers["User-Agent"])
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], "https://www.deribit.com/api/v2/" + BOOK)
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["params"], {"currency": "BTC", "kind": "option"})

    def test_rpc_error_in_http_200_retries_with_cap(self):
        body = envelope()
        del body["result"]
        body["error"] = {"code": 10028, "message": "synthetic rate limit"}
        client, session, seen, sleeps = self.client([SyntheticResponse(body)] * 3, max_retries=2)
        with self.assertRaisesRegex(ClientError, "10028"):
            client.request(BOOK, {})
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([row.reason for row in seen], ["rpc_error"] * 3)
        self.assertEqual(len(sleeps), 2)
        self.assertGreater(sleeps[1], sleeps[0])

    def test_testnet_never_retries_even_on_server_error(self):
        for status in (200, 503):
            with self.subTest(status=status):
                client, session, seen, sleeps = self.client([
                    SyntheticResponse(envelope(testnet=True), status)])
                with self.assertRaisesRegex(ClientError, "boolean false"):
                    client.request(BOOK, {})
                self.assertEqual(session.get.call_count, 1)
                self.assertEqual(seen[0].reason, "testnet_rejected")
                self.assertFalse(sleeps)

    def test_network_and_503_retry_but_400_and_bad_json_stop(self):
        for failure in (requests.Timeout("synthetic timeout"),
                        requests.ConnectionError("synthetic disconnect"), SyntheticResponse("busy", 503)):
            with self.subTest(failure=str(failure)):
                client, session, seen, sleeps = self.client([failure, SyntheticResponse(envelope())])
                client.request(BOOK, {})
                self.assertEqual(session.get.call_count, 2)
                self.assertEqual(len(seen), 2)
        for failure, reason in ((SyntheticResponse({}, 400), "http_error"),
                                (SyntheticResponse("broken"), "invalid_json")):
            client, session, seen, sleeps = self.client([failure])
            with self.assertRaises(ClientError):
                client.request(BOOK, {})
            self.assertEqual(seen[0].reason, reason)
            self.assertEqual(session.get.call_count, 1)
            self.assertFalse(sleeps)

    def test_schema_lists_missing_and_unexpected_keys(self):
        body = envelope(extra="synthetic")
        del body["testnet"]
        with self.assertRaisesRegex(ClientError, "missing keys=.*testnet.*unexpected keys=.*extra"):
            validate_response(BOOK, 200, body)
        for value in (True, 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ClientError):
                validate_response(INDEX, 200, envelope({"index_price": value}))
        for value in (True, "false", None, 0):
            with self.subTest(testnet=value), self.assertRaises(ClientError):
                validate_response(BOOK, 200, envelope(testnet=value))

    def test_storage_error_does_not_retry(self):
        client, session, seen, sleeps = self.client([SyntheticResponse(envelope())])
        client.on_attempt = Mock(side_effect=OSError("synthetic disk full"))
        with self.assertRaisesRegex(OSError, "disk full"):
            client.request(BOOK, {})
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(sleeps)

    def test_interruption_is_recorded_and_reraised(self):
        client, session, seen, sleeps = self.client([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            client.request(BOOK, {})
        self.assertEqual(seen[0].reason, "interrupted")
        self.assertFalse(sleeps)


class RawTests(OfflineTest):
    def test_round_trip_append_and_utc_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RawWriter(Path(tmp))
            rows = [{"fetched_at_ms": 0, "body": {"bid_price": None, "ask_price": 0}},
                    {"fetched_at_ms": 1, "body": {"mark_iv": 55.3}},
                    {"fetched_at_ms": 86400000, "body": {"text": "synthetic café"}}]
            writer.append(rows[0])
            first = Path(tmp) / "1970-01-01.jsonl.gz"
            prefix = first.read_bytes()
            writer.append(rows[1])
            writer.append(rows[2])
            self.assertTrue(first.read_bytes().startswith(prefix))
            self.assertEqual(list(read_raw(first)), rows[:2])
            self.assertEqual(list(read_raw(Path(tmp) / "1970-01-02.jsonl.gz")), rows[2:])

    def test_truncated_tail_recovers_prior_members_and_refuses_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            writer = RawWriter(directory)
            row = {"fetched_at_ms": 0, "body": {"synthetic": True}}
            writer.append(row)
            path = directory / "1970-01-01.jsonl.gz"
            with path.open("ab") as target:
                target.write(gzip.compress(b'{"synthetic_tail":true}\n', mtime=0)[:-8])
            before = path.read_bytes()
            with self.assertLogs("implied_calibration.raw", level="WARNING") as captured:
                self.assertEqual(list(read_raw(path)), [row])
            self.assertIn("truncated_tail", " ".join(captured.output))
            with self.assertRaisesRegex(OSError, "fresh --output"):
                RawWriter(directory).append(row)
            self.assertEqual(path.read_bytes(), before)

    def test_crc_corruption_is_not_treated_as_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corrupt.gz"
            damaged = bytearray(gzip.compress(b'{"synthetic":true}\n', mtime=0))
            damaged[-8] ^= 1
            path.write_bytes(damaged + gzip.compress(b'{"later":true}\n', mtime=0))
            with self.assertRaisesRegex(OSError, "Corrupt gzip"):
                list(read_raw(path))


class ScheduleTests(OfflineTest):
    def test_alignment_and_skip_math(self):
        self.assertEqual(boundary_ms(1, 300000), 300000)
        self.assertEqual(boundary_ms(300000, 300000), 600000)
        self.assertEqual(boundary_ms(300000, 300000, inclusive=True), 300000)
        self.assertEqual(missed_slots(10000, 35000, 10000), 3)
        self.assertEqual(missed_slots(10000, 9999, 10000), 0)
        anchor = Anchor(300000, 20.0)
        self.assertEqual(anchor.deadline(600000), 320.0)
        self.assertEqual(anchor.drift_ms(600000, 320.0), 0)
        with self.assertRaises(ValueError):
            boundary_ms(0, 9000)


class RecorderTests(OfflineTest):
    def run_synthetic(self, clock, seconds, handler):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name)
        session = Mock(spec=requests.Session)
        session.headers = {}
        session.get.side_effect = handler

        def factory(**kwargs):
            return PublicClient(session=session, max_retries=0, **kwargs)

        summary = record(output, interval=10, seconds=seconds, clock=clock, client_factory=factory)
        heartbeat = []
        for path in sorted((output / "heartbeat").glob("*.jsonl")):
            heartbeat.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        return summary, heartbeat, output, session

    @staticmethod
    def success(url, **kwargs):
        return SyntheticResponse(envelope({"index_price": 1}) if url.endswith(INDEX) else envelope())

    def test_endpoint_failure_keeps_later_calls_and_heartbeat(self):
        def handler(url, **kwargs):
            if url.endswith(BOOK):
                return SyntheticResponse({}, 400)
            return self.success(url, **kwargs)

        summary, heartbeat, output, session = self.run_synthetic(SyntheticClock(), 21, handler)
        self.assertEqual(summary.cycles, 3)
        self.assertEqual(summary.failed_cycles, 3)
        self.assertEqual(summary.attempts, 7)
        self.assertEqual(summary.failed_attempts, 3)
        attempts = [row for row in heartbeat if row["event"] == "attempt"]
        self.assertEqual(attempts[1]["endpoint"], INDEX)
        self.assertEqual(len(list(read_raw(output / "raw" / "1970-01-01.jsonl.gz"))), 7)
        instruments = [call for call in session.get.call_args_list if call.args[0].endswith(INSTRUMENTS)]
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0].kwargs["params"]["expired"], "false")
        self.assertFalse((output / ".record.lock").exists())

    def test_hourly_instruments_across_hour_boundary(self):
        summary, heartbeat, output, session = self.run_synthetic(
            SyntheticClock(3590000), 21, self.success)
        calls = [call for call in session.get.call_args_list if call.args[0].endswith(INSTRUMENTS)]
        self.assertEqual(len(calls), 2)

    def test_request_overrun_skips_without_back_to_back_calls(self):
        clock = SyntheticClock()

        def handler(url, **kwargs):
            if url.endswith(BOOK):
                clock.sleep(25)
            return self.success(url, **kwargs)

        summary, heartbeat, output, session = self.run_synthetic(clock, 61, handler)
        slots = [row["scheduled_ms"] for row in heartbeat if row["event"] == "cycle"]
        self.assertEqual(slots, [0, 30000, 60000])
        self.assertGreaterEqual(summary.skipped_slots, 4)
        self.assertTrue(any(row["reason"] == "request_overrun" for row in heartbeat))

    def test_slow_summary_crossing_hour_does_not_duplicate_instruments(self):
        clock = SyntheticClock(3590000)
        first = True

        def handler(url, **kwargs):
            nonlocal first
            if url.endswith(BOOK) and first:
                clock.sleep(15)
                first = False
            return self.success(url, **kwargs)

        summary, heartbeat, output, session = self.run_synthetic(clock, 21, handler)
        calls = [call for call in session.get.call_args_list if call.args[0].endswith(INSTRUMENTS)]
        self.assertEqual(len(calls), 1)

    def test_suspend_when_monotonic_pauses_records_gap(self):
        class SleepingClock(SyntheticClock):
            slept = False

            def sleep(self, seconds):
                super().sleep(seconds)
                if not self.slept:
                    self.wall += 35000
                    self.slept = True

        summary, heartbeat, output, session = self.run_synthetic(SleepingClock(), 21, self.success)
        self.assertEqual(summary.clock_changes, 1)
        self.assertEqual(summary.skipped_slots, 3)
        slots = [row["scheduled_ms"] for row in heartbeat if row["event"] == "cycle"]
        self.assertEqual(slots, [0, 40000, 50000])

    def test_overrun_does_not_count_slots_after_duration(self):
        clock = SyntheticClock()

        def handler(url, **kwargs):
            if url.endswith(BOOK):
                clock.sleep(25)
            return self.success(url, **kwargs)

        summary, heartbeat, output, session = self.run_synthetic(clock, 1, handler)
        self.assertEqual(summary.cycles, 1)
        self.assertEqual(summary.skipped_slots, 0)

    def test_suspend_past_duration_still_records_missed_slots(self):
        class SleepingClock(SyntheticClock):
            def sleep(self, seconds):
                super().sleep(seconds + 100)

        summary, heartbeat, output, session = self.run_synthetic(SleepingClock(), 21, self.success)
        self.assertEqual(summary.skipped_slots, 2)
        self.assertTrue(any(row["reason"] == "duration_ended_with_gap" for row in heartbeat))

    def test_backward_clock_correction_does_not_repeat_utc_slots(self):
        class CorrectedClock(SyntheticClock):
            corrected = False

            def sleep(self, seconds):
                super().sleep(seconds)
                if not self.corrected:
                    self.wall -= 35000
                    self.corrected = True

        summary, heartbeat, output, session = self.run_synthetic(CorrectedClock(60000), 21, self.success)
        self.assertEqual(summary.clock_changes, 1)
        slots = [row["scheduled_ms"] for row in heartbeat if row["event"] == "cycle"]
        self.assertEqual(slots, [60000])

    def test_existing_lock_refuses_output_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".record.lock"
            lock.write_text("synthetic existing lock\n", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(OSError, "locked"):
                record(Path(tmp), 10, 21, clock=SyntheticClock())
            self.assertEqual(lock.read_text(encoding="utf-8"), "synthetic existing lock\n")

    def test_keyboard_interrupt_records_session_end_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Mock(spec=requests.Session)
            session.headers = {}
            session.get.side_effect = KeyboardInterrupt()

            def factory(**kwargs):
                return PublicClient(session=session, max_retries=0, **kwargs)

            with self.assertRaises(KeyboardInterrupt):
                record(Path(tmp), 10, 21, clock=SyntheticClock(), client_factory=factory)
            path = Path(tmp) / "heartbeat" / "1970-01-01.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["reason"], "interrupted")
            self.assertEqual(rows[-2]["reason"], "interrupted_or_storage_error")
            self.assertFalse((Path(tmp) / ".record.lock").exists())

    def test_invalid_intervals_and_durations_fail_before_writing(self):
        for interval, seconds in ((9, 10), (10, 0), (10, -1), (10, True)):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                record(Path(tmp), interval, seconds)


class DiskTests(OfflineTest):
    def test_estimate_from_synthetic_run_span(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "estimate_disk.py"
        spec = importlib.util.spec_from_file_location("estimate_disk", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "heartbeat").mkdir()
            path = directory / "heartbeat" / "synthetic.jsonl"
            rows = [{"run_id": "synthetic", "fetched_at_ms": value} for value in (0, 10000)]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
            result = module.estimate(directory)
            self.assertEqual(result["observed_run_ms"], 10000)
            self.assertEqual(result["estimated_bytes_per_day"], path.stat().st_size * 8640)


if __name__ == "__main__":
    unittest.main()
