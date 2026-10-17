# Implied odds

[![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

## Status

4 of 8 phases complete. Phase 5 next: implied distributions.
I've checked SVI fitting on synthetic smiles, including bad fits and calendar crossings.
My ten-minute capture still has a closed data gate, so I haven't fitted its smiles.
I haven't extracted distributions, frozen forecasts, or measured calibration yet.

## Run it

I use Python 3.11 or later. From this repo, I create an environment and install:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.\.venv\Scripts\python.exe scripts/estimate_disk.py data/smoke-01
.\.venv\Scripts\python.exe -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli coverage --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.\.venv\Scripts\python.exe -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.\.venv\Scripts\python.exe scripts/synthetic_svi.py --output results/synthetic-svi.json --plot results/synthetic-svi.png
```

On macOS or Linux, I use these commands:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.venv/bin/python scripts/estimate_disk.py data/smoke-01
.venv/bin/python -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli coverage --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.venv/bin/python -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.venv/bin/python scripts/synthetic_svi.py --output results/synthetic-svi.json --plot results/synthetic-svi.png
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
`fit` keeps the data gate visible. The synthetic example runs independently of that gate.

## Files

- `src/implied_calibration/`: my recording, database, pricing, cleaning, SVI, and CLI code.
- `tests/`: my offline checks using synthetic responses, clocks, and temporary databases.
- `scripts/`: my disk estimate and labelled synthetic SVI example.
- `report/research-note.md`: my research question, data checks, and limits so far.
- `.github/workflows/tests.yml`: my offline tests on Windows and Linux, Python 3.11 and 3.12.
- `protocol/protocol.json`: my draft cleaning and fitting choices, with no evaluation lock yet.

## Next

I'll add implied distributions in Phase 5, then frozen forecasts,
scoring, baselines, and reporting.
I'll update the research note as I complete each phase.

I haven't picked a licence yet.
