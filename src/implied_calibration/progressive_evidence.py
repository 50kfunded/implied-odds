"""Cash-constrained monitoring of nonnegative, progressively revealed targets.

Validity requires E[total claim payoff | information at purchase] <= 1.
Unpaid claims have no spendable value. This is a conditional-mean test;
historical average forecast superiority does not imply the required null.
"""

from dataclasses import dataclass
import math

import numpy as np


FRACTIONS = (1/120, 1/60, 1/30, .1, .25, .5, 1.)


def qlike_mean_bound(candidate: float, comparator: float, margin: float = 0.) -> float | None:
    """Reduce the directional paired-QLIKE null to an upper bound on E[Y].

    Q(Y,A)-Q(Y,B) = log(A/B) - Y*(1/B-1/A). For A>B the
    null E[paired score | F] >= -margin bounds E[Y] from above.
    Equality imposes no mean bound, so A<=B is skipped, including A==B.
    """
    if (not all(math.isfinite(v) for v in (candidate, comparator, margin))
            or candidate <= 0 or comparator <= 0 or margin < 0):
        raise ValueError('Expected positive finite forecasts and a nonnegative finite margin')
    if candidate <= comparator:
        return None
    ratio_difference = (candidate-comparator)/comparator
    if math.isfinite(ratio_difference):
        log_ratio = math.log1p(ratio_difference)
        # This form avoids the product A*B and subtracting reciprocal forecasts.
        bound = candidate*((log_ratio+margin)/ratio_difference)
    else:
        log_ratio = math.log(candidate)-math.log(comparator)
        bound = comparator*((log_ratio+margin)/(1-comparator/candidate))
    if not math.isfinite(bound) or bound <= 0:
        raise ValueError('Mean bound cannot be represented as a finite positive float')
    return bound


@dataclass(frozen=True)
class Claim:
    identifier: str
    origin: int
    maturity: int
    stake: float
    mean_bound: float


class CashLedger:
    """A replayable scalar ledger with immutable, nonoverlapping increment IDs.

    Times are integer indices on one declared target clock. A bound refers to
    the SUM of the increments in (origin, maturity]. To monitor an average,
    divide each increment by the horizon before crediting it. A delayed
    verified increment can be credited later, but never before its end time.
    """

    def __init__(self) -> None:
        self.time = 0
        self.cash = 1.
        self.maximum = 1.
        self.claims: dict[str, Claim] = {}
        self.increments: dict[str, tuple[int, int, float]] = {}
        self.events: list[dict] = []

    def advance(self, time: int) -> None:
        if type(time) is not int or time < self.time:
            raise ValueError('Observation time must be an increasing integer clock')
        self.time = time

    def issue(self, identifier: str, maturity: int, mean_bound: float, stake: float) -> None:
        if (not identifier or identifier in self.claims or type(maturity) is not int
                or maturity <= self.time or not math.isfinite(mean_bound) or mean_bound <= 0
                or not math.isfinite(stake) or not 0 <= stake <= self.cash):
            raise ValueError('Invalid claim, duplicate ID or borrowing against unpaid outcomes')
        claim = Claim(identifier, self.time, maturity, stake, mean_bound)
        self.claims[identifier] = claim
        self.cash -= stake
        self.events.append({'kind': 'issue', 'time': self.time, 'identifier': identifier,
                            'maturity': maturity, 'mean_bound': mean_bound, 'stake': stake})

    def credit(self, identifier: str, start: int, end: int, amount: float) -> float:
        if (not identifier or type(start) is not int or type(end) is not int or start < 0
                or not start < end <= self.time or not math.isfinite(amount) or amount < 0):
            raise ValueError('Expected a verified nonnegative increment observed after its end')
        observation = (start, end, amount)
        if identifier in self.increments:
            if self.increments[identifier] != observation:
                raise ValueError('Previously credited outcomes are immutable')
            return 0.
        if any(start < other_end and other_start < end
               for other_start, other_end, _ in self.increments.values()):
            raise ValueError('An overlapping increment would credit the same outcome twice')
        payouts = []
        for claim in self.claims.values():
            if start < claim.maturity and claim.origin < end:
                if not claim.origin <= start < end <= claim.maturity:
                    raise ValueError('An increment straddles a claim boundary; split it first')
                payouts.append(claim.stake*(amount/claim.mean_bound))
        payout = math.fsum(payouts)
        if not math.isfinite(payout) or not math.isfinite(self.cash+payout):
            raise ValueError('Evidence payout overflows floating point')
        self.increments[identifier] = observation
        self.cash += payout
        self.maximum = max(self.maximum, self.cash)
        self.events.append({'kind': 'credit', 'time': self.time, 'identifier': identifier,
                            'start': start, 'end': end, 'amount': amount})
        return payout

    @classmethod
    def replay(cls, events: list[dict]) -> 'CashLedger':
        ledger = cls()
        for event in events:
            ledger.advance(event['time'])
            if event['kind'] == 'issue':
                ledger.issue(event['identifier'], event['maturity'], event['mean_bound'], event['stake'])
            elif event['kind'] == 'credit':
                ledger.credit(event['identifier'], event['start'], event['end'], event['amount'])
            else:
                raise ValueError('Unknown evidence event')
        return ledger


