"""Inverse BTC identities are parsed and checked against instrument metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any


class InstrumentError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class InstrumentName:
    instrument_name: str
    expiry_ms: int
    strike: float
    option_type: str


@dataclass(frozen=True)
class Instrument:
    instrument_name: str
    fetched_at_ms: int
    expiry_ms: int
    strike: float
    option_type: str
    tick_size: float
    tick_size_steps: tuple[tuple[float, float], ...] | None
    contract_size: float
    is_active: bool


def parse_name(name: str) -> InstrumentName:
    if not isinstance(name, str):
        raise InstrumentError("invalid_instrument_name", "instrument_name must be a string")
    match = re.fullmatch(r"BTC-([0-9]{1,2})([A-Z]{3})([0-9]{2})-([0-9]+)-([CP])", name)
    if match is None:
        raise InstrumentError("invalid_instrument_name", f"Expected an inverse BTC option name: {name!r}")
    day, month, year, strike_text, side = match.groups()
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    try:
        expiry = datetime(2000 + int(year), months[month], int(day), 8, tzinfo=timezone.utc)
        strike = float(strike_text)
        if not math.isfinite(strike) or strike <= 0:
            raise ValueError("strike must be finite and positive")
    except (ValueError, KeyError, OverflowError) as exc:
        raise InstrumentError("invalid_instrument_identity", f"Invalid expiry or strike in {name!r}: {exc}") from exc
    return InstrumentName(name, int(expiry.timestamp() * 1000), strike, "call" if side == "C" else "put")


def finite_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_metadata(row: dict[str, Any], fetched_at_ms: int) -> Instrument:
    """Validate metadata in one place and reject identity conflicts explicitly."""
    required = {"instrument_name", "expiration_timestamp", "strike", "option_type",
                "tick_size", "contract_size", "is_active"}
    missing = sorted(required - row.keys())
    if missing:
        raise InstrumentError("metadata_schema_error", f"metadata: missing keys={missing}; unexpected keys=[]")
    if not isinstance(row["instrument_name"], str):
        raise InstrumentError("metadata_schema_error", "instrument_name must be a string")
    parsed = parse_name(row["instrument_name"])
    numeric = ("strike", "tick_size", "contract_size")
    for field in numeric:
        if not finite_number(row[field]) or row[field] <= 0:
            raise InstrumentError("metadata_schema_error", f"{field} must be finite and positive")
    if type(row["expiration_timestamp"]) is not int or type(row["is_active"]) is not bool:
        raise InstrumentError("metadata_schema_error", "expiry must be integer milliseconds and is_active boolean")
    conflicts = [field for field, actual, expected in (
        ("strike", row["strike"], parsed.strike),
        ("expiration_timestamp", row["expiration_timestamp"], parsed.expiry_ms),
        ("option_type", row["option_type"], parsed.option_type),
    ) if actual != expected]
    if conflicts:
        raise InstrumentError("metadata_identity_mismatch", f"Name and metadata disagree: {conflicts}")
    for field, expected in (("base_currency", "BTC"), ("settlement_currency", "BTC"),
                            ("kind", "option"), ("instrument_type", "reversed")):
        if field in row and row[field] != expected:
            raise InstrumentError("unsupported_contract", f"{field} must be {expected!r}")
    steps = row.get("tick_size_steps")
    if steps is not None:
        if not isinstance(steps, list):
            raise InstrumentError("metadata_schema_error", "tick_size_steps must be an array or null")
        checked = []
        for step in steps:
            if not isinstance(step, dict) or not {"above_price", "tick_size"} <= step.keys():
                raise InstrumentError("metadata_schema_error", "tick step: missing keys=['above_price', 'tick_size']")
            for field in ("above_price", "tick_size"):
                value = step[field]
                if not finite_number(value) or value < 0:
                    raise InstrumentError("metadata_schema_error", f"tick step {field} must be finite and nonnegative")
            if step["tick_size"] == 0:
                raise InstrumentError("metadata_schema_error", "tick step tick_size must be positive")
            checked.append((float(step["above_price"]), float(step["tick_size"])))
        if any(right[0] <= left[0] for left, right in zip(checked, checked[1:])):
            raise InstrumentError("metadata_schema_error", "tick step thresholds must increase")
        steps = tuple(checked)
    return Instrument(parsed.instrument_name, fetched_at_ms, row["expiration_timestamp"],
                      float(row["strike"]), row["option_type"], float(row["tick_size"]),
                      steps, float(row["contract_size"]), row["is_active"])
