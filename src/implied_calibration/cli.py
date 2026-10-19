"""I record, ingest, and inspect my captured BTC option data."""

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sqlite3
import subprocess

from .coverage import coverage_report
from .client import ClientError, utc_ms
from .cleaning import clean_database
from .fitting import fit_database
from .distributions import cdf_database
from .ingest import ingest
from .evaluation import score_database
from .locking import load_locked_protocol
from .predictions import freeze_predictions
from .protocol import load_protocol
from .recorder import record
from .store import Store
from .settlement import capture_deliveries, ingest_deliveries
from .baseline_lock import load_locked_baselines
from .baseline_evaluation import infer_database, score_baselines
from .history import capture_history, ingest_history
from .report import build_report, write_report


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
    collector = commands.add_parser('collect', help='I collect inputs, freeze due forecasts and score deliveries')
    collector.add_argument('--output', type=Path, required=True)
    collector.add_argument('--db', type=Path, required=True)
    collector.add_argument('--report-output', type=Path, default=Path('results/live'))
    collector.add_argument('--protocol', type=Path, default=Path('protocol/protocol.json'))
    collector.add_argument('--baselines', type=Path, default=Path('protocol/baselines.json'))
    collector.add_argument('--interval', type=int, default=300)
    collector.add_argument('--seconds', type=int)
    stopper = commands.add_parser('collect-stop', help='I ask the collection job to stop after its current cycle')
    stopper.add_argument('--output', type=Path, required=True)
    historical = commands.add_parser('historical-fetch', help='I retain a separate retrospective DVOL variance dataset')
    historical.add_argument('--output', type=Path, required=True)
    historical.add_argument('--study', type=Path, default=Path('protocol/historical-variance.json'))
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
    for name in ('predict', 'score', 'infer'):
        command = commands.add_parser(name, help=f'I {name} with the committed evaluation protocol')
        command.add_argument('--db', type=Path, required=True)
        command.add_argument('--as-of-ms', type=int)
        command.add_argument('--protocol', type=Path, default=Path('protocol/protocol.json'))
        command.add_argument('--baselines', type=Path, default=Path('protocol/baselines.json'))
        command.add_argument('--output', type=Path)
    settler = commands.add_parser('settle', help='I ingest retained official delivery pages or capture them explicitly')
    settler.add_argument('--db', type=Path, required=True)
    source = settler.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path)
    source.add_argument('--fetch', action='store_true')
    settler.add_argument('--raw-output', type=Path)
    settler.add_argument('--as-of-ms', type=int)
    settler.add_argument('--output', type=Path)
    historian = commands.add_parser('history', help='I ingest retained hourly candles or capture them explicitly')
    historian.add_argument('--db', type=Path, required=True)
    history_source = historian.add_mutually_exclusive_group(required=True)
    history_source.add_argument('--input', type=Path)
    history_source.add_argument('--fetch', action='store_true')
    historian.add_argument('--raw-output', type=Path)
    historian.add_argument('--start-ms', type=int)
    historian.add_argument('--end-ms', type=int)
    historian.add_argument('--as-of-ms', type=int)
    historian.add_argument('--output', type=Path)
    reporter = commands.add_parser('report',help='I write figures and an offline report from immutable research records')
    reporter.add_argument('--db',type=Path,required=True)
    reporter.add_argument('--as-of-ms',type=int)
    reporter.add_argument('--protocol',type=Path,default=Path('protocol/protocol.json'))
    reporter.add_argument('--baselines',type=Path,default=Path('protocol/baselines.json'))
    reporter.add_argument('--output',type=Path,required=True)
    snapshot = commands.add_parser('snapshot-report', help='I inspect a real snapshot without freezing forecasts')
    snapshot.add_argument('--db', type=Path, required=True)
    snapshot.add_argument('--protocol', type=Path, default=Path('protocol/protocol.json'))
    snapshot.add_argument('--as-of-ms', type=int)
    snapshot.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s %(message)s")
    try:
        if args.command == 'historical-fetch':
            from .historical_data import capture_study
            emit(capture_study(args.output, args.study), None)
            return 0
        if args.command in ('collect', 'collect-stop'):
            from .collection import CollectionConfig, collect, request_stop
            if args.command == 'collect-stop':
                result = request_stop(args.output)
            else:
                result = collect(CollectionConfig(args.output, args.db, args.report_output,
                                                  args.protocol, args.baselines, args.interval, args.seconds))
            emit(result, None)
            return int(args.command == 'collect' and (result['cycles'] == 0 or result['failed_cycles'] > 0
                       or result['failed_attempts'] > 0 or result['skipped_slots'] > 0 or result['clock_changes'] > 0))
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
        elif args.command == 'cdf':
            result = cdf_database(args.db, load_protocol(args.protocol), args.as_of_ms)
        elif args.command == 'snapshot-report':
            from .snapshot_report import snapshot_report
            details = provenance()
            result = snapshot_report(args.db, load_protocol(args.protocol), args.output,
                                     details['git_commit'], args.as_of_ms, working_tree_dirty=details['working_tree_dirty'])
            emit({k: v for k, v in result.items() if k != 'groups'}, None)
            return 0 if result['n_accepted_cdfs'] else 2
        elif args.command == 'report':
            details = provenance()
            parent = load_locked_protocol(args.protocol)
            extension = load_locked_baselines(args.baselines,parent)
            data = build_report(args.db,parent,extension,args.as_of_ms,details['git_commit'])
            data['provenance']['working_tree_dirty'] = details['working_tree_dirty']
            result = write_report(data,args.output)
            emit(result,None)
            return 0 if result['status'] == 'ok' else 2
        elif args.command in ('predict', 'score', 'infer'):
            details = provenance()
            if details['git_commit'] is None or details['working_tree_dirty'] is not False:
                raise ValueError('I need a clean Git checkout to freeze or score research forecasts')
            now = utc_ms()
            locked = load_locked_protocol(args.protocol)
            extension = load_locked_baselines(args.baselines, locked)
            cutoff = now if args.as_of_ms is None else args.as_of_ms
            if cutoff > now:
                raise ValueError('I cannot use a future as-of time')
            if args.command == 'infer':
                result = infer_database(args.db, extension, locked, cutoff)
            elif args.command == 'predict':
                result = freeze_predictions(args.db, locked, cutoff, now, details['git_commit'],
                                            now_ms=utc_ms, baseline_extension=extension)
            else:
                result = score_database(args.db, locked, cutoff, now, details['git_commit'])
                result['baselines'] = score_baselines(args.db, extension, locked, cutoff, now, details['git_commit'],
                    parent_preview=result['scores'] if cutoff < now else None)
        elif args.command == 'history':
            if args.fetch:
                if args.raw_output is None or args.as_of_ms is not None or args.start_ms is None or args.end_ms is None:
                    raise ValueError('Live history needs --raw-output, --start-ms and --end-ms with actual capture time')
                if args.end_ms > utc_ms():
                    raise ValueError('I only request completed history intervals')
                captured = capture_history(args.raw_output, args.start_ms, args.end_ms)
                result = ingest_history(args.raw_output, args.db, utc_ms())
                result['capture'] = captured
            else:
                if args.start_ms is not None or args.end_ms is not None or args.raw_output is not None:
                    raise ValueError('Offline history uses only the retained request interval')
                result = ingest_history(args.input, args.db, utc_ms() if args.as_of_ms is None else args.as_of_ms)
        else:
            if args.fetch:
                if args.raw_output is None or args.as_of_ms is not None:
                    raise ValueError('Live settlement capture needs --raw-output and uses the actual current time')
                captured = capture_deliveries(args.raw_output)
                result = ingest_deliveries(args.raw_output, args.db, utc_ms())
                result['capture'] = captured
            else:
                result = ingest_deliveries(args.input, args.db, utc_ms() if args.as_of_ms is None else args.as_of_ms)
        result["provenance"] = provenance()
        emit(result, args.output)
        return 2 if (args.command == "clean" and not result["phase4_ready"]
                     or args.command in ("fit", "cdf", "predict", "score", "infer") and result["status"] != "ok") else 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "exit_code": 130}))
        return 130
    except (OSError, ValueError, sqlite3.Error, ClientError) as exc:
        logging.getLogger(__name__).exception("Command stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
