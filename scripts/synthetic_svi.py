"""I generate a labelled synthetic smile check without using captured market quotes."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

import numpy as np

from implied_calibration.cli import provenance
from implied_calibration.fitting import svi_settings
from implied_calibration.plotting import smile_figure
from implied_calibration.protocol import load_protocol
from implied_calibration.svi import SVIParams, fit_svi, total_variance


def main() -> int:
    parser = argparse.ArgumentParser(description='I draw an explicitly synthetic SVI example.')
    parser.add_argument('--protocol', type=Path, default=Path('protocol/protocol.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plot', type=Path, required=True)
    args = parser.parse_args()
    protocol = load_protocol(args.protocol)
    settings = svi_settings(protocol)
    true_params = SVIParams(0.02, 0.15, -0.35, 0.03, 0.25)
    k = np.linspace(-1, 1, 41)
    t_years = 0.5
    iv_mid = np.sqrt(total_variance(k, true_params) / t_years)
    bid, ask = iv_mid - 0.01, iv_mid + 0.01
    result = fit_svi(k, bid, ask, iv_mid, t_years, settings)
    figure = smile_figure(k, bid, ask, t_years, result, synthetic=True,
                          label='SVI fit to a noiseless generated smile')
    args.plot.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.plot, format='png', dpi=150, metadata={'Software': 'implied-odds synthetic SVI'})
    figure.clear()
    output = {'synthetic': True, 't_years': t_years, 'k': k.tolist(), 'iv_mid': iv_mid.tolist(),
              'iv_bid': bid.tolist(), 'iv_ask': ask.tolist(), 'true_params': asdict(true_params),
              'fit': asdict(result), 'protocol_hash': protocol.content_hash,
              'protocol_status': protocol.content['status'], 'provenance': provenance()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + '\n',
                            encoding='utf-8', newline='\n')
    print(json.dumps({'synthetic': True, 'status': result.status, 'rmse_vol_pts': result.rmse_vol_pts}))
    return 0 if result.status == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
