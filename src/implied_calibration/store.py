"""I keep immutable capture rows and timestamped metadata in a local SQLite file."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .instruments import Instrument


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def snapshot_id(endpoint: str, fetched_at_ms: int) -> str:
    return digest([endpoint, fetched_at_ms])


@dataclass(frozen=True)
class QuoteRow:
    snapshot_id: str
    instrument_name: str
    expiry_ms: int
    strike: float
    option_type: str
    bid_btc: float | None
    ask_btc: float | None
    mark_btc: float | None
    mark_iv_pct: float | None
    underlying_price: float | None
    underlying_index: str | None
    interest_rate: float | None
    open_interest: float | None
    volume: float | None
    creation_timestamp_ms: int | None
    metadata_fetched_at_ms: int | None
    identity_status: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_records (
    record_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, fetched_at_ms INTEGER NOT NULL,
    request_sent_ms INTEGER NOT NULL, response_received_ms INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL, http_status INTEGER, ok INTEGER NOT NULL,
    reason TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    instrument_name TEXT NOT NULL, fetched_at_ms INTEGER NOT NULL, expiry_ms INTEGER NOT NULL,
    strike REAL NOT NULL, option_type TEXT NOT NULL, tick_size REAL NOT NULL,
    tick_size_steps TEXT, contract_size REAL NOT NULL, is_active INTEGER NOT NULL,
    PRIMARY KEY (instrument_name, fetched_at_ms)
);
CREATE TABLE IF NOT EXISTS index_observations (
    record_id TEXT PRIMARY KEY REFERENCES raw_records(record_id),
    fetched_at_ms INTEGER NOT NULL, index_price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS index_time ON index_observations(fetched_at_ms);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY REFERENCES raw_records(record_id),
    fetched_at_ms INTEGER NOT NULL, request_sent_ms INTEGER NOT NULL,
    response_received_ms INTEGER NOT NULL, latency_ms INTEGER NOT NULL,
    index_price REAL, index_fetched_at_ms INTEGER, index_age_ms INTEGER,
    n_instruments INTEGER NOT NULL, n_quotes_stored INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(fetched_at_ms);
CREATE TABLE IF NOT EXISTS quotes (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    instrument_name TEXT NOT NULL, expiry_ms INTEGER NOT NULL, strike REAL NOT NULL,
    option_type TEXT NOT NULL, bid_btc REAL, ask_btc REAL, mark_btc REAL, mark_iv_pct REAL,
    underlying_price REAL, underlying_index TEXT, interest_rate REAL,
    open_interest REAL, volume REAL, creation_timestamp_ms INTEGER,
    metadata_fetched_at_ms INTEGER, identity_status TEXT NOT NULL,
    UNIQUE(snapshot_id, instrument_name)
);
CREATE INDEX IF NOT EXISTS quotes_snapshot ON quotes(snapshot_id);
CREATE INDEX IF NOT EXISTS quotes_expiry_snapshot ON quotes(expiry_ms, snapshot_id);
CREATE TABLE IF NOT EXISTS heartbeat (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fetched_at_ms INTEGER NOT NULL,
    event TEXT NOT NULL, ok INTEGER NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS heartbeat_time ON heartbeat(fetched_at_ms);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, fetched_at_ms INTEGER,
    row_number INTEGER, instrument_name TEXT, reason TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS issue_instrument_time ON issues(instrument_name, fetched_at_ms);
CREATE TABLE IF NOT EXISTS delivery_prices (
    expiry_ms INTEGER PRIMARY KEY, delivery_price REAL NOT NULL, fetched_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id TEXT PRIMARY KEY, expiry_ms INTEGER NOT NULL, lead_ms INTEGER NOT NULL,
    snapshot_id TEXT REFERENCES snapshots(snapshot_id), status TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    prediction_id TEXT NOT NULL REFERENCES predictions(prediction_id), metric TEXT NOT NULL,
    value REAL, PRIMARY KEY (prediction_id, metric)
);
CREATE TABLE IF NOT EXISTS evaluation_protocols (
    protocol_hash TEXT PRIMARY KEY, content_json TEXT NOT NULL, lock_json TEXT NOT NULL,
    witness_commit TEXT NOT NULL, witness_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_sources (
    source_id TEXT PRIMARY KEY, fetched_at_ms INTEGER NOT NULL, params_json TEXT NOT NULL,
    body_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_observations (
    expiry_ms INTEGER PRIMARY KEY REFERENCES delivery_prices(expiry_ms),
    source_id TEXT NOT NULL REFERENCES delivery_sources(source_id), content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS score_records (
    prediction_id TEXT PRIMARY KEY REFERENCES predictions(prediction_id), payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_sources (
    source_id TEXT PRIMARY KEY, fetched_at_ms INTEGER NOT NULL, params_json TEXT NOT NULL, body_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hourly_observations (
    open_ms INTEGER NOT NULL, fetched_at_ms INTEGER NOT NULL, close_ms INTEGER NOT NULL,
    close_usd REAL NOT NULL, source_id TEXT NOT NULL REFERENCES history_sources(source_id),
    PRIMARY KEY(open_ms, fetched_at_ms, source_id)
);
CREATE INDEX IF NOT EXISTS hourly_cutoff ON hourly_observations(close_ms, fetched_at_ms);
CREATE TABLE IF NOT EXISTS baseline_protocols (
    extension_hash TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baseline_predictions (
    baseline_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL REFERENCES predictions(prediction_id),
    baseline_name TEXT NOT NULL, extension_hash TEXT NOT NULL REFERENCES baseline_protocols(extension_hash),
    payload_json TEXT NOT NULL, UNIQUE(prediction_id, baseline_name, extension_hash)
);
CREATE TABLE IF NOT EXISTS baseline_scores (
    baseline_id TEXT PRIMARY KEY REFERENCES baseline_predictions(baseline_id), payload_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path, max_days_to_expiry: int = 400) -> None:
        if type(max_days_to_expiry) is not int or max_days_to_expiry < 0:
            raise ValueError("max_days_to_expiry must be a nonnegative integer")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise ValueError(f"Unsupported database schema version: {version}")
            if version == 0 and self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise ValueError("I won't initialise an unrelated nonempty database; use a new --db path")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.executescript(SCHEMA)
            for table in ('predictions', 'scores', 'score_records', 'delivery_prices', 'delivery_sources',
                          'delivery_observations', 'evaluation_protocols', 'history_sources', 'hourly_observations',
                          'baseline_protocols', 'baseline_predictions', 'baseline_scores'):
                for action in ('UPDATE', 'DELETE'):
                    self.connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} "
                                            f"BEFORE {action} ON {table} BEGIN "
                                            "SELECT RAISE(ABORT, 'I keep evaluation rows append-only'); END")
                keys = {'predictions': ('prediction_id',), 'scores': ('prediction_id', 'metric'),
                        'score_records': ('prediction_id',), 'delivery_prices': ('expiry_ms',),
                        'delivery_sources': ('source_id',), 'delivery_observations': ('expiry_ms',),
                        'evaluation_protocols': ('protocol_hash',), 'history_sources': ('source_id',),
                        'hourly_observations': ('open_ms', 'fetched_at_ms', 'source_id'),
                        'baseline_protocols': ('extension_hash',), 'baseline_predictions': ('baseline_id',),
                        'baseline_scores': ('baseline_id',)}[table]
                match = ' AND '.join(f'{key}=NEW.{key}' for key in keys)
                self.connection.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_INSERT '
                                        f'BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE {match}) '
                                        "BEGIN SELECT RAISE(ABORT, 'I never replace evaluation rows'); END")
            self.connection.execute("PRAGMA user_version=3")
            existing = self.connection.execute(
                "SELECT value FROM settings WHERE name='max_days_to_expiry'").fetchone()
            if existing is not None and int(existing[0]) != max_days_to_expiry:
                raise ValueError("Ingestion horizon differs from this database; use a new --db path")
            self.connection.execute("INSERT OR IGNORE INTO settings VALUES ('max_days_to_expiry', ?)",
                                    (str(max_days_to_expiry),))
            self.connection.commit()
        except (sqlite3.Error, ValueError):
            self.connection.close()
            raise
        self.max_days_to_expiry = max_days_to_expiry

    def close(self) -> None:
        self.connection.close()

    def add_issue(self, source_id: str, fetched_at_ms: int | None, reason: str, detail: str,
                  row_number: int | None = None, instrument_name: str | None = None) -> bool:
        values = (source_id, fetched_at_ms, row_number, instrument_name, reason, detail)
        cursor = self.connection.execute("INSERT OR IGNORE INTO issues VALUES (?, ?, ?, ?, ?, ?, ?)",
                                         (digest(values), *values))
        return cursor.rowcount == 1

    def add_raw(self, row: dict[str, Any], ok: bool, reason: str) -> tuple[str, bool]:
        identity = snapshot_id(row["endpoint"], row["fetched_at_ms"])
        content_hash = digest(row)
        existing = self.connection.execute(
            "SELECT content_hash FROM raw_records WHERE record_id=?", (identity,)).fetchone()
        if existing is not None:
            if existing[0] != content_hash:
                raise ValueError(f"Conflicting raw content for immutable record {identity}")
            return identity, False
        self.connection.execute("INSERT INTO raw_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            identity, row["endpoint"], row["fetched_at_ms"], row["request_sent_ms"],
            row["response_received_ms"], row["latency_ms"], row["http_status"], int(ok), reason, content_hash))
        return identity, True

    def add_instrument(self, instrument: Instrument) -> bool:
        row = asdict(instrument)
        row["tick_size_steps"] = None if instrument.tick_size_steps is None else canonical(
            [{"above_price": threshold, "tick_size": size} for threshold, size in instrument.tick_size_steps])
        row["is_active"] = int(instrument.is_active)
        names = list(row)
        cursor = self.connection.execute(
            f"INSERT OR IGNORE INTO instruments ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            tuple(row[name] for name in names))
        if cursor.rowcount == 0:
            existing = self.connection.execute(
                "SELECT * FROM instruments WHERE instrument_name=? AND fetched_at_ms=?",
                (instrument.instrument_name, instrument.fetched_at_ms)).fetchone()
            if dict(existing) != row:
                raise ValueError(f"Conflicting instrument observation for {instrument.instrument_name}")
        return cursor.rowcount == 1

    def latest_instrument(self, name: str, as_of_ms: int) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT * FROM instruments WHERE instrument_name=? AND fetched_at_ms<=? "
            "ORDER BY fetched_at_ms DESC LIMIT 1", (name, as_of_ms)).fetchone()
        assert row is None or row["fetched_at_ms"] <= as_of_ms
        rejected = self.metadata_rejection(name, as_of_ms)
        if rejected is not None and (row is None or rejected["fetched_at_ms"] >= row["fetched_at_ms"]):
            return None
        return row

    def metadata_rejection(self, name: str, as_of_ms: int) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT i.fetched_at_ms, i.reason FROM issues i JOIN raw_records r ON i.source_id=r.record_id "
            "WHERE r.endpoint='public/get_instruments' AND i.instrument_name=? AND i.fetched_at_ms<=? "
            "ORDER BY i.fetched_at_ms DESC, i.reason LIMIT 1", (name, as_of_ms)).fetchone()
        assert row is None or row["fetched_at_ms"] <= as_of_ms
        return row

    def latest_index(self, as_of_ms: int) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT * FROM index_observations WHERE fetched_at_ms<=? "
            "ORDER BY fetched_at_ms DESC, record_id LIMIT 1", (as_of_ms,)).fetchone()
        assert row is None or row["fetched_at_ms"] <= as_of_ms
        return row

    def quotes_as_of(self, identity: str, as_of_ms: int, *, verified_only: bool = True) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT q.*, s.fetched_at_ms FROM quotes q JOIN snapshots s USING(snapshot_id) "
            "WHERE q.snapshot_id=? AND s.fetched_at_ms<=? AND (?=0 OR q.identity_status='verified') "
            "ORDER BY q.instrument_name", (identity, as_of_ms, int(verified_only))).fetchall()
        assert all(row["fetched_at_ms"] <= as_of_ms for row in rows)
        assert all(row["metadata_fetched_at_ms"] is None or row["metadata_fetched_at_ms"] <= row["fetched_at_ms"]
                   for row in rows)
        assert not verified_only or all(row["identity_status"] == "verified" for row in rows)
        return rows

    def counts(self) -> dict[str, int]:
        tables = ("raw_records", "snapshots", "quotes", "instruments", "index_observations",
                  "heartbeat", "issues", "delivery_prices", "predictions", "scores")
        return {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def evaluation_store(path: Path) -> Store:
    path = Path(path).resolve()
    horizon = 400
    if path.exists():
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            row = connection.execute("SELECT value FROM settings WHERE name='max_days_to_expiry'").fetchone()
            if row is not None:
                horizon = int(row[0])
        finally:
            connection.close()
    return Store(path, horizon)
