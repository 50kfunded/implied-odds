# Does DVOL add information beyond HAR?

[Research paper](report/latex/main.pdf) · [LaTeX source](report/latex/main.tex) · [Results and diagnostics](report/incremental-results.json) · [![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

Raw BTC DVOL, Deribit's option-implied volatility index for bitcoin, has the lowest mean quasi-likelihood (QLIKE) loss in this sample. Its paired QLIKE difference from HAR + DVOL is -0.01513, with a descriptive 95% interval [-0.03939, +0.00817] that includes zero.
I'm testing whether it adds information beyond a heterogeneous autoregressive (HAR) model built from daily, weekly and monthly price-history variance.
The comparison uses 1,357 matched 30-day forecasts; HAR + DVOL lowers mean QLIKE loss relative to HAR, while the uncertainty interval spans zero and the volatility-budget diagnostic shows no clear gain.

| Forecast | Mean QLIKE loss, lower is better |
| --- | ---: |
| Trailing variance | 0.16670 |
| Raw DVOL | 0.09611 |
| HAR | 0.12548 |
| HAR + DVOL | 0.11123 |

HAR + DVOL minus HAR has mean QLIKE difference -0.01424 and a descriptive 95% interval [-0.03490, +0.00505]. Its estimated effective sample size (ESS), a dependence-adjusted measure of information, is 51.71; the raw-DVOL comparison has ESS 53.44. Both fall below the preset reporting rule of 60, which is a design choice rather than a validated significance cutoff. Neither regression reaches its fixed positivity floor.

[![First page of my LaTeX research paper, with the abstract and matched BTC forecast results](report/latex/paper-preview.png)](report/latex/main.pdf)

## Limits and status

The historical findings are exploratory. The original open, high, low and close (OHLC) input check fails on an unused DVOL open; the three audited closes match. Chronological fitting and mean QLIKE rankings alone establish neither statistical superiority nor a trading result. The paper's [limitations section](report/latex/main.pdf) explains the data, dependence and volatility-budget assumptions.

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
