# Implied odds

[![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

## Status

7 of 8 phases complete. Phase 8 next: reporting and reproduction.
I've tested baselines, frozen scoring and dependence checks on synthetic data.
My ten-minute capture still has a closed data gate, so I haven't extracted its distributions.
I haven't collected settled research forecasts or measured calibration yet.

## Run it

I use Python 3.11 or later. From this repo, I create an environment and install:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.\.venv\Scripts\python.exe -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli predict --db data/research.sqlite --output results/predictions.json
.\.venv\Scripts\python.exe -m implied_calibration.cli settle --db data/research.sqlite --fetch --raw-output data/delivery-01
.\.venv\Scripts\python.exe -m implied_calibration.cli score --db data/research.sqlite --output results/scores.json
.\.venv\Scripts\python.exe -m implied_calibration.cli infer --db data/research.sqlite --output results/inference.json
.\.venv\Scripts\python.exe scripts/synthetic_cdf.py --output results/synthetic-cdf.json --plot results/synthetic-cdf.png
```

On macOS or Linux, I use these commands:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.venv/bin/python -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli predict --db data/research.sqlite --output results/predictions.json
.venv/bin/python -m implied_calibration.cli settle --db data/research.sqlite --fetch --raw-output data/delivery-01
.venv/bin/python -m implied_calibration.cli score --db data/research.sqlite --output results/scores.json
.venv/bin/python -m implied_calibration.cli infer --db data/research.sqlite --output results/inference.json
.venv/bin/python scripts/synthetic_cdf.py --output results/synthetic-cdf.json --plot results/synthetic-cdf.png
```

I omit `--seconds` for continuous recording. The default interval is 300 seconds;
the minimum is 10. Polls start at UTC epoch-aligned boundaries. A request already
in progress can finish after the duration expires. Ctrl+C records an interruption.
The command returns 1 for failed attempts, gaps, clock changes, or no cycles.

## What I keep

I call the BTC option summary and `btc_usd` index each cycle, plus instruments
on the first cycle and once in each UTC hour. I only use the production API.
The index response isn't a settlement price, and the quotes aren't atomic.

Each `raw/YYYY-MM-DD.jsonl.gz` line contains the parsed response body unchanged,
endpoint, params, HTTP status, local UTC millisecond timings, and attempt outcome.
I keep Deribit's microsecond server timings inside the original body.
Network failures have a null body and HTTP status; invalid JSON also keeps its text.
I follow the [Deribit JSON-RPC response format](https://docs.deribit.com/articles/json-rpc-overview).
The request and schema rules live in [client.py](src/implied_calibration/client.py).

I keep heartbeat JSONL beside the raw files. My retry, crash recovery, clock,
and validation details are in the [research note](report/research-note.md).

## SQLite and coverage

I keep BTC prices, percent mark IVs, and nulls without converting their units.
Instrument identities use an English month map and 08:00 UTC expiries. Metadata
and index joins only use observations fetched by the quote snapshot's timestamp.

I use a fixed ingestion horizon and report declared-session coverage, missing slots,
and low instrument counts. My [research note](report/research-note.md) gives the limits and counting rules.
The schema and as-of reads live in [store.py](src/implied_calibration/store.py).

I clean through the latest capture, or pass `--as-of-ms` for an earlier cutoff.
The cleaning JSON keeps every stored quote and its reasons. Exit 2 from `clean` or
`fit` or `cdf` keeps the data gate visible. `predict` and `score` also return 2 when nothing qualifies.
I store CDF parameters and report repairs and extrapolated mass in the [research note](report/research-note.md#implied-cdf).
I run `predict` within 10 minutes of each due lead time. The [fixed protocol](protocol/protocol.json) starts with 26 October 2026 expiries.

## Files

- `src/implied_calibration/`: my capture, database, fitting, frozen forecasts, baselines and inference.
- `tests/`: my offline checks using synthetic responses, clocks, and temporary databases.
- `scripts/`: my disk estimate and labelled synthetic SVI and CDF examples.
- `report/research-note.md`: my research question, data checks, and limits so far.
- `.github/workflows/tests.yml`: my offline tests on Windows and Linux, Python 3.11 and 3.12.
- `protocol/`: my fixed research choices, baseline extension and committed hash locks.

## Next

I'll finish reporting and reproduction in Phase 8. My [baseline and inference choices](report/research-note.md#baselines-and-inference) are fixed.
I'll update the research note as I complete each phase.

I haven't picked a licence yet.
