"""Baseline choices are locked prospectively against the original protocol."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import subprocess

from .locking import LockedProtocol
from .scoring import expiry_class
from .store import digest


@dataclass(frozen=True)
class BaselineSettings:
    realised_window_hours: int = 720
    garch_window_hours: int = 2160
    history_max_age_ms: int = 7200000
    simulation_paths: int = 16384
    simulation_seed: int = 271828
    simulation_quantiles: int = 257
    atm_rule: str = 'fitted_svi_total_variance_at_k_zero'
    realised_rule: str = 'mean_squared_hourly_log_returns'
    gaussian_drift: str = 'minus_half_total_variance_relative_to_frozen_forward'
    garch_drift: str = 'zero_log_drift_relative_to_frozen_forward'
    garch_rule: str = 'zero_mean_garch11_standardised_student_t_full_hourly_paths'
    garch_fractional_hour: str = 'innovation_times_sqrt_hour_overlap'
    garch_gap_rule: str = 'hourly_state_recursion_discard_gap_only_innovations'
    cdf_rule: str = 'linear_cdf_between_empirical_log_return_quantiles'
    support_rule: str = 'same_frozen_svi_bounds_with_boundary_atoms'
    inference_rule: str = 'single_primary_atm_24h_crps_secondary_descriptive'
    block_rule: str = 'max_protocol_block_and_observed_overlap_count'
    hac_rule: str = 'max_protocol_block_minus_one_and_overlap_minus_one'
    ess_rule: str = 'minimum_initial_positive_monotone_pairs_and_overlap_hac'
    pit_rule: str = 'berkowitz_type_hac_joint_normal_moments'
    bootstrap_interval_rule: str = 'two_sided_95_percent_percentile_mean'
    bootstrap_p_rule: str = 'two_sided_null_centred_with_plus_one'

    def validate(self) -> None:
        defaults = asdict(BaselineSettings())
        for key, value in asdict(self).items():
            if isinstance(defaults[key], str):
                if value != defaults[key]:
                    raise ValueError(f'Unknown baseline rule: {key}')
            elif type(value) is not int or value < 1:
                raise ValueError(f'Invalid baseline setting: {key}')
        if (self.realised_window_hours < 24 or self.garch_window_hours < 100
                or self.simulation_paths < 1024 or not 17 <= self.simulation_quantiles <= self.simulation_paths):
            raise ValueError('Baseline windows or simulation resolution are too small')


@dataclass(frozen=True)
class LockedBaselines:
    content: dict
    lock: dict
    witness_commit: str
    witness_at_ms: int

    @property
    def content_hash(self) -> str:
        return digest(self.content)

    @property
    def settings(self) -> BaselineSettings:
        return BaselineSettings(**self.content['settings'])

    def validate(self, parent: LockedProtocol) -> None:
        parent.validate()
        if (not isinstance(self.content, dict) or set(self.content) != {'version', 'parent_protocol_hash', 'first_expiry_ms', 'settings'}
                or type(self.content['version']) is not int or self.content['version'] != 1
                or self.content['parent_protocol_hash'] != parent.protocol.content_hash
                or type(self.content['first_expiry_ms']) is not int
                or self.content['first_expiry_ms'] < parent.lock['first_expiry_ms']
                or not isinstance(self.content['settings'], dict) or set(self.content['settings']) != set(asdict(BaselineSettings()))
                or not isinstance(self.lock, dict) or set(self.lock) != {'extension_hash', 'git_commit', 'locked_at_ms'}
                or self.lock['extension_hash'] != self.content_hash
                or not isinstance(self.lock['git_commit'], str) or not re.fullmatch('[0-9a-f]{40}', self.lock['git_commit'])
                or not isinstance(self.witness_commit, str) or not re.fullmatch('[0-9a-f]{40}', self.witness_commit)
                or type(self.lock['locked_at_ms']) is not int or type(self.witness_at_ms) is not int):
            raise ValueError('Baseline extension or lock differs')
        self.settings.validate()
        expiry_class(self.content['first_expiry_ms'])
        first_target = self.content['first_expiry_ms'] - max(parent.protocol.content['evaluation']['lead_times_ms'])
        if not 0 <= self.lock['locked_at_ms'] <= self.witness_at_ms + 999 < first_target:
            raise ValueError('Expected the baseline lock committed before the first eligible decision')


def load_locked_baselines(path: Path, parent: LockedProtocol) -> LockedBaselines:
    path = Path(path).resolve()
    lock_path = path.with_name('baselines.lock.json')
    content, lock = (json.loads(p.read_text(encoding='utf-8')) for p in (path, lock_path))
    if not isinstance(lock, dict) or type(lock.get('locked_at_ms')) is not int:
        raise ValueError('Invalid baseline lock fields')
    result = LockedBaselines(content, lock, '0' * 40, lock['locked_at_ms'])
    result.validate(parent)
    root = path.parent.parent
    relative, lock_relative = (p.relative_to(root).as_posix() for p in (path, lock_path))

    def git(*args: str) -> str:
        run = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=False)
        if run.returncode:
            raise ValueError('Expected a committed baseline extension and lock')
        return run.stdout.strip()

    if digest(json.loads(git('show', f"{lock['git_commit']}:{relative}"))) != result.content_hash:
        raise ValueError('Baseline lock references different settings')
    for commit in git('log', '--reverse', '--format=%H', '--', lock_relative).splitlines():
        if (json.loads(git('show', f'{commit}:{lock_relative}')) == lock
                and digest(json.loads(git('show', f'{commit}:{relative}'))) == result.content_hash):
            result = LockedBaselines(content, lock, commit, int(git('show', '-s', '--format=%ct', commit)) * 1000)
            result.validate(parent)
            return result
    raise ValueError('Expected a committed matching baseline lock')
