"""Captured responses are ingested without changing raw files or using later data."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from .client import ClientError, validate_response
from .instruments import InstrumentError, finite_number, parse_name, validate_metadata
from .raw import read_raw
from .store import QuoteRow, Store, canonical, digest, snapshot_id


LOGGER = logging.getLogger(__name__)
BOOK = "public/get_book_summary_by_currency"
INSTRUMENTS = "public/get_instruments"
INDEX = "public/get_index_price"


def validate_record(row: dict[str, Any]) -> tuple[bool, str]:
    required = {"endpoint", "params", "fetched_at_ms", "request_sent_ms", "response_received_ms",
                "latency_ms", "http_status", "body", "ok", "reason"}
    missing = sorted(required - row.keys())
    if missing:
        raise ValueError(f"raw record: missing keys={missing}; unexpected keys=[]")
    for field in ("fetched_at_ms", "request_sent_ms", "response_received_ms", "latency_ms"):
        if type(row[field]) is not int or not 0 <= row[field] <= 9223372036854775807:
            raise ValueError(f"raw {field}: expected nonnegative integer milliseconds")
    if row["fetched_at_ms"] != row["response_received_ms"] or row["request_sent_ms"] > row["response_received_ms"]:
        raise ValueError("raw record timings are inconsistent")
    if type(row["ok"]) is not bool or not isinstance(row["reason"], str):
        raise ValueError("raw ok must be boolean and reason string")
    expected = {BOOK: {"currency": "BTC", "kind": "option"},
                INSTRUMENTS: {"currency": "BTC", "kind": "option", "expired": "false"},
                INDEX: {"index_name": "btc_usd"}}
    if not isinstance(row["endpoint"], str) or row["endpoint"] not in expected:
        raise ValueError("Unsupported raw endpoint")
    if row["params"] != expected[row["endpoint"]]:
        raise ValueError(f"Unexpected params for {row['endpoint']}")
    canonical(row)
    if not row["ok"]:
        return False, row["reason"]
    if type(row["http_status"]) is not int:
        raise ValueError("Successful raw http_status must be an integer")
    try:
        validate_response(row["endpoint"], row["http_status"], row["body"])
    except ClientError as exc:
        return False, exc.reason
    return True, "ok"


def quote_row(row: dict[str, Any], identity: str, fetched_at_ms: int, store: Store) -> QuoteRow:
    name = row.get("instrument_name")
    if not isinstance(name, str):
        raise InstrumentError("invalid_instrument_name", "instrument_name must be a string")
    parsed = parse_name(name)
    metadata = store.latest_instrument(name, fetched_at_ms)
    if metadata is None and store.metadata_rejection(name, fetched_at_ms) is not None:
        raise InstrumentError("quarantined_metadata", f"The latest known metadata was rejected for {name}")
    if metadata is not None and any(metadata[field] != getattr(parsed, field)
                                    for field in ("strike", "expiry_ms", "option_type")):
        raise InstrumentError("metadata_identity_mismatch", f"Stored metadata conflicts with {name}")
    mapping = {"bid_btc": "bid_price", "ask_btc": "ask_price", "mark_btc": "mark_price",
               "mark_iv_pct": "mark_iv", "underlying_price": "underlying_price",
               "interest_rate": "interest_rate", "open_interest": "open_interest", "volume": "volume"}
    values: dict[str, Any] = {}
    for target, source in mapping.items():
        value = row.get(source)
        if value is not None and not finite_number(value):
            raise InstrumentError("invalid_quote_field", f"{source} must be numeric or null")
        values[target] = None if value is None else float(value)
    timestamp = row.get("creation_timestamp")
    if timestamp is not None and (type(timestamp) is not int or not 0 <= timestamp <= 9223372036854775807):
        raise InstrumentError("invalid_quote_field", "creation_timestamp must be integer milliseconds or null")
    underlying = row.get("underlying_index")
    if underlying is not None and not isinstance(underlying, str):
        raise InstrumentError("invalid_quote_field", "underlying_index must be string or null")
    origin = parsed if metadata is None else metadata
    get = (lambda field: getattr(origin, field)) if metadata is None else (lambda field: origin[field])
    return QuoteRow(identity, name, get("expiry_ms"), get("strike"), get("option_type"),
                    values["bid_btc"], values["ask_btc"], values["mark_btc"], values["mark_iv_pct"],
                    values["underlying_price"], underlying, values["interest_rate"], values["open_interest"],
                    values["volume"], timestamp, None if metadata is None else metadata["fetched_at_ms"],
                    "missing_metadata" if metadata is None else "verified")


def ingest(directory: Path, store: Store) -> dict[str, Any]:
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("--input must be an existing recording directory")
    paths = sorted((directory / "raw").glob("*.jsonl.gz"))
    if not paths:
        raise ValueError("No raw/*.jsonl.gz files found under --input")
    before = store.counts()
    duplicate_raw = 0
    new_books: dict[str, str] = {}
    file_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    def records(diagnostics: bool) -> Iterator[dict[str, Any]]:
        for path in paths:
            def capture(issue: dict[str, Any]) -> None:
                store.add_issue(digest([path.name, issue["source_prefix"]]), None, issue["reason"],
                                canonical(issue), row_number=issue["line"])
            yield from read_raw(path, on_issue=capture if diagnostics else None)

    with store.connection:
        for row in records(True):
            try:
                ok, reason = validate_record(row)
            except (ValueError, TypeError, OverflowError) as exc:
                source = hashlib.sha256(repr(row).encode("utf-8")).hexdigest()
                store.add_issue(source, None, "invalid_raw_record", str(exc))
                LOGGER.warning("invalid_raw_record: %s", exc)
                continue
            identity, added = store.add_raw(row, ok, reason)
            if not added:
                duplicate_raw += 1
                continue
            if not ok:
                store.add_issue(identity, row["fetched_at_ms"], reason, "Captured request failed validation")
                LOGGER.warning("captured_failure endpoint=%s reason=%s", row["endpoint"], reason)
                continue
            result = row["body"]["result"]
            if row["endpoint"] == INDEX:
                store.connection.execute("INSERT INTO index_observations VALUES (?, ?, ?)",
                                         (identity, row["fetched_at_ms"], result["index_price"]))
            elif row["endpoint"] == INSTRUMENTS:
                names = Counter(item.get("instrument_name") for item in result
                                if isinstance(item.get("instrument_name"), str))
                for ordinal, item in enumerate(result):
                    try:
                        name = item.get("instrument_name")
                        if isinstance(name, str) and names[name] > 1:
                            raise InstrumentError("duplicate_instrument", f"Repeated metadata name: {name}")
                        store.add_instrument(validate_metadata(item, row["fetched_at_ms"]))
                    except InstrumentError as exc:
                        store.add_issue(identity, row["fetched_at_ms"], exc.reason, str(exc), ordinal,
                                        name if isinstance(name, str) else None)
                        LOGGER.warning("%s: %s", exc.reason, exc)
            else:
                new_books[identity] = digest(row)
        for row in records(False):
            if row.get("endpoint") != BOOK or type(row.get("fetched_at_ms")) is not int:
                continue
            identity = snapshot_id(BOOK, row["fetched_at_ms"])
            if identity not in new_books:
                continue
            try:
                approved = digest(row) == new_books[identity]
            except (ValueError, TypeError, OverflowError):
                approved = False
            if not approved:
                continue
            del new_books[identity]
            result = row["body"]["result"]
            names = Counter(item.get("instrument_name") for item in result
                            if isinstance(item.get("instrument_name"), str))
            observed: set[str] = set()
            kept: list[QuoteRow] = []
            reasons: Counter[str] = Counter()
            for ordinal, item in enumerate(result):
                name = item.get("instrument_name")
                try:
                    if not isinstance(name, str):
                        raise InstrumentError("invalid_instrument_name", "instrument_name must be a string")
                    parsed = parse_name(name)
                    observed.add(name)
                    if names[name] > 1:
                        raise InstrumentError("duplicate_quote", f"Repeated quote name: {name}")
                    remaining = parsed.expiry_ms - row["fetched_at_ms"]
                    if remaining < 0:
                        raise InstrumentError("expired_instrument", "Expiry precedes this snapshot")
                    if remaining > store.max_days_to_expiry * 86400000:
                        raise InstrumentError("beyond_max_days_to_expiry", "Expiry exceeds this database's horizon")
                    quote = quote_row(item, identity, row["fetched_at_ms"], store)
                    kept.append(quote)
                    if quote.identity_status == "missing_metadata":
                        reasons["missing_metadata"] += 1
                        store.add_issue(identity, row["fetched_at_ms"], "missing_metadata",
                                        "I have no instrument metadata observed by this snapshot", ordinal, name)
                except InstrumentError as exc:
                    reasons[exc.reason] += 1
                    store.add_issue(identity, row["fetched_at_ms"], exc.reason, str(exc), ordinal,
                                    name if isinstance(name, str) else None)
            index = store.latest_index(row["fetched_at_ms"])
            if index is None:
                reasons["missing_index"] += 1
                store.add_issue(identity, row["fetched_at_ms"], "missing_index",
                                "I have no index observation available by this snapshot")
            status = "ok" if not reasons else "partial"
            store.connection.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                identity, row["fetched_at_ms"], row["request_sent_ms"], row["response_received_ms"], row["latency_ms"],
                None if index is None else index["index_price"], None if index is None else index["fetched_at_ms"],
                None if index is None else row["fetched_at_ms"] - index["fetched_at_ms"], len(observed), len(kept), status))
            if kept:
                fields = list(asdict(kept[0]))
                store.connection.executemany(
                    f"INSERT INTO quotes ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                    [tuple(asdict(quote).values()) for quote in kept])
            LOGGER.info("snapshot=%s stored=%s observed=%s reasons=%s", identity, len(kept), len(observed), dict(reasons))
        for path in sorted((directory / "heartbeat").glob("*.jsonl")):
            with path.open("rb") as source:
                for ordinal, line in enumerate(source, 1):
                    try:
                        event = json.loads(line)
                        validate_heartbeat(event)
                    except (ValueError, TypeError, KeyError) as exc:
                        source_id = digest([path.name, ordinal, hashlib.sha256(line).hexdigest()])
                        store.add_issue(source_id, None, "corrupt_heartbeat_line", str(exc), ordinal)
                        LOGGER.warning("corrupt_heartbeat_line %s:%s reason=%s", path.name, ordinal, exc)
                        continue
                    store.connection.execute("INSERT OR IGNORE INTO heartbeat VALUES (?, ?, ?, ?, ?, ?, ?)", (
                        digest(event), event["run_id"], event["fetched_at_ms"], event["event"], int(event["ok"]),
                        event["reason"], canonical(event)))
        if new_books or any(hashlib.sha256(path.read_bytes()).hexdigest() != file_hashes[path] for path in paths):
            raise OSError("Raw files changed during ingestion; retry after the recording stops")
    after = store.counts()
    return {"added": {table: after[table] - before[table] for table in before}, "totals": after,
            "duplicate_raw_records": duplicate_raw, "max_days_to_expiry": store.max_days_to_expiry,
            "issue_counts": dict(store.connection.execute(
                "SELECT reason, COUNT(*) FROM issues GROUP BY reason ORDER BY reason").fetchall())}


def validate_heartbeat(event: Any) -> None:
    required = {"event", "run_id", "fetched_at_ms", "ok", "reason"}
    if not isinstance(event, dict) or not required <= event.keys():
        missing = sorted(required - event.keys()) if isinstance(event, dict) else sorted(required)
        raise ValueError(f"heartbeat: missing keys={missing}; unexpected keys=[]")
    if (type(event["fetched_at_ms"]) is not int or not 0 <= event["fetched_at_ms"] <= 9223372036854775807
            or type(event["ok"]) is not bool):
        raise ValueError("heartbeat timestamp must be integer milliseconds and ok boolean")
    if any(not isinstance(event[field], str) for field in ("event", "run_id", "reason")):
        raise ValueError("heartbeat event, run_id and reason must be strings")
    fields = {"session_start": ("first_slot_ms", "interval_ms"),
              "cycle": ("scheduled_ms", "cycle_started_ms", "cycle_finished_ms")}.get(event["event"], ())
    for field in fields:
        if type(event.get(field)) is not int or not 0 <= event[field] <= 9223372036854775807:
            raise ValueError(f"heartbeat {field} must be nonnegative integer milliseconds")
    if event["event"] == "session_start" and event["interval_ms"] < 10000:
        raise ValueError("heartbeat interval must be at least 10000 ms")
    if event["event"] == "session_start":
        seconds = event.get("seconds")
        if seconds is not None and (type(seconds) is not int or seconds <= 0):
            raise ValueError("heartbeat seconds must be null or a positive integer")
        if "anchor_wall_ms" in event and (type(event["anchor_wall_ms"]) is not int
                or not 0 <= event["anchor_wall_ms"] <= 9223372036854775807):
            raise ValueError("heartbeat anchor_wall_ms must be nonnegative integer milliseconds")
    if event["event"] == "cycle" and event["cycle_finished_ms"] < event["cycle_started_ms"]:
        raise ValueError("heartbeat cycle timestamps are out of order")
    if event["event"] == "gap":
        for field in ("first_slot_ms", "last_slot_ms", "count"):
            if type(event.get(field)) is not int or not 0 <= event[field] <= 9223372036854775807:
                raise ValueError(f"heartbeat gap {field} must be a nonnegative integer")
        if event["count"] == 0 or event["last_slot_ms"] < event["first_slot_ms"]:
            raise ValueError("heartbeat gap bounds are inconsistent")
    canonical(event)
