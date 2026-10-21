"""Rebuild the matched historical HAR experiment offline."""

import argparse
import json
from pathlib import Path

from implied_calibration.incremental_report import write_incremental_report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('results/incremental'))
    parser.add_argument('--no-simulations',action='store_true')
    args = parser.parse_args()
    result = write_incremental_report(Path(__file__).resolve().parents[1],args.output,simulations=not args.no_simulations)
    print(json.dumps({'n':result['n'],'floor_hits':result['floor_hits'],'metrics':result['summary']['metrics']},indent=2))
