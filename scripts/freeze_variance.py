"""Run a bounded public-state operation; activation stays explicit in the workflow."""

import argparse
import json
from pathlib import Path

from implied_calibration.public_variance import (bundle_manifest, check_liveness, check_staged_state, current_bundle, export_failure, freeze_once, observation, preflight,
                                                refresh_and_score, retain_failure, retain_server_receipt, score_existing, transfer_state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preflight', 'freeze', 'restore', 'export', 'bundle', 'receipt', 'score', 'refresh-score', 'failure', 'observe', 'manifest', 'verify-staged', 'watchdog'])
    parser.add_argument('--directory', type=Path, default=Path('data/variance-forward'))
    parser.add_argument('--state', type=Path, default=Path('../public-state'))
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--run-id', type=int, default=0)
    parser.add_argument('--attempt', type=int, default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.mode == 'preflight':
        result = preflight(Path('results/runner-preflight'))
    elif args.mode == 'freeze':
        result = freeze_once(root, args.directory)
    elif args.mode == 'restore':
        result = transfer_state(args.state, args.directory)
    elif args.mode == 'export':
        result = transfer_state(args.directory, args.state)
    elif args.mode == 'bundle':
        result = current_bundle(root, args.directory, args.state)
    elif args.mode == 'receipt':
        if args.metadata is None:
            parser.error('receipt requires --metadata')
        result = retain_server_receipt(args.directory, args.metadata, args.run_id, args.attempt)
    elif args.mode == 'observe':
        result = observation(root, args.directory, args.run_id, args.attempt)
    elif args.mode == 'score':
        result = score_existing(root, args.directory)
    elif args.mode == 'refresh-score':
        result = refresh_and_score(root, args.directory)
    elif args.mode == 'failure':
        retain_failure(args.directory, args.run_id, args.attempt)
        result = export_failure(args.directory, args.state)
    elif args.mode == 'verify-staged':
        result = check_staged_state(args.state)
    elif args.mode == 'watchdog':
        result = check_liveness(args.state, require_latest_forecast=True)
    else:
        result = bundle_manifest(args.directory)
    print(json.dumps(result, indent=2, sort_keys=True))
