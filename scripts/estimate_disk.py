"""Estimate compressed daily disk use from observed recording time."""

import argparse
import json
import logging
from pathlib import Path


def estimate(directory: Path) -> dict[str, int | float | None]:
    files = list((directory / "raw").glob("*.jsonl.gz"))
    files += list((directory / "heartbeat").glob("*.jsonl"))
    total = sum(path.stat().st_size for path in files)
    runs: dict[str, tuple[int, int]] = {}
    corrupt = 0
    for path in sorted((directory / "heartbeat").glob("*.jsonl")):
        with path.open(encoding="utf-8", newline="\n") as source:
            for line_number, line in enumerate(source, 1):
                try:
                    row = json.loads(line)
                    run_id, stamp = row["run_id"], row["fetched_at_ms"]
                    if not isinstance(run_id, str) or type(stamp) is not int:
                        raise ValueError("invalid run_id or fetched_at_ms")
                except (ValueError, KeyError, TypeError) as exc:
                    corrupt += 1
                    logging.warning("corrupt heartbeat %s:%s: %s", path, line_number, exc)
                    continue
                start, end = runs.get(run_id, (stamp, stamp))
                runs[run_id] = min(start, stamp), max(end, stamp)
    elapsed_ms = sum(end - start for start, end in runs.values())
    return {"bytes_written": total, "observed_run_ms": elapsed_ms,
            "estimated_bytes_per_day": round(total * 86400000 / elapsed_ms) if elapsed_ms else None,
            "corrupt_heartbeat_lines": corrupt}


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate disk use from observed recording time.")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error("directory does not exist")
    print(json.dumps(estimate(args.directory), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
