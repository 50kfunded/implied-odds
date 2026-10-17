# Research note

I'm studying whether BTC option-implied probabilities line up with realised
settlement frequencies across expiries. Risk-neutral probabilities can differ
from realised frequencies because of risk premia. I won't treat a difference
as evidence of a trading edge.

## Evidence so far

I've checked the recording and ingestion pipeline on a local ten-minute capture
from 16 October 2026. These are engineering checks. Repeated quotes aren't
independent outcomes, and this capture doesn't establish calibration.
I haven't published the raw recording or database, so the numbers below describe
my local validation. The repo's offline tests use synthetic data.

| Check | What I observed |
| --- | --- |
| Recording | 10 expected minute slots, 10 received, no gaps or failed attempts |
| Responses | 22 HTTP 200 responses: 10 summaries, 10 index prices, 2 instrument refreshes |
| Instruments | 942 unique identities, 1,884 timestamped metadata observations |
| Ingestion | 10 snapshots and 9,420 quote rows |
| As-of reads | 8,478 verified quote rows; 942 rows in the first partial snapshot |
| Missing fields | 617 missing bids stayed SQL NULL |
| Re-import | No new rows in any table |
| Storage | Foreign keys, integrity, WAL, and required quote indexes passed |
| Coverage | No missing slots, inferred slot assignments, or low-count warnings in this capture |
| Future results | No delivery prices, frozen predictions, or scores yet |

I recorded with commit `b04d316144a642a867000e701d6e085c4293f63e` and checked
ingestion at `395170301c0e0796692b658edf74ea79ee52fc5f`. I checked that ingestion
left the raw file unchanged. Its SHA-256 is
`6a399d0bccd85d81c25070cae593de4ac111859f67e2bb68bebd4cc49df51f77`.

My first snapshot has 942 missing-metadata issues and one missing-index issue.
I fetched its quotes before the first metadata and index observations arrived,
so I keep the quotes but exclude them from verified reads. I don't join later
observations back into that snapshot. Later imports don't revise earlier joins.

## Recording rules

I append each raw line as a complete gzip member and flush the write. Heartbeat
records show attempts, cycles, skipped slots, clock changes, and session outcomes.
The reader reports and discards an incomplete final member, retaining earlier ones.
I refuse to append behind a damaged member. After a crash or stale lock, I use
a fresh output path such as `data/smoke-02` and keep the old files.

I reject schema changes explicitly and preserve missing quote fields as nulls.
I retry network errors, HTTP 429/5xx, and JSON-RPC errors up to three times.
Schema failures and testnet responses stop that endpoint's attempt sequence.
The other endpoints still run, and each failure stays visible.
After a backward clock correction, I keep the next unused UTC slot. Recording
can pause until the clock catches up; I record that correction in the heartbeat.
My response envelope follows the [Deribit JSON-RPC format](https://docs.deribit.com/articles/json-rpc-overview).

## Research still to do

I plan to clean quotes and check Black-76 implied volatilities in Phase 3,
fit SVI smiles in Phase 4, and extract risk-neutral distributions in Phase 5.
I plan to lock the protocol and freeze forecasts in Phase 6, compare baselines
and uncertainty in Phase 7, then finish the report and reproduction work in
Phase 8. I've brought offline CI forward for this release.

I haven't fixed the protocol choices or collected a set of settled forecasts.
I'll commit the locked protocol and its hash before the first evaluated expiry
occurs. I'll freeze each forecast using only information available at its decision
timestamp, then score it after the official delivery price exists. I won't draw
calibration conclusions before I have enough recorded expiries.
