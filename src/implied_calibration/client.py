"""I retain each public REST attempt before deciding whether to retry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import logging
import math
import random
import re
import time
from typing import Any, Callable

import requests


BASE_URL = "https://www.deribit.com/api/v2"
LOGGER = logging.getLogger(__name__)


def utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class ClientError(RuntimeError):
    def __init__(self, reason: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class AttemptRecord:
    fetched_at_ms: int
    request_sent_ms: int
    response_received_ms: int
    latency_ms: int
    endpoint: str
    params: dict[str, Any]
    http_status: int | None
    body: Any
    attempt: int
    ok: bool
    reason: str
    detail: str
    response_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApiResponse:
    result: Any
    metadata: AttemptRecord


def validate_response(endpoint: str, status: int, body: Any) -> Any:
    """I keep all response-schema checks here, without changing the body."""
    if isinstance(body, dict) and "testnet" in body and body["testnet"] is not False:
        raise ClientError("testnet_rejected", "testnet must be the boolean false")
    if status != 200:
        raise ClientError("http_error", f"HTTP {status}", status == 429 or 500 <= status <= 599)
    if not isinstance(body, dict):
        raise ClientError("schema_error", "envelope: expected object; missing keys=[result, testnet]")
    required = {"jsonrpc", "testnet", "usIn", "usOut", "usDiff"}
    required.add("error" if "error" in body else "result")
    allowed = {"jsonrpc", "id", "result", "error", "testnet", "usIn", "usOut", "usDiff"}
    missing, unexpected = sorted(required - body.keys()), sorted(body.keys() - allowed)
    if missing or unexpected:
        raise ClientError("schema_error", f"envelope: missing keys={missing}; unexpected keys={unexpected}")
    if body["jsonrpc"] != "2.0":
        raise ClientError("schema_error", "jsonrpc: expected '2.0'")
    for field in ("usIn", "usOut", "usDiff"):
        if type(body[field]) is not int or body[field] < 0:
            raise ClientError("schema_error", f"{field}: expected nonnegative integer microseconds")
    if "error" in body:
        raise ClientError("rpc_error", f"JSON-RPC error: {body['error']!r}", True)
    result = body["result"]
    if endpoint in ("public/get_instruments", "public/get_book_summary_by_currency"):
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ClientError("schema_error", "result: expected array of objects")
    elif endpoint == "public/get_index_price":
        if not isinstance(result, dict) or "index_price" not in result:
            raise ClientError("schema_error", "result: missing keys=['index_price']")
        value = result["index_price"]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ClientError("schema_error", "index_price: expected finite positive number")
    elif endpoint == 'public/get_tradingview_chart_data':
        arrays = {'ticks', 'open', 'high', 'low', 'close', 'volume', 'cost'}
        actual = set(result) if isinstance(result, dict) else set()
        if (not isinstance(result, dict) or result.get('status') not in ('ok', 'no_data')
                or actual - (arrays | {'status'})
                or result['status'] == 'ok' and actual != arrays | {'status'}):
            expected = arrays | {'status'}
            raise ClientError('schema_error', f'Hourly candle result: missing keys={sorted(expected-actual)}; '
                              f'unexpected keys={sorted(actual-expected)}; status must be ok or no_data')
        lengths = []
        for name in arrays & actual:
            values = result[name]
            if (not isinstance(values, list) or any(type(v) not in (int, float) or not math.isfinite(v)
                    or v < 0 or name in ('open', 'high', 'low', 'close') and v <= 0
                    or name == 'ticks' and (type(v) is not int or v % 3600000) for v in values)):
                raise ClientError('schema_error', f'Invalid hourly candle array: {name}')
            lengths.append(len(values))
        if (len(set(lengths)) > 1 or result['status'] == 'no_data' and any(lengths)
                or result['status'] == 'ok' and (not lengths or not lengths[0])
                or any(b <= a for a, b in zip(result.get('ticks', []), result.get('ticks', [])[1:]))):
            raise ClientError('schema_error', 'Hourly candle lengths, order or no_data differ')
        if result['status'] == 'ok' and any(lo > min(o, c) or hi < max(o, c) or lo > hi for o, hi, lo, c
                in zip(result['open'], result['high'], result['low'], result['close'])):
            raise ClientError('schema_error', 'Hourly OHLC values are inconsistent')
    elif endpoint == 'public/get_volatility_index_data':
        fields = {'data', 'continuation'}
        actual = set(result) if isinstance(result, dict) else set()
        if not isinstance(result, dict) or actual != fields:
            raise ClientError('schema_error', f'Volatility candles: missing keys={sorted(fields-actual)}; '
                              f'unexpected keys={sorted(actual-fields)}')
        continuation = result['continuation']
        if continuation is not None and (type(continuation) is not int or continuation < 0):
            raise ClientError('schema_error', 'Volatility continuation must be null or UTC milliseconds')
        if not isinstance(result['data'], list):
            raise ClientError('schema_error', 'Volatility data must be an array of OHLC arrays')
        previous = -1
        for row in result['data']:
            if (not isinstance(row, list) or len(row) != 5 or type(row[0]) is not int or row[0] < 0
                    or row[0] <= previous or any(type(v) not in (int, float) or not math.isfinite(v)
                                               or v <= 0 for v in row[1:])
                    or row[3] > min(row[1], row[4]) or row[2] < max(row[1], row[4])):
                raise ClientError('schema_error', 'Invalid volatility OHLC row or timestamp order')
            previous = row[0]
    elif endpoint == 'public/get_delivery_prices':
        fields = {'data', 'records_total'}
        if not isinstance(result, dict) or set(result) != fields:
            actual = set(result) if isinstance(result, dict) else set()
            raise ClientError('schema_error', f'delivery result: missing keys={sorted(fields-actual)}; unexpected keys={sorted(actual-fields)}')
        if (type(result['records_total']) is not int or result['records_total'] < 0
                or not isinstance(result['data'], list) or len(result['data']) > result['records_total']):
            raise ClientError('schema_error', 'delivery result needs nonnegative records_total and data array')
        for row in result['data']:
            fields = {'date', 'delivery_price'}
            if not isinstance(row, dict) or set(row) != fields:
                actual = set(row) if isinstance(row, dict) else set()
                raise ClientError('schema_error', f'delivery row: missing keys={sorted(fields-actual)}; unexpected keys={sorted(actual-fields)}')
            try:
                if not isinstance(row['date'], str) or not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}', row['date']):
                    raise ValueError('I need YYYY-MM-DD')
                date.fromisoformat(row['date'])
                value = row['delivery_price']
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError('delivery_price must be finite and positive')
            except (ValueError, OverflowError) as exc:
                raise ClientError('schema_error', f'Invalid delivery row: {exc}') from exc
    else:
        raise ClientError("unsupported_endpoint", f"No schema for {endpoint}")
    return result


def reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric constant: {value}")


class PublicClient:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        on_attempt: Callable[[AttemptRecord], None] | None = None,
        max_retries: int = 3,
        now_ms: Callable[[], int] = utc_ms,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be an integer from 0 to 10")
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": "implied-odds/0.1.0 (public BTC research recorder)"})
        self.on_attempt = on_attempt
        self.max_retries = max_retries
        self.now_ms, self.monotonic, self.sleep = now_ms, monotonic, sleep
        self.rng = rng if rng is not None else random.Random(0)

    def close(self) -> None:
        self.session.close()

    def request(self, endpoint: str, params: dict[str, Any]) -> ApiResponse:
        if endpoint not in (
            "public/get_book_summary_by_currency", "public/get_index_price", "public/get_instruments",
            "public/get_delivery_prices", "public/get_tradingview_chart_data", "public/get_volatility_index_data"
        ):
            raise ValueError(f"Unsupported endpoint: {endpoint}")
        for attempt in range(1, self.max_retries + 2):
            sent, started = self.now_ms(), self.monotonic()
            body, status, text, error = None, None, None, None
            response = None
            interrupted = False
            try:
                response = self.session.get(
                    f"{BASE_URL}/{endpoint}", params=params,
                    timeout=(5, 20), allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                error = ClientError("network_error", f"{type(exc).__name__}: {exc}", True)
            except requests.RequestException as exc:
                error = ClientError("request_error", f"{type(exc).__name__}: {exc}")
            except KeyboardInterrupt:
                error = ClientError("interrupted", "Request interrupted before a response")
                interrupted = True
            received = self.now_ms()
            latency = max(0, round((self.monotonic() - started) * 1000))
            if response is not None:
                status = response.status_code
                try:
                    body = response.json(parse_constant=reject_constant)
                except ValueError as exc:
                    text = response.text
                    error = ClientError("invalid_json", str(exc))
                try:
                    result = validate_response(endpoint, status, body)
                except ClientError as exc:
                    if error is None or status != 200:
                        error = exc
            record = AttemptRecord(
                received, sent, received, latency,
                endpoint, dict(params), status, body, attempt, error is None,
                "ok" if error is None else error.reason,
                "" if error is None else str(error), text,
            )
            # I let storage errors escape here so they never trigger network retries.
            if self.on_attempt is not None:
                self.on_attempt(record)
            if interrupted:
                raise KeyboardInterrupt
            if error is None:
                return ApiResponse(result, record)
            LOGGER.warning("%s attempt=%s reason=%s detail=%s", endpoint, attempt, error.reason, error)
            if not error.retryable or attempt > self.max_retries:
                raise error
            self.sleep(min(30.0, 2.0 ** (attempt - 1)) + self.rng.uniform(0.0, 0.5))
        raise AssertionError("Retry loop ended without a result")
