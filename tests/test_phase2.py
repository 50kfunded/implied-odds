"""Offline ingestion and coverage checks use synthetic records and temporary databases."""

from contextlib import closing
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import Mock

import requests

from implied_calibration.client import PublicClient
from implied_calibration.coverage import coverage_report
from implied_calibration.ingest import BOOK, INDEX, INSTRUMENTS, ingest
from implied_calibration.instruments import InstrumentError, parse_name, validate_metadata
from implied_calibration.raw import RawWriter
from implied_calibration.recorder import record
from implied_calibration.store import Store, snapshot_id
from test_phase1 import OfflineTest, SyntheticClock, SyntheticResponse, envelope


BASE = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
NAME = "BTC-5DEC26-100-C"


def metadata(name=NAME, **changes):
    parsed = parse_name(name)
    return {"instrument_name": name, "expiration_timestamp": parsed.expiry_ms,
            "strike": parsed.strike, "option_type": parsed.option_type, "tick_size": 0.0001,
            "contract_size": 1.0, "is_active": True,
            "tick_size_steps": [{"above_price": 0.005, "tick_size": 0.0005}], **changes}


def quote(name=NAME, **changes):
    return {"instrument_name": name, "bid_price": None, "ask_price": 0.02,
            "mark_price": 0.015, "mark_iv": 55.3, "underlying_price": 101.0,
            "underlying_index": "SYN.synthetic", "interest_rate": 0, "open_interest": 1,
            "volume": 0, "creation_timestamp": BASE - 10, **changes}


def raw(endpoint, stamp, result, **changes):
    params = {BOOK: {"currency": "BTC", "kind": "option"},
              INDEX: {"index_name": "btc_usd"},
              INSTRUMENTS: {"currency": "BTC", "kind": "option", "expired": "false"}}[endpoint]
    return {"endpoint": endpoint, "params": params, "fetched_at_ms": stamp,
            "request_sent_ms": stamp, "response_received_ms": stamp, "latency_ms": 0,
            "http_status": 200, "body": envelope(result), "ok": True, "reason": "ok", **changes}


def heartbeat(event, stamp, run_id="synthetic", **fields):
    return {"event": event, "fetched_at_ms": stamp, "run_id": run_id,
            "ok": True, "reason": "synthetic", **fields}


class InstrumentTests(OfflineTest):
    def test_all_months_and_both_day_widths_at_utc_eight(self):
        months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
        for month_number, month in enumerate(months, 1):
            for day in (5, 15):
                with self.subTest(month=month, day=day):
                    result = parse_name(f"BTC-{day}{month}26-80000-P")
                    expected = datetime(2026, month_number, day, 8, tzinfo=timezone.utc)
                    self.assertEqual(result.expiry_ms, int(expected.timestamp() * 1000))
                    self.assertEqual(result.strike, 80000)
                    self.assertEqual(result.option_type, "put")

    def test_leap_day_and_year_rollover(self):
        self.assertEqual(parse_name("BTC-29FEB24-1-C").expiry_ms,
                         int(datetime(2024, 2, 29, 8, tzinfo=timezone.utc).timestamp() * 1000))
        self.assertEqual(parse_name("BTC-1JAN26-1-C").expiry_ms - parse_name("BTC-31DEC25-1-C").expiry_ms, 86400000)

    def test_rejects_bad_dates_usdc_unicode_and_trailing_input(self):
        names = ["BTC_USDC-5DEC26-100-C", "BTC-29FEB25-1-C", "BTC-32JAN26-1-C",
                 "BTC-1ABC26-1-C", "BTC-1JAN26-0-C", "BTC-1JAN26-١-C", NAME + "\n",
                 NAME.lower(), "BTC-5DEC26-1.5-C", None]
        for name in names:
            with self.subTest(name=name), self.assertRaises(InstrumentError):
                parse_name(name)

    def test_metadata_mismatches_and_schema_rejections(self):
        for field, value in (("strike", 101), ("expiration_timestamp", parse_name(NAME).expiry_ms + 1),
                             ("option_type", "put")):
            with self.subTest(field=field), self.assertRaisesRegex(InstrumentError, "disagree"):
                validate_metadata(metadata(**{field: value}), BASE)
        for change in ({"tick_size": 0}, {"tick_size_steps": {}}, {"is_active": 1},
                       {"contract_size": 10 ** 400}, {"instrument_type": "linear"}):
            with self.subTest(change=str(change)), self.assertRaises(InstrumentError):
                validate_metadata(metadata(**change), BASE)
        missing = metadata()
        del missing["tick_size"]
        with self.assertRaisesRegex(InstrumentError, "missing keys=.*tick_size"):
            validate_metadata(missing, BASE)
        self.assertFalse(validate_metadata(metadata(is_active=False), BASE).is_active)


