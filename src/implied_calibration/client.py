"""I retain each public REST attempt before deciding whether to retry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
import math
import random
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
    else:
        raise ClientError("unsupported_endpoint", f"No Phase 1 schema for {endpoint}")
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
            "public/get_book_summary_by_currency", "public/get_index_price", "public/get_instruments"
        ):
            raise ValueError(f"Unsupported Phase 1 endpoint: {endpoint}")
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
