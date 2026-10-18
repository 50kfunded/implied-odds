"""I extract CDF diagnostics only from captured smiles that pass the existing fit gate."""

from collections import Counter
from pathlib import Path
from typing import Any

from .density import CDFSettings, ImpliedCDF
from .fitting import fit_database
from .protocol import Protocol
from .svi import SVIParams


def cdf_database(path: Path, protocol: Protocol, as_of_ms: int | None = None,
                 *, snapshot_id: str | None = None) -> dict[str, Any]:
    fitted = fit_database(path, protocol, as_of_ms, snapshot_id=snapshot_id)
    output = []
    for group in fitted['groups']:
        assert group['fetched_at_ms'] <= fitted['as_of_ms']
        row = {**group, 'distribution': None}
        if not group['primary_accepted']:
            row['cdf_status'] = 'blocked_upstream' if not fitted['phase4_ready'] else 'fit_rejected'
            row['cdf_message'] = group['fit']['message']
        else:
            try:
                distribution = ImpliedCDF(SVIParams(**group['fit']['params']), group['forward_usd'],
                                          group['t_years'], min(group['observed_k']), max(group['observed_k']),
                                          CDFSettings(**protocol.content['cdf']))
                row['distribution'] = distribution.to_record()
                row['cdf_status'] = row['distribution']['diagnostics']['status']
                row['cdf_message'] = '; '.join(row['distribution']['diagnostics']['reasons'])
            except ValueError as exc:
                row['cdf_status'], row['cdf_message'] = 'invalid_distribution', str(exc)
        output.append(row)
    count_ok = sum(row['cdf_status'] == 'ok' for row in output)
    return {**fitted, 'groups': output, 'n_cdf_ok': count_ok,
            'n_cdf_constructed': sum(row['distribution'] is not None for row in output),
            'cdf_reason_counts': dict(sorted(Counter(reason for row in output if row['distribution']
                                                   for reason in row['distribution']['diagnostics']['reasons']).items())),
            'cdf_status_counts': dict(sorted(Counter(row['cdf_status'] for row in output).items())),
            'status': 'blocked_upstream' if not fitted['phase4_ready'] else ('ok' if count_ok else 'no_accepted_cdfs')}
