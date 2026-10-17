"""I draw smile diagnostics and label synthetic inputs explicitly."""

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

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
