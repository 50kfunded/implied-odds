"""Exact finite-tree checks and fixed-policy simulations for partial settlement."""

from functools import lru_cache
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
from statistics import NormalDist

import matplotlib as mpl
from matplotlib.figure import Figure
import numpy as np

from .progressive_evidence import FRACTIONS, horizon_means, qlike_mean_bound, simulate_cash


def exact_tree(horizon: int, *, periods: int = 12, markov: bool = False) -> dict:
    """Independent scalar accounting and Snell recursion on the complete tree.

    Stop BEFORE a new stake. The recursion uses the entire observed history,
    including partially revealed outcomes, rather than only matured windows.
    """
    if horizon not in (2, 3, 4) or type(periods) is not int or not horizon <= periods <= 14:
        raise ValueError('Exact enumeration is restricted to small declared trees')
    transition = np.array([[.9, .1], [.15, .85]]) if markov else np.full((2, 2), .5)
    values = np.array([.2, 3.]) if markov else np.array([0., 2.])
    bounds = horizon_means(transition, values, horizon)

    @lru_cache(None)
    def state(history: tuple[int, ...]) -> float:
        cash, active, previous = 1., [], 0
        for value in history:
            fraction = (.8 if previous else .2) if markov else (.8 if values[previous] > 0 else .2)
            stake = fraction*cash
            cash -= stake
            active.append((stake/(horizon*bounds[previous]), horizon))
            cash += values[value]*sum(coef for coef, _ in active)
            active = [(coef, remaining-1) for coef, remaining in active if remaining > 1]
            previous = value
        return cash

    @lru_cache(None)
    def optimal(history: tuple[int, ...]) -> float:
        cash = state(history)
        if len(history) == periods:
            return cash
        previous = history[-1] if history else 0
        continuation = sum(transition[previous, value]*optimal(history+(value,)) for value in (0, 1))
        return max(cash, continuation)

    terminal, naive_expectation, naive_crossing = 0., 0., 0.
    for path in itertools.product((0, 1), repeat=periods):
        probability, previous = 1., 0
        for value in path:
            probability *= transition[previous, value]
            previous = value
        terminal += probability*state(path)
        if not markov:
            factors = [sum(values[list(path[i:i+horizon])])/horizon for i in range(periods-horizon+1)]
            naive = np.cumprod(factors)
            naive_expectation += probability*naive[-1]
            naive_crossing += probability*bool(max(naive) >= 20)
    result = {'horizon': horizon, 'periods': periods, 'paths': 2**periods,
              'dependent_markov': markov, 'maximum_expected_stopped_cash': optimal(()),
              'expected_terminal_cash': terminal}
    if not markov:
        result.update(naive_product_expectation=naive_expectation,
                      naive_product_threshold_20_crossing=naive_crossing)
    if abs(result['maximum_expected_stopped_cash']-1) > 1e-10:
        raise ValueError('Exact stopped-cash check failed')
    return result


def early_witness() -> dict:
    horizon, baseline, event_target, event_day = 30, .01, 21., 11
    event_probability = (1-baseline)/(event_target-baseline)
    increment = (event_target-baseline)*horizon
    daily = np.full(horizon, baseline)
    daily[event_day-1] += increment
    progressive = np.cumsum(daily)/horizon
    completion = np.zeros(horizon)
    completion[-1] = progressive[-1]
    candidate, comparator = math.e-1, 1-math.exp(-1)
    bound = qlike_mean_bound(candidate, comparator)
    if bound is None or not math.isclose(bound, 1, abs_tol=1e-14):
        raise ValueError('QLIKE witness bound differs')
    return {'horizon': horizon, 'baseline': baseline, 'event_day': event_day,
            'event_target_average': event_target, 'extra_increment': increment,
            'null_event_probability': event_probability,
            'null_expected_target_average': (1-event_probability)*baseline+event_probability*event_target,
            'alternative_event_probability': .5, 'candidate': candidate, 'comparator': comparator,
            'mean_bound': bound, 'progressive': progressive.tolist(), 'completion_only': completion.tolist(),
            'progressive_first_crossing': int(np.flatnonzero(progressive >= 20)[0]+1),
            'completion_first_crossing': horizon,
            'scope': 'one stake at origin zero; purpose-built mathematical witness, not a market simulation'}


