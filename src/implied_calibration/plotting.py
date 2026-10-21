"""Smile diagnostics identify synthetic inputs explicitly."""

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .density import ImpliedCDF
from .svi import FitResult, total_variance


def smile_figure(k: list[float] | np.ndarray, iv_bid: list[float] | np.ndarray,
                 iv_ask: list[float] | np.ndarray, t_years: float, result: FitResult,
                 *, synthetic: bool, label: str) -> Figure:
    x, bid, ask = [np.asarray(value, dtype=float) for value in (k, iv_bid, iv_ask)]
    figure = Figure(figsize=(8, 5), layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    axes.scatter(x, bid * 100, marker='v', s=18, label='Bid IV')
    axes.scatter(x, ask * 100, marker='^', s=18, label='Ask IV')
    order = np.argsort(x)
    axes.fill_between(x[order], bid[order] * 100, ask[order] * 100, alpha=0.15)
    if result.params is not None:
        dense = np.linspace(float(x.min()), float(x.max()), 501)
        axes.plot(dense, 100 * np.sqrt(total_variance(dense, result.params) / t_years), label='SVI fit')
    axes.set(xlabel='Log-moneyness k = ln(K/F)', ylabel='Implied volatility, percent',
             title=('Synthetic: ' if synthetic else '') + label)
    min_g = f'{result.min_g:.6g}' if result.min_g is not None else 'not available'
    axes.text(0.02, 0.02, f'Status: {result.status}; min g: {min_g}',
              transform=axes.transAxes, fontsize=8)
    axes.legend()
    axes.grid(alpha=0.2)
    return figure


def cdf_figure(distribution: ImpliedCDF, *, synthetic: bool, label: str) -> Figure:
    record = distribution.to_record()
    extent = max(0.8, abs(distribution.observed_k_min) + 0.3, abs(distribution.observed_k_max) + 0.3)
    k = np.linspace(-extent, extent, 1201)
    price = distribution.forward_usd * np.exp(k)
    cdf, _ = distribution.probabilities(k)
    figure = Figure(figsize=(8, 5), layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    axes.plot(price, cdf, color='#195b80', label='SVI risk-neutral CDF')
    lower, upper = distribution.forward_usd * np.exp([distribution.observed_k_min, distribution.observed_k_max])
    axes.axvspan(price[0], lower, color='#dfac5c', alpha=0.2, label='Extrapolated SVI wings')
    axes.axvspan(upper, price[-1], color='#dfac5c', alpha=0.2)
    for boundary in (lower, upper):
        axes.axvline(boundary, color='#97732e', linestyle=':', linewidth=1)
    axes.set(xlabel='Terminal price, USD', ylabel='Risk-neutral probability below price', ylim=(0, 1),
             xlim=(price[0], price[-1]), title=('Synthetic: ' if synthetic else '') + label)
    diagnostics = record['diagnostics']
    axes.text(0.02, 0.02, f"Extrapolated mass: {diagnostics['extrapolated_probability_mass']:.2%}; "
              f"max correction: {diagnostics['maximum_correction']:.3g}", transform=axes.transAxes, fontsize=8)
    axes.legend(loc='upper left')
    axes.grid(alpha=0.2)
    return figure
