# Implied odds

I'm building a study of BTC option probabilities. When options imply a given
risk-neutral probability of settling below a strike, I want to compare it with
the realised frequency and see how that changes across expiries.

I'm starting with the recorder. I've added the API client, raw files,
heartbeat records, scheduling, and offline tests.
I haven't validated a live recording yet.
The distribution fitting and scoring are planned for later phases.

## Run it

I use Python 3.11 or later. From this repo, I create an environment and install:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.\.venv\Scripts\python.exe scripts/estimate_disk.py data/smoke-01
```

On macOS or Linux, I use these commands:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.venv/bin/python scripts/estimate_disk.py data/smoke-01
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

I reject schema changes explicitly. Optional quote fields stay untouched here;
instrument parsing and ingestion belong to the next phase.
I retry network errors, HTTP 429/5xx, and JSON-RPC errors up to three times.
Schema failures and testnet responses stop that endpoint's attempt sequence.
The other endpoints still run, and each failure stays visible.
After a backward clock correction, I keep the next unused UTC slot. Recording
can pause until the clock catches up; I record that correction in the heartbeat.

## Files

- `src/implied_calibration/client.py`: my public client and response checks.
- `src/implied_calibration/recorder.py`: my polling loop and heartbeat records.
- `src/implied_calibration/raw.py`: my gzip writer and crash-tolerant reader.
- `src/implied_calibration/schedule.py`: my clock-independent slot arithmetic.
- `src/implied_calibration/cli.py`: my command entry point.
- `tests/test_phase1.py`: my offline checks using synthetic responses and clocks.
- `scripts/estimate_disk.py`: my disk estimate from observed recording time.

## Next

I'll add instrument parsing and SQLite ingestion in Phase 2, then quote cleaning,
smile fitting, distributions, frozen forecasts, scoring, baselines, and reporting.
I won't draw calibration conclusions before I have enough recorded expiries.
Risk-neutral probabilities can differ from realised frequencies because of risk premia.

I haven't picked a licence yet.