def rate_summary(count: int, n: int) -> dict:
    p, z = count/n, NormalDist().inv_cdf(.975)
    denominator = 1+z*z/n
    center = (p+z*z/(2*n))/denominator
    radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
    return {'count': count, 'rate': p, 'monte_carlo_wilson_95': [center-radius, center+radius]}


def simulation_experiment(*, replications: int = 10000, seed: int = 5261028) -> list[dict]:
    if type(replications) is not int or replications < 1 or type(seed) is not int or seed < 0:
        raise ValueError('Expected positive replication count and a nonnegative seed')
    rng, periods, horizon = np.random.default_rng(seed), 365, 30
    results = []
    for label, multiplier, markov in (('iid_null', 1., False), ('iid_alt1.5', 1.5, False),
                                      ('markov_null', 1., True), ('markov_alt1.5', 1.5, True)):
        if markov:
            transition, means = np.array([[.98, .02], [.03, .97]]), np.array([.3, 2.2])
            bound = horizon_means(transition, means, horizon)
            state = rng.choice(2, size=replications, p=[.6, .4])
            daily, thresholds = np.empty((replications, periods)), np.empty((replications, periods))
            for time in range(periods):
                thresholds[:, time] = bound[state]
                state = (rng.random(replications) < transition[state, 1]).astype(int)
                daily[:, time] = rng.gamma(.3, multiplier*means[state]/.3)
        else:
            daily = rng.gamma(.3, multiplier/.3, size=(replications, periods))
            thresholds = np.ones_like(daily)
        item: dict = {'dgp': label, 'replications': replications, 'periods': periods, 'horizon': horizon}
        for name, delayed in (('progressive', False), ('completion_only', True)):
            paths = simulate_cash(daily, thresholds, completion_only=delayed)
            item[name] = {**rate_summary(int(np.sum(paths['maximum_cash'] >= 20)), replications),
                          'mean_terminal_cash': float(paths['terminal_cash'].mean()),
                          'individual_fraction_rates': np.mean(paths['individual_maximum_cash'] >= 20, axis=1).tolist()}
        phases = {}
        for fraction in (.5, 1.):
            cash, maximum = np.ones(replications), np.ones(replications)
            for origin in range(0, periods-horizon+1, horizon):
                factor = daily[:, origin:origin+horizon].mean(axis=1)/thresholds[:, origin]
                cash *= 1-fraction+fraction*factor
                maximum = np.maximum(maximum, cash)
            phases[str(fraction)] = rate_summary(int(np.sum(maximum >= 20)), replications)
        item['preselected_phase_zero'] = phases
        results.append(item)
    return results


