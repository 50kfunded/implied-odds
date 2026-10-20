"""I publish public counts and hashes without copying private collector files."""

import argparse
import json
from pathlib import Path

from implied_calibration.baseline_lock import load_locked_baselines
from implied_calibration.client import utc_ms
from implied_calibration.locking import load_locked_protocol
from implied_calibration.operational_status import build_scoreboard, write_scoreboard
from implied_calibration.research_plan import load_plan
from implied_calibration.variance_forward import forward_summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=Path('data/live.sqlite'))
    parser.add_argument('--status',type=Path,default=Path('data/live/status.json'))
    parser.add_argument('--variance',type=Path,default=Path('data/variance-forward'))
    parser.add_argument('--output',type=Path,default=Path('report'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.db.is_file() or not args.status.is_file():
        parser.error('I need an existing collector database and status file')
    parent = load_locked_protocol(root/'protocol/protocol.json')
    baseline = load_locked_baselines(root/'protocol/baselines.json',parent)
    now = utc_ms()
    state = json.loads(args.status.read_text(encoding='utf-8'))
    data = build_scoreboard(args.db,parent.protocol.content_hash,baseline.content_hash,state,now)
    write_scoreboard(data,args.output)
    plan = load_plan(root/'protocol/incremental-variance-v1.json')
    guard = load_plan(root/'protocol/inference-validation-v1.json')
    variance = forward_summary(args.variance,plan,now,guard)
    (args.output/'variance-scoreboard.json').write_text(json.dumps(variance,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps({'svi':data['forecasts'],'variance_frozen':variance['n_frozen'],'liveness':data['liveness']['status']}))
