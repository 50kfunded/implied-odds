"""I draw a labelled synthetic CDF without using or fitting captured quotes."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from implied_calibration.cli import provenance
from implied_calibration.density import CDFSettings, ImpliedCDF
from implied_calibration.plotting import cdf_figure
from implied_calibration.protocol import load_protocol
from implied_calibration.svi import SVIParams


def main() -> int:
    parser = argparse.ArgumentParser(description='I check an explicitly synthetic risk-neutral CDF.')
    parser.add_argument('--protocol', type=Path, default=Path('protocol/protocol.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plot', type=Path, required=True)
    args = parser.parse_args()
    protocol = load_protocol(args.protocol)
    model = ImpliedCDF(SVIParams(0.02, 0.15, -0.35, 0.03, 0.25), 80000.0, 0.5, -0.3, 0.3,
                       CDFSettings(**protocol.content['cdf']))
    figure = cdf_figure(model, synthetic=True, label='CDF from a generated SVI slice')
    args.plot.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.plot, format='png', dpi=150, metadata={'Software': 'implied-odds synthetic CDF'})
    figure.clear()
    result = {'synthetic': True, 'distribution': model.to_record(),
              'queries': [{'price_usd': price, **asdict(model.cdf_at(price))}
                          for price in (40000.0, 80000.0, 120000.0)],
              'protocol_hash': protocol.content_hash, 'protocol_status': protocol.content['status'],
              'provenance': provenance()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n',
                           encoding='utf-8', newline='\n')
    print(json.dumps({'synthetic': True, **model.to_record()['diagnostics']}, allow_nan=False))
    return 0 if model.to_record()['diagnostics']['status'] == 'ok' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