def simulate_cash(daily, thresholds, *, horizon: int = 30, fractions=FRACTIONS,
                  completion_only: bool = False, evidence_threshold: float = 20.) -> dict:
    """Fixed equally funded accounts, with next increment observed AFTER staking.

    Each threshold is known at its origin and bounds the next horizon-average.
    The runner cannot certify that information requirement from arrays. Each
    account starts at one; its equal-weight mean is total capital from an
    initial unit split equally. The mean is formed BEFORE taking a maximum.
    """
    daily, thresholds = np.asarray(daily, float), np.asarray(thresholds, float)
    fractions = np.asarray(fractions, float)
    if (daily.ndim != 2 or daily.shape != thresholds.shape or not daily.size
            or not np.isfinite(daily).all() or np.any(daily < 0)
            or not np.isfinite(thresholds).all() or np.any(thresholds <= 0)
            or fractions.ndim != 1 or not len(fractions) or not np.isfinite(fractions).all()
            or np.any((fractions < 0) | (fractions > 1))
            or type(horizon) is not int or horizon < 1
            or not math.isfinite(evidence_threshold) or evidence_threshold <= 1):
        raise ValueError('Invalid cash experiment inputs')
    n, periods = daily.shape
    cash = np.ones((len(fractions), n))
    maximum, individual_max = np.ones(n), cash.copy()
    coefficients = np.zeros((len(fractions), horizon, n))
    active, window = np.zeros_like(cash), np.zeros(n)
    first_crossing = np.full(n, -1, dtype=int)
    for time in range(periods):
        stakes = fractions[:, None]*cash
        cash -= stakes
        slot = time % horizon
        if not completion_only:
            active -= coefficients[:, slot]
        coefficients[:, slot] = stakes/(horizon*thresholds[:, time])
        if completion_only:
            window += daily[:, time]
            if time >= horizon:
                window -= daily[:, time-horizon]
            if time >= horizon-1:
                cash += coefficients[:, (time-horizon+1) % horizon]*window
        else:
            active += coefficients[:, slot]
            cash += active*daily[:, time]
        if not np.isfinite(cash).all() or np.min(cash) < -1e-10:
            raise ValueError('Cash simulation lost numerical validity')
        current = cash.mean(axis=0)
        maximum = np.maximum(maximum, current)
        individual_max = np.maximum(individual_max, cash)
        first_crossing = np.where((first_crossing < 0) & (current >= evidence_threshold), time+1, first_crossing)
    return {'terminal_cash': cash.mean(axis=0), 'maximum_cash': maximum,
            'individual_maximum_cash': individual_max, 'first_crossing': first_crossing}


def horizon_means(transition: np.ndarray, daily_means: np.ndarray, horizon: int) -> np.ndarray:
    """Exact next-horizon mean conditional on the current Markov state."""
    if (transition.ndim != 2 or transition.shape != (len(daily_means), len(daily_means))
            or not np.isfinite(transition).all() or np.any(transition < 0)
            or not np.allclose(transition.sum(axis=1), 1)
            or not np.isfinite(daily_means).all() or np.any(daily_means < 0)
            or type(horizon) is not int or horizon < 1):
        raise ValueError('Invalid Markov model')
    result, value = np.zeros(len(daily_means)), daily_means.copy()
    for _ in range(horizon):
        value = transition @ value
        result += value
    return result/horizon
