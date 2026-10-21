"""Rebuild the quantitative research results from retained public inputs."""

import argparse
import json
from pathlib import Path
import shutil

from implied_calibration.incremental_report import write_incremental_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('results/research'))
    parser.add_argument('--publish', action='store_true',
                        help='Refresh the checked-in results and figure; compile the paper separately.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = write_incremental_report(root, args.output)
    if args.publish:
        shutil.copyfile(args.output/'incremental-variance.svg', root/'report/incremental-variance.svg')
        shutil.copyfile(args.output/'summary.json', root/'report/incremental-results.json')
    print(json.dumps({'n': result['n'], 'summary': str(args.output/'summary.json'),
                      'formal_claim_eligible': False}))


if __name__ == '__main__':
    main()
