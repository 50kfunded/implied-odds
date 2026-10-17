"""I record, ingest, and inspect my captured BTC option data."""

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sqlite3
import subprocess

from .coverage import coverage_report
from .cleaning import clean_database
from .fitting import fit_database
from .distributions import cdf_database
from .ingest import ingest
from .protocol import load_protocol
from .recorder import record
from .store import Store


def provenance() -> dict[str, str | bool | None]:
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, check=False)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return {"git_commit": None, "working_tree_dirty": None}
    return {"git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "working_tree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None}


def emit(result: dict, output: Path | None) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="I record public BTC option data locally.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    recorder = commands.add_parser("record", help="Record production BTC option responses")
    recorder.add_argument("--output", type=Path, required=True)
    recorder.add_argument("--interval", type=int, default=300)
    recorder.add_argument("--seconds", type=int)
    importer = commands.add_parser("ingest", help="Ingest an unchanged local recording into SQLite")
    importer.add_argument("--input", type=Path, required=True)
    importer.add_argument("--db", type=Path, required=True)
    importer.add_argument("--max-days-to-expiry", type=int, default=400)
    importer.add_argument("--output", type=Path)
    coverage = commands.add_parser("coverage", help="Inspect declared polling windows and quote counts")
    coverage.add_argument("--db", type=Path, required=True)
    coverage.add_argument("--as-of-ms", type=int)
    coverage.add_argument("--output", type=Path)
    cleaner = commands.add_parser("clean", help="I classify captured quotes and check IV conventions")
    cleaner.add_argument("--db", type=Path, required=True)
    cleaner.add_argument("--as-of-ms", type=int)
    cleaner.add_argument("--protocol", type=Path, default=Path("protocol/protocol.json"))
    cleaner.add_argument("--output", type=Path)
    fitter = commands.add_parser("fit", help="I fit cleaned smiles after the IV gate clears")
    fitter.add_argument("--db", type=Path, required=True)
    fitter.add_argument("--as-of-ms", type=int)
    fitter.add_argument("--protocol", type=Path, default=Path("protocol/protocol.json"))
    fitter.add_argument("--output", type=Path)
    cdf = commands.add_parser("cdf", help="I extract risk-neutral distributions from accepted fits")
    cdf.add_argument("--db", type=Path, required=True)
    cdf.add_argument("--as-of-ms", type=int)
    cdf.add_argument("--protocol", type=Path, default=Path("protocol/protocol.json"))
    cdf.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s %(message)s")
    try:
        if args.command == "record":
            summary = record(args.output, args.interval, args.seconds)
            emit(asdict(summary), None)
            return int(summary.cycles == 0 or summary.failed_attempts > 0
                       or summary.skipped_slots > 0 or summary.clock_changes > 0)
        if args.command == "ingest":
            store = Store(args.db, args.max_days_to_expiry)
            try:
                result = ingest(args.input, store)
            finally:
                store.close()
        elif args.command == "coverage":
            result = coverage_report(args.db, args.as_of_ms)
        elif args.command == "clean":
            result = clean_database(args.db, load_protocol(args.protocol), args.as_of_ms)
        elif args.command == "fit":
            result = fit_database(args.db, load_protocol(args.protocol), args.as_of_ms)
        else:
            result = cdf_database(args.db, load_protocol(args.protocol), args.as_of_ms)
        result["provenance"] = provenance()
        emit(result, args.output)
        return 2 if (args.command == "clean" and not result["phase4_ready"]
                     or args.command in ("fit", "cdf") and result["status"] != "ok") else 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "exit_code": 130}))
        return 130
    except (OSError, ValueError, sqlite3.Error) as exc:
        logging.getLogger(__name__).exception("Command stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
