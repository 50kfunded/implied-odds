"""Finite-sample inference diagnostics use declared loss processes."""

import math

import numpy as np

from .inference import effective_sample_size


def bootstrap_interval(values, *, resamples: int, block: int, rng) -> list[float]:
    x = np.asarray(values, dtype=float)
    n = len(x)
    prefix = np.r_[0., np.cumsum(np.tile(x, 2))]
    totals = np.zeros(resamples)
    remaining = np.full(resamples, n)
    while np.any(remaining):
        active = np.flatnonzero(remaining)
        starts = rng.integers(0, n, len(active))
        lengths = np.minimum(rng.geometric(1/block, len(active)), remaining[active])
        totals[active] += prefix[starts+lengths]-prefix[starts]
        remaining[active] -= lengths
    return np.quantile(totals/n, [.025,.975]).tolist()


def loss_process(n: int, name: str, rng) -> np.ndarray:
    innovations = rng.normal(size=n+29)
    overlap = np.convolve(innovations, np.ones(30)/math.sqrt(30), mode='valid')
    if name == 'overlap_30':
        return overlap
    if name == 'overlap_30_plus_slow_365':
        rho = math.exp(-1/365)
        slow = np.empty(n)
        slow[0] = rng.normal()
        for i in range(1,n):
            slow[i] = rho*slow[i-1]+math.sqrt(1-rho*rho)*rng.normal()
        return (overlap+.7*slow)/math.sqrt(1+.7**2)
    if name == 'overlap_30_plus_clustered_tails':
        shocks = rng.binomial(1,.005,n+29)*rng.choice([-10.,10.],n+29)
        tails = np.convolve(shocks,np.ones(30)/math.sqrt(30),mode='valid')
        return (overlap+tails)/math.sqrt(1+.005*100)
    raise ValueError('Expected a declared simulation process')


def wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes/total
    centre = (p+z*z/(2*total))/(1+z*z/total)
    half = z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/(1+z*z/total)
    return [max(0.,centre-half),min(1.,centre+half)]


def simulation_report(plan: dict) -> dict:
    config = plan['simulation']
    output: list[dict] = []
    for n in config['sample_sizes']:
        for name in config['dgp']:
            for shift in config['power_mean_shifts']:
                # Independent stationary draws preserve the population null; realised samples stay uncentred.
                rng = np.random.default_rng(config['seed']+n+len(output)*10000)
                rejected = passed = gated = 0
                for _ in range(config['replications']):
                    x = loss_process(n,name,rng)+shift
                    interval = bootstrap_interval(x,resamples=config['bootstrap_resamples'],
                                                  block=plan['expected_block_days'],rng=rng)
                    reject = interval[0] > 0 or interval[1] < 0
                    gate = effective_sample_size(x,plan['hac_lags_days']) >= plan['minimum_effective_sample_size']
                    rejected += reject
                    passed += gate
                    gated += reject and gate
                count = config['replications']
                output.append({'n':n,'dgp':name,'population_mean':shift,'replications':count,
                    'ungated_rejection_rate':rejected/count,'ungated_mc_interval_95':wilson(rejected,count),
                    'gate_pass_rate':passed/count,'gate_pass_mc_interval_95':wilson(passed,count),
                    'gated_unconditional_rejection_rate':gated/count,'gated_mc_interval_95':wilson(gated,count),
                    'conditional_rejection_given_gate':gated/passed if passed else None,
                    'conditional_mc_interval_95':wilson(gated,passed)})
    return {'kind':'synthetic_finite_sample_inference_diagnostic','synthetic':True,'settings':config,
        'rows':output,'limit':"The simulation uses mean-zero overlapping losses and a 365-day persistence component, rather than the entire fitted market pipeline. Its size and power estimates do not certify market inference."}
