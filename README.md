# Does DVOL add information beyond HAR?

[Five-page paper](report/research-paper.pdf) · [Results and diagnostics](report/incremental-results.json) · [![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

I'm testing whether option-implied BTC variance adds information to a price-history forecast.
I compared HAR with the same regression plus DVOL on 1,357 matched 30-day forecasts.
I found lower average QLIKE, but no clear incremental gain after uncertainty or in a fixed risk-budget decision.

| Forecast | Mean QLIKE, lower is better |
| --- | ---: |
| Trailing variance | 0.16670 |
| Raw DVOL | 0.09611 |
| HAR | 0.12548 |
| HAR + DVOL | 0.11123 |

I get an augmented-minus-HAR difference of -0.01424, with descriptive 95% interval [-0.03490, +0.00505] and ESS 51.7 against my threshold of 60. Neither regression hit its fixed positivity floor.

![Real historical BTC variance forecasts and cumulative paired losses; exploratory comparison](report/incremental-variance.svg)

## Status

I keep the historical study exploratory. The original vendor all-OHLC gate still fails on an unused DVOL open; the three audited closes match.
I locked a future year starting 21 October 2026, but its inference stress tests show poor size and no demonstrated useful power, so I disabled formal superiority claims.
I have zero settled SVI forecasts. I publish [local scoreboard snapshots](report/scoreboard.html); collection stops after supervised validation because this PC isn't always on.

## Run it

With Python 3.12 on Windows, I rebuild offline:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-research.txt -e ".[research,dev]"
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/build_research.py
```

On macOS or Linux, I replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`. I open `results/research/research-paper.pdf` after the rebuild.

I keep the [timing, input format and live commands](report/research-note.md), [versioned plans](protocol/incremental-variance-v1.json), [inference amendment](protocol/inference-validation-v1.json), [retained public inputs](recordings/historical-variance/manifest.json), source in `src/implied_calibration/`, and checks in `tests/`.

I'll collect genuinely frozen outcomes on an always-on host before making a prospective research claim. I don't infer trading profit from these losses.

I release my code under the [MIT licence](LICENSE). The retained Deribit responses keep their original source provenance.
