"""Render or check manuscript macros against the offline summary JSON files."""

import argparse
import json
from pathlib import Path

from implied_calibration.paper_numbers import check_numbers, render_numbers


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', type=Path, default=Path('report/incremental-results.json'))
    parser.add_argument('--v2-summary', type=Path, default=Path('report/v2-results.json'))
    parser.add_argument('--source', type=Path, default=Path('report/latex/main.tex'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = args.source.read_text(encoding='utf-8')
    summary = json.loads(args.summary.read_text(encoding='utf-8'))
    v2 = json.loads(args.v2_summary.read_text(encoding='utf-8')) if args.v2_summary.exists() else None
    if args.check:
        count = check_numbers(source, summary, v2)
    else:
        generated, count = render_numbers(source, summary, v2)
        args.source.write_text(generated, encoding='utf-8', newline='\n')
    print(json.dumps({'computed_macros': count, 'status': 'checked' if args.check else 'rendered'}))
