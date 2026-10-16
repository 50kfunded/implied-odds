"""I expose the Phase 1 recorder through python -m implied_calibration.cli."""

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path

from .recorder import record


def main() -> int:
    parser = argparse.ArgumentParser(description="I record public BTC option data locally.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    recorder = commands.add_parser("record", help="Record production BTC option responses")
    recorder.add_argument("--output", type=Path, required=True)
    recorder.add_argument("--interval", type=int, default=300)
    recorder.add_argument("--seconds", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s %(message)s")
    try:
        summary = record(args.output, args.interval, args.seconds)
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "exit_code": 130}))
        return 130
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).exception("Recording stopped: %s", exc)
        return 1
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    return int(summary.cycles == 0 or summary.failed_attempts > 0
               or summary.skipped_slots > 0 or summary.clock_changes > 0)


if __name__ == "__main__":
    raise SystemExit(main())
