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

I ingest unchanged recordings in time order with a fixed `--max-days-to-expiry`,
default 400. I use a new database to change that limit or rebuild metadata joins.
Corrupt JSON lines and truncated tails get counted; a bad gzip CRC stops import.
Coverage uses declared session windows, shows incomplete sessions and downtime,
and flags counts below half the prior 20-snapshot median after 3 observations.
Source counts precede the expiry filter; per-expiry counts use the latest retained
snapshot.

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

## Quote cleaning and IV checks

I use undiscounted Black-76 with BTC premium = USD premium / forward, decimal IV,
and 365-day years. I keep interest_rate for diagnostics and don't discount with it.
I invert bid, ask, and the arithmetic midpoint PRICE separately. My tail calculation
uses a positive payoff integral when the normal-CDF terms nearly cancel. I check it
against a separate 100-digit Decimal calculation, including prices around 1e-200.
Every successful IV must reprice within 1e-10 relative error. Prices rounded to
intrinsic or zero after floating-point underflow get explicit failure reasons.

I use the median underlying_price from verified active quotes as one expiry forward,
before filtering books or choosing the OTM side. I reject quote forwards more than
0.1% away. I use puts below F and calls at or above F; ATM doesn't count as a strike
strictly above F. Tick steps apply only strictly above their BTC price thresholds,
following [Deribit's tick rules](https://support.deribit.com/hc/en-us/articles/25944779257373-6-July-2023-TS).
I use the metadata version attached at ingestion, never a later tick observation.

My [draft settings](../protocol/protocol.json) require at least 6 hours to expiry,
a midpoint of at least 2 ticks, spread/mid at most 0.5, 6 distinct usable strikes,
and 2 strictly on each side of F. These are initial research choices, not validated
optimal filters. The report includes the settings and their hash. It doesn't lock
the evaluation protocol. Each quote keeps its pre-expiry-filter status and final
status; reason counts can include several reasons for one quote.

I compare midpoint IV and mark-price IV separately with mark_iv/100. The latter
uses each quote's own forward. Deribit identifies mark_iv as the IV of mark_price
and underlying_price as the input to IV calculations in its [summary field definitions](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency).
I require 6 comparisons and flag either signed median beyond 0.5 vol points per
group or across the capture. I keep the progression gate closed for a flagged group,
even if the global median passes. These medians are descriptive engineering checks.

On the same local capture, I classified all 9,420 stored rows across 120 groups.
I found 3,298 quote-level candidates and 3,266 usable rows in 102 groups.
17 groups were unusable and one stayed flagged for investigation.
The candidate midpoint-minus-mark median was -0.021773 vol points; the mark-price
median was +0.000122. Neither aggregate median breached the 0.5 threshold.

The flagged 17 October expiry group had 5 candidate strikes below F and one above,
so it also failed the side-count rule. Its midpoint-minus-mark median was -0.569744
vol points, while the mark-price median was +0.068910. Using each quote's own F
changed midpoint IV by at most 0.01823 vol points, so the common F doesn't explain
the breach. The 81,500 put's mark was 0.00267248 BTC, above its 0.0025 ask; its
midpoint was 0.0024. This supports a midpoint-versus-mark price gap in that capture.
I keep the flag and `phase4_ready=false`; I haven't relaxed the filters to clear it.
The full row-level diagnostic stays local with the unpublished capture.

## SVI fitting

I fit raw SVI total variance, w(k) = a + b(rho(k-m) + sqrt((k-m)^2 + s^2)).
I use analytic first and second derivatives in the butterfly diagnostic, following
[Gatheral and Jacquier](https://arxiv.org/html/1204.0646). My bounded transform fits
the positive minimum variance, a slope fraction, rho, m and s. It allows negative a
and keeps both wing slopes below 2 with a 1e-6 margin. Passing the parameter bounds
alone doesn't establish that the fitted smile passes the butterfly check.

I run 10 seeded TRF starts and fit IV residuals. Weights start at inverse squared
bid/ask IV spread, with a 0.005 decimal-IV floor, then get capped so one quote has
at most 20% of the total weight. I select the lowest weighted error among converged
constraint-valid starts before checking butterflies; I don't select a different
start just to hide a failed check. Failed starts stay in the result message.

I check g(k) against -1e-8 over at least 6 ATM standard deviations, observed strikes,
and local grids around m and the variance minimum. I compare all butterfly-eligible
expiry pairs within a snapshot on one shared grid. Calendar crossings get flags;
I don't repair them. My draft primary rule requires both checks to pass. A single
eligible expiry has zero calendar pairs, which isn't evidence about a term structure.
Finite grids can miss violations between nodes or beyond the checked range.

My labelled synthetic example has 41 strikes, T=0.5 years and raw parameters
a=0.02, b=0.15, rho=-0.35, m=0.03, s=0.25. I recovered its noiseless smile with
RMSE below 1e-10 vol points, all fitted strikes inside the generated bid/ask spread,
and min g about 0.260893 on the check grid. Separate tests cover seeded noise,
negative a, narrow s bounds, deliberate butterfly violations and calendar crossings.
These checks don't establish performance on recorded market quotes.

The `fit` command re-cleans captured rows as of the requested cutoff before fitting.
It keeps every expiry group in its output, including rejected or failed fits.
My ten-minute capture still has `phase4_ready=false`, so its groups remain blocked
before the optimiser is called. I haven't fitted those recorded smiles or changed
the Phase 3 thresholds. The synthetic example runs without that capture.

## Implied CDF

I differentiate the undiscounted Black call across strikes while keeping the SVI
slice and forward fixed. With k=ln(K/F), w from SVI and d2=(-k-w/2)/sqrt(w), I use
survival = N(d2) - phi(d2) w'(k)/(2 sqrt(w)). I compute both tails directly with
signed log sums, then take the larger probability as the smaller tail's complement.
That keeps tiny survival probabilities when the CDF rounds to one. Total variance
already contains T; I store T for the slice and don't multiply w by T again.
My tests compare the digital with finite differences of Black calls at three step
sizes and check the exact flat-vol lognormal limit.

I store SVI parameters, F, T, observed log-strike bounds, settings and diagnostics.
`ImpliedCDF.from_record(record).cdf_at(price)` rebuilds the same curve. I keep the
numerical grid in memory. I don't save it in the distribution record. Each query
reports whether its price is outside the observed usable strike range.
Extrapolated mass is CDF at the lowest observed strike plus survival at the highest.
I don't claim calibration in the extrapolated wings or inside the observed range.

My draft grid starts at k=-8 to 8 and doubles to at most -64 to 64, including local
nodes around m and the variance minimum. I coalesce nearly identical floating-point
nodes. I require each raw boundary tail below 1e-10 and each omitted mean contribution
below 1e-8 of F. I integrate survival with the price Jacobian F exp(k) and check the
mean against F within 2e-6 relative error. For the analytic curve, I also compare the
truncated integral with C(L)-C(U) and report omitted contributions F-C(L) and C(U).
The implementation uses L-Put(L) for the left contribution to avoid cancellation.
These tail identities assume a valid slice with the required endpoint limits.

If I detect a decreasing CDF, increasing survival or out-of-range probability,
I apply equal-weight PAVA on the fixed nodes and project its values onto [0,1].
I record isotonic, bounds and endpoint corrections, their total maximum, pooling
and changed-point counts. I anchor repaired endpoints to zero and one, use linear
interpolation in log-moneyness, and report that finite support explicitly.
I integrate this repaired curve exactly per segment and check its mean again.
I don't rescale it to force the mean to F. Repaired or unresolved curves stay
visible in the output and aren't counted in `n_cdf_ok`.

My synthetic CDF example uses a=0.02, b=0.15, rho=-0.35, m=0.03, s=0.25,
F=80,000, T=0.5 and an observed k range of -0.3 to 0.3. It needs no repair.
Its mean check differs from F by about 6.63e-7 relatively, with raw truncated
probability about 5.06e-30 and extrapolated mass about 20.57%. These are generated
inputs and numerical checks. A separate synthetic test near the Lee slope limit
leaves more than half the mean beyond k=64 and correctly reports unresolved tails.
Tiny tail probability alone doesn't establish that the omitted mean is small.

The `cdf` command reuses the as-of cleaning and fit gates, retains every group and
returns exit 2 when no CDF passes. My saved capture produces zero distributions;
its 120 groups remain blocked before fitting. Finite grids can miss violations,
and PAVA doesn't preserve option prices or the martingale mean. I haven't added
density evaluation or log scores in this phase.

I can still run the synthetic smile plot from Phase 4 with
`.venv/bin/python scripts/synthetic_svi.py --output results/synthetic-svi.json --plot results/synthetic-svi.png`
on macOS or Linux, or replace the interpreter with `.\.venv\Scripts\python.exe`
in PowerShell. The README gives the equivalent CDF example commands.

## Research still to do

I plan to lock the protocol and freeze forecasts in Phase 6, compare baselines
and uncertainty in Phase 7, then finish the report and reproduction work in
Phase 8. I've brought offline CI forward for this release.

I haven't fixed the protocol choices or collected a set of settled forecasts.
I'll commit the locked protocol and its hash before the first evaluated expiry
occurs. I'll freeze each forecast using only information available at its decision
timestamp, then score it after the official delivery price exists. I won't draw
calibration conclusions before I have enough recorded expiries.
