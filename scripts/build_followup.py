"""Rebuild the integrated research follow-up, including its failed hypothesis."""

import argparse
from pathlib import Path

from implied_calibration.frequency_audit import write_frequency_report
from implied_calibration.progressive_report import write_progressive_report
from implied_calibration.qlike_attribution import write_attribution
from implied_calibration.relative_report import write_relative_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('results/followup'))
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    write_relative_report(root, args.output/'relative', publish=args.publish)
    forecasts = args.output/'relative/forecasts.csv'
    write_attribution(root, forecasts, args.output/'attribution', check=args.check, publish=args.publish)
    write_frequency_report(root, forecasts, args.output/'frequency', check=args.check, publish=args.publish)
    write_progressive_report(root, args.output/'progressive', check=args.check, publish=args.publish)
    print('Rebuilt matched BTC/ETH forecasts, QLIKE attribution, frequency refits and partial-settlement checks')


if __name__ == '__main__':
    main()
