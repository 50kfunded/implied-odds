"""Count accepted frozen forecasts, permanent omissions and local liveness."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from implied_calibration.client import utc_ms
from implied_calibration.research_plan import load_plan
from implied_calibration.variance_forward import forward_summary


def audit(root: Path, directory: Path, now: int, svi_db: Path | None = None) -> dict:
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    guard = load_plan(root/'protocol/inference-validation-v1.json')
    report = forward_summary(directory, plan, now, guard)
    status_path = directory/'status.json'
    status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else None
    report['local_status'] = status
    report['status_age_seconds'] = max(0, (now-status['updated_at_ms'])/1000) if status is not None else None
    report['missing_origins_utc'] = [datetime.fromtimestamp(value/1000, timezone.utc).isoformat()
                                     for value in report['missing_origins']]
    report['v1_terminal_scope'] = 'The original terminal aggregate requires all 365 pairs; individual evidence remains usable and formal inference stays disabled.'
    if svi_db is not None:
        with sqlite3.connect(svi_db.resolve().as_uri()+'?mode=ro', uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            report['svi_counts'] = {name: connection.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                                    for name in ('snapshots', 'predictions', 'baseline_predictions', 'scores') if name in tables}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/variance-forward'))
    parser.add_argument('--as-of', help='ISO timestamp with an explicit UTC offset')
    parser.add_argument('--svi-db', type=Path)
    args = parser.parse_args()
    now = utc_ms()
    if args.as_of:
        date = datetime.fromisoformat(args.as_of.replace('Z', '+00:00'))
        if date.tzinfo is None:
            parser.error('--as-of requires a UTC offset')
        now = int(date.timestamp()*1000)
    print(json.dumps(audit(Path(__file__).resolve().parents[1], args.directory, now, args.svi_db), indent=2))
