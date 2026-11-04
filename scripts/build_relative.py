"""Rebuild the declared ETH/BTC extension offline from retained source responses."""

import argparse
import json
from pathlib import Path

from implied_calibration.relative_report import write_relative_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('results/eth-relative'))
    parser.add_argument('--input-root', type=Path, help='Use a separate retained BTC/ETH capture directory.')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    result = write_relative_report(Path(__file__).resolve().parents[1], args.output,
                                   publish=args.publish, input_root=args.input_root)
    print(json.dumps({'n': result['n'], 'data_checks_passed': result['data_checks_passed'],
                      'formal_claim_eligible': result['formal_claim_eligible']}))


if __name__ == '__main__':
    main()
