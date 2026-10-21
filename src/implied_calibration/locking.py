"""Fixed protocols are verified against immutable locks and Git witnesses."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .protocol import Protocol, load_protocol, validate_protocol
from .store import digest


@dataclass(frozen=True)
class LockedProtocol:
    protocol: Protocol
    lock: dict[str, Any]
    witness_commit: str
    witness_at_ms: int

    def validate(self) -> None:
        verified = validate_protocol(self.protocol.content)
        fields = {'protocol_hash', 'locked_at_ms', 'first_expiry_ms', 'git_commit'}
        if (verified.content_hash != self.protocol.content_hash or self.protocol.content['status'] != 'fixed'
                or not isinstance(self.lock, dict) or set(self.lock) != fields or self.lock['protocol_hash'] != verified.content_hash
                or self.lock['first_expiry_ms'] != self.protocol.content['evaluation']['first_expiry_ms']
                or not isinstance(self.witness_commit, str) or not re.fullmatch('[0-9a-f]{40}', self.witness_commit)
                or not isinstance(self.lock['git_commit'], str) or not re.fullmatch('[0-9a-f]{40}', self.lock['git_commit'])
                or type(self.lock['locked_at_ms']) is not int or type(self.witness_at_ms) is not int):
            raise ValueError('Protocol, lock or Git witness differs')
        first_target = self.lock['first_expiry_ms'] - max(self.protocol.content['evaluation']['lead_times_ms'])
        if not 0 <= self.lock['locked_at_ms'] <= self.witness_at_ms + 999 < first_target:
            raise ValueError('Protocol lock must be committed before the first eligible decision time')


def load_locked_protocol(path: Path) -> LockedProtocol:
    path = Path(path).resolve()
    protocol = load_protocol(path)
    lock_path = path.with_name('protocol.lock.json')
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if not isinstance(lock, dict) or set(lock) != {'protocol_hash', 'locked_at_ms', 'first_expiry_ms', 'git_commit'}:
        raise ValueError('Invalid protocol lock fields')
    LockedProtocol(protocol, lock, '0' * 40, lock['locked_at_ms']).validate()
    root = path.parent.parent
    relative, lock_relative = path.relative_to(root).as_posix(), lock_path.relative_to(root).as_posix()

    def git(*args: str) -> str:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError('Expected the committed protocol and lock in this Git checkout')
        return result.stdout.strip()

    config = json.loads(git('show', f"{lock['git_commit']}:{relative}"))
    if digest(config) != protocol.content_hash:
        raise ValueError('The lock does not reference the committed protocol content')
    witness = None
    for commit in git('log', '--reverse', '--format=%H', '--', lock_relative).splitlines():
        if json.loads(git('show', f'{commit}:{lock_relative}')) == lock and digest(
                json.loads(git('show', f'{commit}:{relative}'))) == protocol.content_hash:
            witness = commit
            break
    if witness is None:
        raise ValueError('Expected a committed matching protocol lock before prediction or scoring')
    result = LockedProtocol(protocol, lock, witness, int(git('show', '-s', '--format=%ct', witness)) * 1000)
    result.validate()
    return result
