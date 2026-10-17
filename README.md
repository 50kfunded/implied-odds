# Implied odds

[![tests](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/50kfunded/implied-odds/actions/workflows/tests.yml)

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

## Status

3 of 8 phases complete. Phase 4 next: SVI smile fitting.
I've built recording, ingestion, coverage, Black-76 pricing, and quote cleaning.
I've checked these on a ten-minute capture; its Phase 4 data gate stays closed.
I haven't fitted distributions, frozen forecasts, or measured calibration yet.

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

I ingest an unchanged recording with a fixed `--max-days-to-expiry`, default 400.
I use a new database to change that limit or rebuild earlier metadata joins.
I ingest folders in time order; later imports don't revise earlier snapshot joins.
Corrupt JSON lines and truncated tails get counted; a bad gzip CRC stops import.
Coverage uses declared session windows, shows incomplete sessions and downtime,
and flags counts below half the prior 20-snapshot median after 3 observations.
Source counts precede the expiry filter; per-expiry counts use the latest retained
snapshot. The schema and as-of reads live in [store.py](src/implied_calibration/store.py).

I clean through the latest capture, or pass `--as-of-ms` for an earlier cutoff.
The JSON keeps every stored quote and its reasons. Exit 2 keeps an IV investigation
or missing comparison visible. My [research note](report/research-note.md) explains the flagged sample.

## Files

- `src/implied_calibration/`: my recording, database, Black-76, cleaning, and CLI code.
- `tests/`: my offline checks using synthetic responses, clocks, and temporary databases.
- `scripts/estimate_disk.py`: my disk estimate from observed recording time.
- `report/research-note.md`: my research question, data checks, and limits so far.
- `.github/workflows/tests.yml`: my offline tests on Windows and Linux, Python 3.11 and 3.12.
- `protocol/protocol.json`: my draft cleaning and diagnostic thresholds, with no evaluation lock yet.

## Next

I'll add SVI smile fitting in Phase 4, then distributions,
frozen forecasts, scoring, baselines, and reporting.
I'll update the research note as I complete each phase.

I haven't picked a licence yet.
