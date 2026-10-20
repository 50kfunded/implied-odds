"""I verify versioned research choices and their pre-outcome Git witness."""

import json
from pathlib import Path
import subprocess

from .store import digest


def load_plan(path: Path, *, require_witness: bool = True) -> dict:
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding='utf-8'))
    lock_path = path.with_suffix('.lock.json')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if (lock.get('sha256') != digest(value) or lock.get('schema_version') != 1
            or type(lock.get('locked_at_ms')) is not int):
        raise ValueError('I need the matching immutable research plan lock')
    if value['kind'] == 'incremental_variance_v1':
        first = value['future']['origin_start_ms'] - value['future']['freeze_start_before_origin_ms']
    elif value['kind'] == 'forward_stopping_amendment_v1':
        original = json.loads((path.parent/'protocol.json').read_text(encoding='utf-8'))
        if digest(original) != value['parent_protocol_hash']:
            raise ValueError('I need the unchanged original protocol')
        first = original['evaluation']['first_expiry_ms']
    elif value['kind'] == 'incremental_inference_guard_v1':
        first = value['first_freeze_ms']
    else:
        raise ValueError('I do not recognise this research plan')
    if not 0 <= lock['locked_at_ms'] < first:
        raise ValueError('I need the plan lock before the first decision')
    if require_witness:
        root = path.parent.parent
        def git(*args):
            return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
        relative = path.relative_to(root).as_posix()
        witness = None
        for commit in git('log', '--reverse', '--format=%H', '--', lock_path.relative_to(root).as_posix()).splitlines():
            saved = json.loads(git('show', f'{commit}:{relative}'))
            saved_lock = json.loads(git('show', f'{commit}:{lock_path.relative_to(root).as_posix()}'))
            if saved_lock == lock and digest(saved) == lock['sha256']:
                witness = commit
                break
        if witness is None or int(git('show', '-s', '--format=%ct', witness)) * 1000 >= first:
            raise ValueError('I need a matching Git witness before the first decision')
    return value


def stopping_choices(choices: dict, parent_hash: str, baseline_hash: str, as_of_ms: int,
                     amendment_path: Path | None = None) -> dict:
    path = amendment_path or Path(__file__).resolve().parents[2] / 'protocol' / 'forward-inference-v1.json'
    plan = load_plan(path)
    if plan['parent_protocol_hash'] != parent_hash or plan['parent_baseline_hash'] != baseline_hash:
        return {**choices, 'formal_decision_allowed': False, 'expiry_cutoff_ms': 9223372036854775807,
                'stopping_amendment_hash': None, 'final_analysis_at_ms': None,
                'stopping_status': 'I have no matching stopping amendment; I keep inference descriptive.'}
    return {**choices, 'formal_decision_allowed': as_of_ms >= plan['analysis_at_ms'],
            'expiry_cutoff_ms': plan['expiry_cutoff_ms'], 'stopping_amendment_hash': digest(plan),
            'final_analysis_at_ms': plan['analysis_at_ms']}