def figure_for(result: dict) -> Figure:
    figure = Figure(figsize=(11.5, 4.6), layout='constrained', facecolor='white')
    left, right = figure.subplots(1, 2)
    figure.suptitle('Partial settlement: a constructed witness and synthetic simulations', fontsize=12)
    witness = result['early_witness']
    days = np.arange(31)
    for label, key, color in (('Pay as observed', 'progressive', '#237d88'),
                               ('Pay at completion', 'completion_only', '#cb743f')):
        left.step(days, [0.]+witness[key], where='post', label=label, color=color, linewidth=2)
    left.axhline(20, color='#67717c', linestyle='--', linewidth=.8)
    left.set(title='Constructed example: day 11 versus day 30', xlabel='Day after origin',
             ylabel='Evidence balance', xlim=(0, 31), ylim=(-.5, 24))
    left.legend(fontsize=8, loc='center left', frameon=False)
    alternatives = [r for r in result['simulations'] if 'alt' in r['dgp']]
    x = np.arange(2)
    for offset, key, label, color in ((-.25, 'progressive', 'Pay as observed', '#237d88'),
                                    (0, 'completion_only', 'Pay at completion', '#cb743f'),
                                    (.25, 'phase', 'Fixed nonoverlap phase', '#727f8c')):
        values = [100*(r['preselected_phase_zero']['1.0']['rate'] if key == 'phase' else r[key]['rate']) for r in alternatives]
        bars = right.bar(x+offset, values, .23, label=label, color=color)
        right.bar_label(bars, fmt='%.2f', fontsize=8, padding=3)
    right.set(xticks=x, xticklabels=['Independent Gamma', 'Markov Gamma'], ylim=(0, 104),
              ylabel='Rejection by day 365 (%)', title='A 50% increase in the outcome mean')
    right.legend(fontsize=7.5, frameon=False, loc='upper right')
    for axis in (left, right):
        axis.grid(axis='y', alpha=.15)
        axis.set_axisbelow(True)
        axis.spines[['top', 'right']].set_visible(False)
    return figure


def render_results(result: dict) -> str:
    witness = result['early_witness']
    lines = ['<!-- computed-results:start -->', '## Numerical checks', '',
             f"I enumerate all {result['exact_checks'][0]['paths']:,} binary paths of length "
             f"{result['exact_checks'][0]['periods']} for horizons 2, 3 and 4, using both independent "
             'and dependent Markov outcomes. A backward stop-or-continue recursion checks every '
             'full-information stopping rule for the declared adaptive policy.', '',
             '| Outcome process | Horizon | Maximum expected stopped cash | Naive overlapping product crosses 20 |',
             '| --- | ---: | ---: | ---: |']
    for row in result['exact_checks']:
        naive = row.get('naive_product_threshold_20_crossing')
        lines.append(f"| {'Markov' if row['dependent_markov'] else 'Independent binary'} | {row['horizon']} | "
                     f"{row['maximum_expected_stopped_cash']:.10f} | {'Not evaluated' if naive is None else f'{100*naive:.2f}%'} |")
    lines += ['', 'The naive product violates the nominal 5% bound in these examples. Waiting for outcomes '
              'to mature does not make overlapping products valid.', '',
              f"The single-claim QLIKE witness reaches evidence {witness['progressive'][witness['event_day']-1]:.5f} "
              f"on day {witness['progressive_first_crossing']}; completion-only payment arrives on day "
              f"{witness['completion_first_crossing']}. The event has null probability "
              f"{100*witness['null_event_probability']:.4f}%, with expected target "
              f"{witness['null_expected_target_average']:.5f}. At alternative event probability "
              f"{100*witness['alternative_event_probability']:.0f}%, rejection before maturity is "
              f"{100*witness['alternative_event_probability']:.0f}% versus 0%. This is a constructed witness, "
              'not a market power estimate.', '',
              f"The simulations use {result['specification']['replications']:,} paths, "
              f"{result['specification']['periods']} observations and a {result['specification']['horizon']}-observation "
              'horizon. Seven fixed fractions, 1/120, 1/60, 1/30, 0.1, 0.25, 0.5 and 1, receive equal initial capital. '
              'I average current cash before taking its running maximum. All methods use the same draws and origin bounds.', '',
              '| Scenario | Progressive mixture | Completion-only mixture | Preselected phase, full stake |',
              '| --- | ---: | ---: | ---: |']
    for row in result['simulations']:
        lines.append(f"| {row['dgp']} | {100*row['progressive']['rate']:.2f}% | "
                     f"{100*row['completion_only']['rate']:.2f}% | {100*row['preselected_phase_zero']['1.0']['rate']:.2f}% |")
    lines += ['', 'The entries are probabilities of crossing evidence 20 by observation 365. The alternative '
              'multiplies the daily mean by 1.5. The Markov null uses the exact conditional mean of the next '
              '30-observation average, rather than a bound on each future daily mean. The JSON retains '
              'Monte Carlo Wilson intervals and every individual fraction and phase comparator.', '',
              'Progressive settlement does not dominate the preselected phase: the Markov alternative gives '
              'the phase higher power. The proof supplies validity under its assumptions; simulation rates '
              'illustrate finite examples.', '', '<!-- computed-results:end -->']
    return '\n'.join(lines)


