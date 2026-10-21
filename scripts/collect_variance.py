"""Run a bounded supervised prospective variance session."""

import argparse
import json
from pathlib import Path

from implied_calibration.variance_forward import VarianceForwardJob


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('data/variance-forward'))
    parser.add_argument('--seconds',type=int,default=600)
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error('Expected a positive bounded session length')
    job = VarianceForwardJob(Path(__file__).resolve().parents[1],args.output)
    print(json.dumps(job.run(args.seconds),indent=2))
