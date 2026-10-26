"""Rebuild the declared exploratory v2 comparisons without changing v1 outputs."""

import argparse
import json
from pathlib import Path
import shutil

from implied_calibration.review_report import write_review_report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('results/review-v2'))
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = write_review_report(root, args.output, progress=lambda value: print(json.dumps(value), flush=True))
    if args.publish:
        shutil.copyfile(args.output/'summary.json', root/'report/v2-results.json')
        shutil.copyfile(args.output/'results.md', root/'report/v2-results.md')
        shutil.copyfile(args.output/'review-variance.svg', root/'report/review-variance.svg')
    print(json.dumps({'n': result['benchmarks']['summary']['n'], 'formal_claim_eligible': False}))
