"""Reproduce exact checks and declared partial-settlement simulations offline."""

import argparse
from pathlib import Path

from implied_calibration.progressive_report import write_progressive_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('results/progressive'))
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    result = write_progressive_report(Path(__file__).resolve().parents[1], args.output,
                                      check=args.check, publish=args.publish)
    print(f"Verified {len(result['exact_checks'])} complete trees and {len(result['simulations'])} simulation scenarios")


if __name__ == '__main__':
    main()
