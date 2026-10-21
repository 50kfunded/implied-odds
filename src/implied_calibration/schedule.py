"""UTC slot arithmetic stays separate from clocks and file access."""

from dataclasses import dataclass


def boundary_ms(now_ms: int, interval_ms: int, *, inclusive: bool = False) -> int:
    if interval_ms < 10000:
        raise ValueError("interval must be at least 10 seconds")
    if inclusive:
        return -(-now_ms // interval_ms) * interval_ms
    return (now_ms // interval_ms + 1) * interval_ms


def missed_slots(slot_ms: int, now_ms: int, interval_ms: int) -> int:
    return max(0, (now_ms - slot_ms) // interval_ms + 1)


@dataclass(frozen=True)
class Anchor:
    wall_ms: int
    monotonic_s: float

    def deadline(self, slot_ms: int) -> float:
        return self.monotonic_s + (slot_ms - self.wall_ms) / 1000

    def drift_ms(self, wall_ms: int, monotonic_s: float) -> int:
        return wall_ms - (self.wall_ms + round((monotonic_s - self.monotonic_s) * 1000))
