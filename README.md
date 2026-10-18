# Implied odds

[![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

## Status

8 of 8 implementation phases complete. Data collection and research are still in progress.
I've tested the pipeline and report figures offline, with separate synthetic examples.
The included ten-minute market capture still has a closed IV gate.
I haven't collected settled research forecasts or measured market calibration yet.

## Run it

I use Python 3.11 or later. From this repo, I install and rebuild offline:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/reproduce.py
.\.venv\Scripts\python.exe -m implied_calibration.cli ingest --input recordings/smoke-01 --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli report --db data/research.sqlite --output results/report
```

On macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/reproduce.py
.venv/bin/python -m implied_calibration.cli ingest --input recordings/smoke-01 --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli report --db data/research.sqlite --output results/report
```

I open `results/reproduction/index.html` to view the two reports. The market report
preserves the capture's gate; the synthetic report demonstrates all six figures.
Reproduction returns 0 when the rebuild completes. `report` returns 2 for
"insufficient data". My [live recording and scoring commands](report/research-note.md#running-the-pipeline)
are separate from this offline rebuild.

## Method and data

I record production Deribit BTC inverse options, keep missing sides as nulls,
convert BTC premiums with each expiry's forward, and fit checked SVI smiles.
I freeze accepted CDFs and baselines at 24-hour and seven-day leads, then score
them against official delivery prices. My headline comparison is 24-hour USD CRPS
against the same-snapshot ATM lognormal, with dependence-aware uncertainty.

Each retained gzip JSON-lines record keeps the response body, endpoint, parameters
and local millisecond timings. I include the unchanged public-market sample and
heartbeat under `recordings/`, with hashes in its [manifest](recordings/manifest.json).
The [research note](report/research-note.md) gives the observed engineering results,
format details, rejection reasons and numerical choices.

## Limits

Risk-neutral probabilities include risk premia, so they can differ from realised
frequencies. I use one venue and a short sample; overlapping outcomes reduce
effective sample size. I suppress inference below the fixed ESS minimum.

Settlement is a 30-minute index TWAP. Thin option wings need extrapolation, and
the GARCH CDF uses a finite simulation approximation. Quotes and the index aren't
an atomic snapshot. Crypto funding, jumps and market structure also affect comparisons.

## Files

- `src/implied_calibration/`: my recorder, database, pricing, forecasts, inference and reports.
- `tests/`: my offline synthetic checks; `.github/workflows/tests.yml` runs them on Windows and Linux.
- `scripts/reproduce.py`: my offline rebuild; the other scripts keep the earlier synthetic examples and disk estimate.
- `recordings/`: my small public-market sample and source hashes.
- `protocol/`: my fixed choices and committed locks.
- `report/research-note.md`: my method, engineering evidence and research limits.

## Next

I'll investigate the IV flag, collect qualifying snapshots and hourly history,
then freeze and score enough expiries to study calibration with the declared checks.

I haven't picked a licence yet.
