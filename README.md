# Implied odds

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

I've built the recorder, instrument parser, SQLite ingestion, and coverage report.
I've checked a ten-minute live run with no failures or gaps,
then re-imported it without adding rows.
The offline tests cover both phases. Fitting and scoring are planned for later.

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

I append each line as a complete gzip member and flush the write. Heartbeat JSONL
records show attempts, cycles, skipped slots, clock changes, and session outcomes.
The reader reports and discards an incomplete final member, retaining earlier ones.
I refuse to append behind a damaged member. After a crash or stale lock, I use a
fresh output path such as `data/smoke-02` and keep the old files.

I reject schema changes explicitly and preserve missing quote fields as nulls.
I retry network errors, HTTP 429/5xx, and JSON-RPC errors up to three times.
Schema failures and testnet responses stop that endpoint's attempt sequence.
The other endpoints still run, and each failure stays visible.
After a backward clock correction, I keep the next unused UTC slot. Recording
can pause until the clock catches up; I record that correction in the heartbeat.

## SQLite and coverage

I keep BTC prices, percent mark IVs, and nulls without converting their units.
Instrument identities use an English month map and 08:00 UTC expiries. Metadata
and index joins only use observations fetched by the quote snapshot's timestamp.
My first snapshot has no earlier metadata or index, so it's marked partial;
its parsed quotes stay stored but are excluded from verified reads.

I ingest an unchanged recording with a fixed `--max-days-to-expiry`, default 400.
I use a new database to change that limit or rebuild earlier metadata joins.
I ingest folders in time order; later imports don't revise earlier snapshot joins.
Corrupt JSON lines and truncated tails get counted; a bad gzip CRC stops import.
Coverage uses declared session windows, shows incomplete sessions and downtime,
and flags counts below half the prior 20-snapshot median after 3 observations.
Source counts precede the expiry filter; per-expiry counts use the latest retained
snapshot. The schema and as-of reads live in [store.py](src/implied_calibration/store.py).

## Files

- `src/implied_calibration/`: my client, recorder, parser, database, ingestion, and coverage commands.
- `tests/`: my offline checks using synthetic responses, clocks, and temporary databases.
- `scripts/estimate_disk.py`: my disk estimate from observed recording time.

## Next

I'll add quote cleaning in Phase 3, then smile fitting, distributions,
frozen forecasts, scoring, baselines, and reporting.
I won't draw calibration conclusions before I have enough recorded expiries.
Risk-neutral probabilities can differ from realised frequencies because of risk premia.

I haven't picked a licence yet.
