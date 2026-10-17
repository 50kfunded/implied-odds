"""I append complete gzip members and leave damaged raw files untouched."""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterator
import zlib


LOGGER = logging.getLogger(__name__)


class TruncatedTail(OSError):
    pass


def day_name(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def _members(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        pending = source.read(65536)
        while pending:
            decoder = zlib.decompressobj(wbits=31)
            pieces: list[bytes] = []
            while True:
                try:
                    pieces.append(decoder.decompress(pending))
                except zlib.error as exc:
                    raise OSError(f"Corrupt gzip member in {path}: {exc}") from exc
                if decoder.eof:
                    yield b"".join(pieces)
                    pending = decoder.unused_data or source.read(65536)
                    break
                pending = source.read(65536)
                if not pending:
                    raise TruncatedTail(f"Truncated final gzip member in {path}")


def read_raw(path: Path, *, on_issue: Callable[[dict[str, Any]], None] | None = None) -> Iterator[dict[str, Any]]:
    member_number = 0
    source_prefix = None
    try:
        for member_number, member in enumerate(_members(Path(path)), 1):
            if source_prefix is None:
                source_prefix = hashlib.sha256(member).hexdigest()
            for line_number, line in enumerate(member.splitlines(), 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("expected a JSON object")
                except (ValueError, UnicodeDecodeError) as exc:
                    LOGGER.warning("corrupt_json_line path=%s reason=%s", path, exc)
                    if on_issue is not None:
                        on_issue({"reason": "corrupt_json_line", "member": member_number,
                                  "line": line_number, "detail": str(exc), "source_prefix": source_prefix,
                                  "line_hash": hashlib.sha256(line).hexdigest()})
                    continue
                yield record
    except TruncatedTail as exc:
        LOGGER.warning("truncated_tail reason=%s; I discarded the incomplete member", exc)
        if on_issue is not None:
            on_issue({"reason": "truncated_tail", "member": member_number + 1,
                      "line": None, "detail": "I discarded the incomplete final gzip member",
                      "source_prefix": source_prefix or hashlib.sha256(Path(path).read_bytes()).hexdigest()})


class RawWriter:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.checked: set[Path] = set()

    def preflight(self, timestamp_ms: int) -> Path:
        path = self.directory / f"{day_name(timestamp_ms)}.jsonl.gz"
        if path not in self.checked:
            if path.exists():
                try:
                    for _ in _members(path):
                        pass
                except OSError as exc:
                    raise OSError(f"I won't append to {path}; use a fresh --output directory: {exc}") from exc
            self.checked.add(path)
        return path

    def append(self, record: dict[str, Any]) -> None:
        path = self.preflight(record["fetched_at_ms"])
        line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
        member = gzip.compress(line.encode("utf-8"), mtime=0)
        with path.open("ab") as target:
            target.write(member)
            target.flush()
