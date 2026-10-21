# Does DVOL add information beyond HAR?

[Research paper](report/latex/main.pdf) · [LaTeX source](report/latex/main.tex) · [Results and diagnostics](report/incremental-results.json) · [![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

Raw BTC DVOL, Deribit's option-implied volatility index for bitcoin, has the lowest average forecast loss in this sample.
I'm testing whether it adds information beyond a heterogeneous autoregressive (HAR) model built from daily, weekly and monthly price-history variance.
The comparison uses 1,357 matched 30-day forecasts; adding DVOL improves HAR's average loss, while the uncertainty interval spans zero and the allocation diagnostic shows no clear gain.

| Forecast | Mean quasi-likelihood (QLIKE) variance loss, lower is better |
| --- | ---: |
| Trailing variance | 0.16670 |
| Raw DVOL | 0.09611 |
| HAR | 0.12548 |
| HAR + DVOL | 0.11123 |

Augmentation minus HAR has mean loss difference -0.01424 and a descriptive 95% interval [-0.03490, +0.00505]. Estimated effective sample size (ESS), an estimate of sample information after accounting for dependence, is 51.71 against the preset reporting threshold of 60. Neither regression reaches its fixed positivity floor.

[![First page of my LaTeX research paper, with the abstract and matched BTC forecast results](report/latex/paper-preview.png)](report/latex/main.pdf)

## Limits and status

The historical findings are exploratory. The original open, high, low and close (OHLC) input check fails on an unused DVOL open; the three audited closes match. Chronological fitting and point-loss rankings alone establish neither statistical superiority nor a trading result. The paper's [limitations section](report/latex/main.pdf) explains the data, dependence and allocation assumptions.

A locked year starts on 21 October 2026. Every declared one-year synthetic replication fails the ESS reporting rule, so the inference amendment makes this an evidence-collection period. A separate stochastic volatility inspired (SVI) option-probability study has zero settled forecasts. The [scoreboard snapshots](report/scoreboard.html) record bounded supervised validation; continuous collection needs an always-on host.

## Run it

With Python 3.12 on Windows, the numerical results rebuild offline:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt -e ".[research,dev]"
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/build_research.py
```

On macOS or Linux, I replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`. The outputs are `results/research/summary.json` and `forecasts.csv`.

The paper compiles separately with [these LaTeX commands](report/latex/README.md). Its printed values live in `main.tex` and are checked against the rebuilt results before manuscript revisions.

[Methods and live commands](report/research-note.md) · [Locked plan](protocol/incremental-variance-v1.json) · [Inference amendment](protocol/inference-validation-v1.json) · [Retained public inputs](recordings/historical-variance/manifest.json). Source is in `src/implied_calibration/`; checks are in `tests/`.

The code has an [MIT licence](LICENSE). Retained Deribit responses keep their original source provenance.
