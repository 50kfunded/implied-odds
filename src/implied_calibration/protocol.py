"""I load draft cleaning choices and hash their exact canonical content."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from .store import digest


@dataclass(frozen=True)
class Protocol:
    content: dict[str, Any]
    content_hash: str


def validate_protocol(content: dict[str, Any]) -> Protocol:
    sections = {
        'cleaning': {'minimum_maturity_hours', 'minimum_mid_ticks', 'maximum_spread_to_mid',
                     'minimum_unique_strikes', 'minimum_strikes_each_side',
                     'maximum_forward_relative_deviation'},
        'inversion': {'lower_iv', 'upper_iv', 'absolute_tolerance', 'relative_tolerance',
                      'max_iterations', 'max_relative_price_error'},
        'diagnostics': {'systematic_difference_vol_points', 'minimum_mark_comparisons'},
    }
    expected = {'version', 'status', *sections}
    if not isinstance(content, dict) or set(content) != expected:
        actual = set(content) if isinstance(content, dict) else set()
        raise ValueError(f'protocol: missing keys={sorted(expected - actual)}; unexpected keys={sorted(actual - expected)}')
    if type(content['version']) is not int or content['version'] != 1 or content['status'] != 'draft':
        raise ValueError('I only accept version 1 draft cleaning protocols in Phase 3')
    integer_fields = {'minimum_unique_strikes', 'minimum_strikes_each_side', 'max_iterations',
                      'minimum_mark_comparisons'}
    for section, fields in sections.items():
        values = content[section]
        if not isinstance(values, dict) or set(values) != fields:
            actual = set(values) if isinstance(values, dict) else set()
            raise ValueError(f'{section}: missing keys={sorted(fields - actual)}; unexpected keys={sorted(actual - fields)}')
        for field, value in values.items():
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if (not finite or value <= 0
                    or field in integer_fields and type(value) is not int):
                raise ValueError(f'{field} must be finite and positive with the declared numeric type')
    inv, clean = content['inversion'], content['cleaning']
    if inv['lower_iv'] >= inv['upper_iv'] or inv['relative_tolerance'] < 4 * math.ulp(1.0):
        raise ValueError('Invalid IV bracket or Brent relative tolerance')
    if clean['minimum_unique_strikes'] < 2 * clean['minimum_strikes_each_side']:
        raise ValueError('minimum_unique_strikes must cover both strike sides')
    return Protocol(json.loads(json.dumps(content)), digest(content))


def load_protocol(path: Path) -> Protocol:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f'Duplicate protocol key: {name}')
            result[name] = value
        return result

    return validate_protocol(json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique))
