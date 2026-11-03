"""Capture the fixed BTC and ETH source intervals into a fresh local directory."""

import argparse
import json
from pathlib import Path
import time

from implied_calibration.historical_data import capture_study


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/relative-variance-2026-10-27'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for index, asset in enumerate(('BTC', 'ETH')):
        if index:
            time.sleep(10.)
        manifest = capture_study(args.output/asset.lower(), root/f'protocol/relative-variance-{asset.lower()}-source.json')
        print(json.dumps({'asset': asset, 'attempts': manifest['capture_attempts'],
                          'failed_attempts': manifest['failed_attempts']}), flush=True)