def write_progressive_report(root: Path, output: Path, *, check: bool = False, publish: bool = False) -> dict:
    spec_path = root/'protocol/progressive-evidence-v1.json'
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    if (spec['fractions'] != list(FRACTIONS) or spec['replications'] != 10000
            or spec['horizon'] != 30 or spec['periods'] != 365 or spec['evidence_threshold'] != 20
            or spec['gamma_shape'] != .3 or spec['alternative_mean_multiplier'] != 1.5
            or spec['markov_transition'] != [[.98, .02], [.03, .97]]
            or spec['markov_daily_means'] != [.3, 2.2] or spec['markov_initial_probabilities'] != [.6, .4]):
        raise ValueError('Declared experiment differs from implemented settings')
    result = {'kind': 'synthetic_partial_settlement_verification', 'live_test': False,
              'worldwide_novelty_established': False, 'specification': spec,
              # Git checkouts can use CRLF; the specification witness uses UTF-8 LF.
              'specification_sha256': hashlib.sha256(spec_path.read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
              'exact_checks': [exact_tree(h, markov=m) for m in (False, True) for h in (2, 3, 4)],
              'early_witness': early_witness(),
              'simulations': simulation_experiment(replications=spec['replications'], seed=spec['seed'])}
    reference = root/'report/progressive-results.json'
    if check:
        check_numeric_tree(result, json.loads(reference.read_text(encoding='utf-8')))
    output.mkdir(parents=True, exist_ok=True)
    data = json.dumps(result, indent=2, allow_nan=False)+'\n'
    (output/'summary.json').write_text(data, encoding='utf-8')
    note_path = root/'report/progressive-evidence.md'
    note = note_path.read_text(encoding='utf-8')
    pattern = r'<!-- computed-results:start -->.*?<!-- computed-results:end -->'
    updated, count = re.subn(pattern, lambda _: render_results(result), note, flags=re.S)
    if count != 1 or (check and updated != note):
        raise ValueError('The partial-settlement manuscript numbers differ from the rebuilt results')
    (output/'research-note.md').write_text(updated, encoding='utf-8')
    figure = figure_for(result)
    with mpl.rc_context({'svg.hashsalt': 'implied-odds-progressive-v1'}):
        figure.savefig(output/'progressive-evidence.svg', metadata={'Date': None})
        figure.savefig(output/'progressive-evidence.pdf', metadata={'CreationDate': None, 'ModDate': None})
    if publish:
        reference.write_text(data, encoding='utf-8')
        note_path.write_text(updated, encoding='utf-8')
        (root/'report/progressive-evidence.svg').write_bytes((output/'progressive-evidence.svg').read_bytes())
    return result


def check_numeric_tree(actual, expected) -> None:
    """Keep counts and structure exact, allowing final-bit numerical differences."""
    if isinstance(actual, dict) and isinstance(expected, dict) and actual.keys() == expected.keys():
        for key in actual:
            check_numeric_tree(actual[key], expected[key])
    elif isinstance(actual, list) and isinstance(expected, list) and len(actual) == len(expected):
        for a, b in zip(actual, expected):
            check_numeric_tree(a, b)
    elif isinstance(actual, float) and isinstance(expected, float) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12):
        return
    elif actual != expected:
        raise ValueError('Rebuilt progressive evidence results differ from the published JSON')