class DatabaseFixture(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recording = self.root / "recording"
        self.writer = RawWriter(self.recording / "raw")
        self.store = Store(self.root / "synthetic.sqlite")
        self.addCleanup(self.store.close)

    def write(self, *rows):
        for row in rows:
            self.writer.append(row)

    def events(self, *rows):
        directory = self.recording / "heartbeat"
        directory.mkdir(exist_ok=True)
        with (directory / "synthetic.jsonl").open("a", encoding="utf-8", newline="\n") as target:
            for row in rows:
                target.write(json.dumps(row) + "\n")

    def seed(self, names=(NAME,), stamp=BASE):
        self.write(raw(INSTRUMENTS, stamp - 20, [metadata(name) for name in names]),
                   raw(INDEX, stamp - 10, {"index_price": 100.0}))

    def book(self, stamp=BASE, rows=None, **changes):
        row = raw(BOOK, stamp, [quote()] if rows is None else rows, **changes)
        self.write(row)
        return snapshot_id(BOOK, stamp)

    def session(self, slots, start=BASE, seconds=40, interval=10000, end=None, run_id="synthetic"):
        self.events(heartbeat("session_start", start, run_id, first_slot_ms=start,
                              interval_ms=interval, seconds=seconds, anchor_wall_ms=start))
        for slot in slots:
            self.events(heartbeat("cycle", slot + 5, run_id, scheduled_ms=slot,
                                  cycle_started_ms=slot, cycle_finished_ms=slot + 5))
        if end is not None:
            self.events(heartbeat("session_end", end, run_id))


class IngestionTests(DatabaseFixture):
    def test_wal_foreign_keys_tables_and_idempotency(self):
        self.seed()
        self.book()
        self.session([BASE], seconds=1, end=BASE + 1000)
        first = ingest(self.recording, self.store)
        before = self.store.counts()
        second = ingest(self.recording, self.store)
        self.assertEqual(self.store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(first["added"]["quotes"], 1)
        self.assertTrue(all(value == 0 for value in second["added"].values()))
        self.assertEqual(before, self.store.counts())
        for table in ("delivery_prices", "predictions", "scores"):
            self.assertEqual(before[table], 0)
        self.assertEqual(snapshot_id(BOOK, BASE), snapshot_id(BOOK, BASE))

    def test_nulls_zeroes_and_original_units_survive(self):
        self.seed()
        identity = self.book(rows=[quote(bid_price=None, ask_price=0, volume=None)])
        ingest(self.recording, self.store)
        row = self.store.quotes_as_of(identity, BASE)[0]
        self.assertIsNone(row["bid_btc"])
        self.assertIsNone(row["volume"])
        self.assertEqual(row["ask_btc"], 0)
        self.assertEqual(row["mark_iv_pct"], 55.3)
        self.assertEqual(row["underlying_price"], 101)
        self.assertEqual(row["underlying_index"], "SYN.synthetic")
        steps = json.loads(self.store.latest_instrument(NAME, BASE)["tick_size_steps"])
        self.assertEqual(steps, [{"above_price": 0.005, "tick_size": 0.0005}])

    def test_later_same_cycle_metadata_and_index_do_not_validate_earlier_quote(self):
        identity = self.book(BASE)
        self.seed(stamp=BASE + 1000)
        ingest(self.recording, self.store)
        rows = self.store.quotes_as_of(identity, BASE, verified_only=False)
        self.assertEqual(rows[0]["identity_status"], "missing_metadata")
        self.assertIsNone(rows[0]["metadata_fetched_at_ms"])
        self.assertEqual(self.store.quotes_as_of(identity, BASE + 10000), [])
        snapshot = self.store.connection.execute("SELECT * FROM snapshots").fetchone()
        self.assertIsNone(snapshot["index_price"])
        self.assertEqual(snapshot["status"], "partial")

    def test_future_observations_do_not_change_prior_as_of_output(self):
        self.seed()
        identity = self.book()
        ingest(self.recording, self.store)
        quotes_before = [dict(row) for row in self.store.quotes_as_of(identity, BASE)]
        instrument_before = dict(self.store.latest_instrument(NAME, BASE))
        index_before = dict(self.store.latest_index(BASE))
        report_before = coverage_report(self.store.path, BASE)
        self.write(raw(INSTRUMENTS, BASE + 100, [metadata(tick_size=0.001)]),
                   raw(INDEX, BASE + 200, {"index_price": 10000.0}))
        future = self.book(BASE + 300)
        ingest(self.recording, self.store)
        self.assertEqual(quotes_before, [dict(row) for row in self.store.quotes_as_of(identity, BASE)])
        self.assertEqual(instrument_before, dict(self.store.latest_instrument(NAME, BASE)))
        self.assertEqual(index_before, dict(self.store.latest_index(BASE)))
        self.assertEqual(report_before, coverage_report(self.store.path, BASE))
        self.assertEqual(self.store.quotes_as_of(future, BASE), [])

    def test_horizon_is_relative_inclusive_and_source_count_is_separate(self):
        names = ("BTC-1JAN26-100-C", "BTC-2JAN26-100-C", "BTC-1JAN24-100-C")
        stamp = parse_name(names[0]).expiry_ms - 400 * 86400000
        self.seed(names, stamp)
        self.book(stamp, [quote(name) for name in names])
        result = ingest(self.recording, self.store)
        snapshot = self.store.connection.execute("SELECT * FROM snapshots").fetchone()
        self.assertEqual(snapshot["n_instruments"], 3)
        self.assertEqual(snapshot["n_quotes_stored"], 1)
        self.assertEqual(result["issue_counts"]["beyond_max_days_to_expiry"], 1)
        self.assertEqual(result["issue_counts"]["expired_instrument"], 1)
        with self.assertRaisesRegex(ValueError, "horizon differs"):
            Store(self.store.path, 30)

    def test_conflicting_raw_content_rolls_back_import(self):
        self.seed()
        self.book()
        self.book(rows=[quote(bid_price=0.5)])
        with self.assertRaisesRegex(ValueError, "Conflicting raw content"):
            ingest(self.recording, self.store)
        self.assertTrue(all(value == 0 for value in self.store.counts().values()))

    def test_invalid_first_same_id_cannot_consume_approved_payload(self):
        self.seed()
        bad = raw(BOOK, BASE, [quote(bid_price=0.9)], params={"currency": "USDC"})
        self.write(bad)
        identity = self.book(rows=[quote(bid_price=0.1)])
        result = ingest(self.recording, self.store)
        self.assertEqual(self.store.quotes_as_of(identity, BASE)[0]["bid_btc"], 0.1)
        self.assertEqual(result["issue_counts"]["invalid_raw_record"], 1)

    def test_conflicting_latest_metadata_quarantines_older_valid_identity(self):
        self.seed()
        self.write(raw(INSTRUMENTS, BASE - 5, [metadata(strike=999)]))
        self.book()
        result = ingest(self.recording, self.store)
        self.assertIsNone(self.store.latest_instrument(NAME, BASE))
        self.assertEqual(result["totals"]["quotes"], 0)
        self.assertEqual(result["issue_counts"]["metadata_identity_mismatch"], 1)
        self.assertEqual(result["issue_counts"]["quarantined_metadata"], 1)

    def test_duplicate_rows_are_quarantined_and_counted(self):
        self.seed()
        self.book(rows=[quote(), quote(bid_price=0.1)])
        result = ingest(self.recording, self.store)
        self.assertEqual(result["totals"]["quotes"], 0)
        self.assertEqual(result["issue_counts"]["duplicate_quote"], 2)
        self.assertEqual(self.store.connection.execute("SELECT n_instruments FROM snapshots").fetchone()[0], 1)

    def test_corrupt_lines_and_tail_are_visible_and_idempotent(self):
        self.seed()
        self.book()
        path = next((self.recording / "raw").glob("*.gz"))
        with path.open("ab") as target:
            target.write(gzip.compress(b"synthetic broken JSON\n", mtime=0))
            target.write(gzip.compress(b'{"synthetic_tail":true}\n', mtime=0)[:-8])
        with self.assertLogs("implied_calibration.raw", level="WARNING"):
            first = ingest(self.recording, self.store)
        with self.assertLogs("implied_calibration.raw", level="WARNING"):
            second = ingest(self.recording, self.store)
        self.assertEqual(first["totals"]["quotes"], 1)
        self.assertEqual(first["issue_counts"]["corrupt_json_line"], 1)
        self.assertEqual(first["issue_counts"]["truncated_tail"], 1)
        self.assertTrue(all(value == 0 for value in second["added"].values()))

    def test_corrupt_line_issue_does_not_duplicate_after_append(self):
        self.seed()
        path = next((self.recording / "raw").glob("*.gz"))
        with path.open("ab") as target:
            target.write(gzip.compress(b"synthetic broken JSON\n", mtime=0))
        with self.assertLogs("implied_calibration.raw", level="WARNING"):
            ingest(self.recording, self.store)
        self.book()
        with self.assertLogs("implied_calibration.raw", level="WARNING"):
            result = ingest(self.recording, self.store)
        self.assertEqual(result["issue_counts"]["corrupt_json_line"], 1)

    def test_crc_corruption_aborts_instead_of_hiding_a_damaged_member(self):
        self.seed()
        path = next((self.recording / "raw").glob("*.gz"))
        damaged = bytearray(gzip.compress(b'{"synthetic":true}\n', mtime=0))
        damaged[-8] ^= 1
        with path.open("ab") as target:
            target.write(damaged)
        with self.assertRaisesRegex(OSError, "Corrupt gzip"):
            ingest(self.recording, self.store)
        self.assertTrue(all(value == 0 for value in self.store.counts().values()))

    def test_oversized_integer_fields_are_counted_and_skipped(self):
        self.seed()
        self.book(rows=[quote(creation_timestamp=2 ** 80)])
        self.events(heartbeat("session_start", 2 ** 80, first_slot_ms=0, interval_ms=10000, seconds=1))
        with self.assertLogs("implied_calibration.ingest", level="WARNING"):
            result = ingest(self.recording, self.store)
        self.assertEqual(result["totals"]["quotes"], 0)
        self.assertEqual(result["issue_counts"]["invalid_quote_field"], 1)
        self.assertEqual(result["issue_counts"]["corrupt_heartbeat_line"], 1)

    def test_unrelated_database_is_not_modified(self):
        path = self.root / "unrelated.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaisesRegex(ValueError, "unrelated nonempty"):
            Store(path)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [("unrelated",)])

    def test_usdc_and_nonfinite_quote_fields_get_explicit_reasons(self):
        self.seed()
        self.book(rows=[quote("BTC_USDC-5DEC26-100-C"), quote(bid_price="synthetic invalid")])
        result = ingest(self.recording, self.store)
        self.assertEqual(result["totals"]["quotes"], 0)
        self.assertEqual(result["issue_counts"]["invalid_instrument_name"], 1)
        self.assertEqual(result["issue_counts"]["invalid_quote_field"], 1)

    def test_duplicate_instruments_do_not_silently_choose_a_version(self):
        self.write(raw(INSTRUMENTS, BASE - 10, [metadata(), metadata(tick_size=0.002)]))
        self.book()
        result = ingest(self.recording, self.store)
        self.assertEqual(result["totals"]["instruments"], 0)
        self.assertEqual(result["issue_counts"]["duplicate_instrument"], 2)
        self.assertEqual(result["issue_counts"]["quarantined_metadata"], 1)


class CoverageTests(DatabaseFixture):
    def test_missing_slots_and_edge_gaps_use_declared_window(self):
        self.seed()
        self.book(BASE + 10000)
        self.book(BASE + 20000)
        self.session([BASE + 10000, BASE + 20000], end=BASE + 40006)
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path)
        self.assertEqual(report["days"][0]["expected_slots"], 4)
        self.assertEqual(report["days"][0]["received_slots"], 2)
        self.assertEqual(report["days"][0]["missing_slots"], 2)
        self.assertEqual(report["days"][0]["longest_gap_ms"], 10000)

    def test_consecutive_missing_slots_form_one_long_gap(self):
        self.seed()
        self.book(BASE)
        self.book(BASE + 30000)
        self.session([BASE, BASE + 30000], end=BASE + 40000)
        ingest(self.recording, self.store)
        self.assertEqual(coverage_report(self.store.path)["days"][0]["longest_gap_ms"], 20000)

    def test_partial_days_split_at_utc_midnight(self):
        start = BASE + 86400000 - 10000
        self.seed(stamp=start)
        self.book(start + 10000)
        self.session([start + 10000], start=start, seconds=20, end=start + 20000)
        ingest(self.recording, self.store)
        days = coverage_report(self.store.path)["days"]
        self.assertEqual([row["expected_slots"] for row in days], [1, 1])
        self.assertEqual([row["received_slots"] for row in days], [0, 1])

    def test_low_count_uses_prior_history_and_source_not_retained_counts(self):
        names = [f"BTC-5DEC26-{strike}-C" for strike in range(100, 200)]
        self.seed(names)
        for index, count in enumerate((100, 100, 1, 40)):
            self.book(BASE + index * 10000, [quote(name) for name in names[:count]])
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path, trailing_window=3, min_history=3)
        self.assertEqual(len(report["low_instrument_counts"]), 1)
        self.assertEqual(report["low_instrument_counts"][0]["n_instruments"], 40)
        self.assertEqual(report["low_instrument_counts"][0]["trailing_median"], 100)
        self.assertEqual(sum(row["n_instruments"] for row in report["instruments_per_expiry"]), 40)

    def test_open_session_is_labelled_and_explicit_asof_exposes_missing_slots(self):
        self.seed()
        self.book()
        self.session([BASE], seconds=None)
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path)
        self.assertEqual(report["incomplete_sessions"], 1)
        self.assertEqual(report["sessions"][0]["bound"], "last_observed_event")
        self.assertEqual(report["days"][0]["expected_slots"], 1)
        later = coverage_report(self.store.path, BASE + 30000)
        self.assertEqual(later["days"][0]["expected_slots"], 4)
        self.assertEqual(later["days"][0]["missing_slots"], 3)

    def test_long_cycle_maps_to_its_original_slot_and_respects_duration(self):
        clock = SyntheticClock(BASE)
        session = Mock(spec=requests.Session)
        session.headers = {}

        def handler(url, **kwargs):
            if url.endswith(BOOK):
                clock.sleep(25)
            return SyntheticResponse(envelope({"index_price": 1}) if url.endswith(INDEX) else envelope())

        session.get.side_effect = handler

        def factory(**kwargs):
            return PublicClient(session=session, max_retries=0, **kwargs)

        record(self.recording, 10, 1, clock=clock, client_factory=factory)
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path)
        self.assertEqual(report["days"][0]["expected_slots"], 1)
        self.assertEqual(report["days"][0]["received_slots"], 1)
        self.assertEqual(report["days"][0]["missing_slots"], 0)
        self.assertEqual(report["inferred_slot_assignments"], 0)

    def test_time_between_sessions_is_visible_but_not_scheduled(self):
        self.seed()
        self.book(BASE)
        self.book(BASE + 100000)
        self.session([BASE], seconds=10, end=BASE + 10000, run_id="synthetic-a")
        self.session([BASE + 100000], start=BASE + 100000, seconds=10,
                     end=BASE + 110000, run_id="synthetic-b")
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path)
        self.assertEqual(report["days"][0]["expected_slots"], 2)
        self.assertEqual(report["unobserved_between_sessions"][0]["duration_ms"], 90000)

    def test_clock_change_marks_expected_window_uncertain_and_retains_gap_events(self):
        self.seed()
        self.book()
        self.session([BASE], seconds=40, end=BASE + 40000)
        self.events(heartbeat("clock_change", BASE + 5000, ok=False, drift_ms=20000),
                    heartbeat("gap", BASE + 25000, ok=False, first_slot_ms=BASE + 10000,
                              last_slot_ms=BASE + 20000, count=2))
        ingest(self.recording, self.store)
        report = coverage_report(self.store.path)
        self.assertFalse(report["sessions"][0]["expected_slots_known"])
        self.assertEqual(report["warnings"][0]["reason"], "clock_change_expected_window_unknown")
        self.assertEqual(report["days"][0]["expected_slots"], 3)
        self.assertEqual(report["days"][0]["missing_slots"], 2)
