"""Research choices are validated and hashed in their exact canonical form."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from .density import CDFSettings
from .scoring import ScoreSettings
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
        'cdf': set(CDFSettings.__dataclass_fields__),
        'scoring': set(ScoreSettings.__dataclass_fields__),
        'svi': {'n_starts', 'seed', 'spread_floor_iv', 'maximum_weight_share', 'minimum_variance',
                'maximum_minimum_variance', 'minimum_b_fraction', 'rho_limit', 'minimum_s', 'maximum_s',
                'maximum_abs_m', 'lee_margin', 'max_nfev', 'optimiser_tolerance', 'butterfly_tolerance',
                'calendar_tolerance', 'grid_standard_deviations', 'grid_points', 'local_grid_points',
                'local_grid_scales', 'candidate_selection', 'primary_acceptance'},
    }
    expected = {'version', 'status', 'evaluation', *sections}
    if not isinstance(content, dict) or set(content) != expected:
        actual = set(content) if isinstance(content, dict) else set()
        raise ValueError(f'protocol: missing keys={sorted(expected - actual)}; unexpected keys={sorted(actual - expected)}')
    if type(content['version']) is not int or content['version'] != 1 or content['status'] not in ('draft', 'fixed'):
        raise ValueError('Expected version 1 draft or fixed research settings')
    integer_fields = {'minimum_unique_strikes', 'minimum_strikes_each_side', 'max_iterations',
                      'minimum_mark_comparisons', 'n_starts', 'seed', 'max_nfev', 'grid_points',
                      'local_grid_points', 'crps_initial_points', 'crps_max_refinements'}
    for section, fields in sections.items():
        values = content[section]
        if not isinstance(values, dict) or set(values) != fields:
            actual = set(values) if isinstance(values, dict) else set()
            raise ValueError(f'{section}: missing keys={sorted(fields - actual)}; unexpected keys={sorted(actual - fields)}')
        for field, value in values.items():
            if field in ('candidate_selection', 'primary_acceptance'):
                expected_value = {'candidate_selection': 'lowest_weighted_error',
                                  'primary_acceptance': 'butterfly_and_calendar_clear'}[field]
                if value != expected_value:
                    raise ValueError(f'{field} must be {expected_value}')
                continue
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if (not finite or value < 0 or value == 0 and field != 'seed'
                    or field in integer_fields and type(value) is not int):
                raise ValueError(f'{field} must be finite and positive with the declared numeric type')
    inv, clean = content['inversion'], content['cleaning']
    if inv['lower_iv'] >= inv['upper_iv'] or inv['relative_tolerance'] < 4 * math.ulp(1.0):
        raise ValueError('Invalid IV bracket or Brent relative tolerance')
    if clean['minimum_unique_strikes'] < 2 * clean['minimum_strikes_each_side']:
        raise ValueError('minimum_unique_strikes must cover both strike sides')
    svi = content['svi']
    if (not 1 / clean['minimum_unique_strikes'] <= svi['maximum_weight_share'] <= 1
            or not 0 < svi['rho_limit'] <= 0.999 or not 0 < svi['minimum_b_fraction'] < 1
            or not 0 < svi['lee_margin'] < 2 or svi['minimum_s'] < 1e-4
            or svi['maximum_s'] <= svi['minimum_s']
            or svi['maximum_minimum_variance'] <= svi['minimum_variance']
            or svi['optimiser_tolerance'] <= math.ulp(1.0)
            or min(svi['grid_points'], svi['local_grid_points']) < 3):
        raise ValueError('Invalid coupled SVI settings')
    CDFSettings(**content['cdf'])
    ScoreSettings(**content['scoring'])
    evaluation = content['evaluation']
    fields = {'lead_times_ms', 'snapshot_tolerance_ms', 'freeze_delay_tolerance_ms', 'first_expiry_ms',
              'primary_hypothesis', 'primary_metric', 'primary_lead_ms', 'primary_baseline', 'baseline_names',
              'bootstrap_resamples', 'bootstrap_seed', 'expected_block_length', 'minimum_effective_sample_size',
              'multiple_testing', 'expiry_class_rule', 'cdf_support_policy'}
    if not isinstance(evaluation, dict) or set(evaluation) != fields:
        raise ValueError('Invalid evaluation fields')
    if evaluation['lead_times_ms'] != [86400000, 604800000]:
        raise ValueError('Supported leads are 24 hours and 7 days')
    for field in ('snapshot_tolerance_ms', 'freeze_delay_tolerance_ms', 'first_expiry_ms',
                  'primary_lead_ms', 'bootstrap_resamples', 'bootstrap_seed',
                  'expected_block_length', 'minimum_effective_sample_size'):
        value = evaluation[field]
        if type(value) is not int or value < (0 if field == 'bootstrap_seed' else 1):
            raise ValueError(f'Invalid evaluation integer: {field}')
    if (evaluation['primary_lead_ms'] not in evaluation['lead_times_ms']
            or evaluation['bootstrap_resamples'] < 2000 or evaluation['expected_block_length'] < 7
            or evaluation['freeze_delay_tolerance_ms'] >= min(evaluation['lead_times_ms'])
            or evaluation['snapshot_tolerance_ms'] >= min(evaluation['lead_times_ms'])
            or evaluation['primary_metric'] != 'crps_usd'
            or evaluation['primary_baseline'] != 'same_snapshot_atm_lognormal'
            or evaluation['baseline_names'] != ['same_snapshot_atm_lognormal', 'trailing_realised_lognormal', 'garch11_student_t']
            or not isinstance(evaluation['primary_hypothesis'], str) or not evaluation['primary_hypothesis'].strip()
            or evaluation['multiple_testing'] != 'single_declared_primary_comparison'
            or evaluation['expiry_class_rule'] != 'quarterly_then_monthly_then_friday_weekly_else_daily'
            or evaluation['cdf_support_policy'] != 'collapse_tails_to_boundary_atoms'
            or evaluation['first_expiry_ms'] % 86400000 != 8 * 3600000):
        raise ValueError('Invalid coupled evaluation choices')
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
