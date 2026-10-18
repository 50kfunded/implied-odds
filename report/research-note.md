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

My [fixed settings](../protocol/protocol.json) require at least 6 hours to expiry,
a midpoint of at least 2 ticks, spread/mid at most 0.5, 6 distinct usable strikes,
and 2 strictly on each side of F. These are initial research choices, not validated
optimal filters. The report includes the settings and their hash. My committed
lock fixes the evaluation choices below. Each quote keeps its pre-expiry-filter status and final
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
I don't repair them. My primary rule requires both checks to pass. A single
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

My grid starts at k=-8 to 8 and doubles to at most -64 to 64, including local
nodes around m and the variance minimum. I coalesce nearly identical floating-point
nodes. I require each raw boundary tail below 1e-10 and each omitted mean contribution
below 1e-8 of F. I integrate survival with the price Jacobian F exp(k) and check the
mean against F within 2e-6 relative error. For the analytic curve, I also compare the
truncated integral with C(L)-C(U) and report omitted contributions F-C(L) and C(U).
The implementation uses L-Put(L) for the left contribution to avoid cancellation.
These tail identities assume a valid slice with the required endpoint limits.
I refine the integration grid up to four times if the forward check is unresolved,
without changing the CDF query or repair grid. This resolves narrow synthetic slices
that a coarse irregular integration grid would otherwise reject.

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
and PAVA doesn't preserve option prices or the martingale mean. My scoring convention
for accepted analytic CDFs is below.

I can still run the synthetic smile plot from Phase 4 with
`.venv/bin/python scripts/synthetic_svi.py --output results/synthetic-svi.json --plot results/synthetic-svi.png`
on macOS or Linux, or replace the interpreter with `.\.venv\Scripts\python.exe`
in PowerShell. The README gives the equivalent CDF example commands.

## Frozen forecasts and scoring

I've fixed the [protocol](../protocol/protocol.json) before the first eligible decision.
My first eligible expiry is 26 October 2026 at 08:00 UTC; its seven-day target is
19 October at 08:00 UTC. I keep the earlier capture as engineering evidence.
The [lock](../protocol/protocol.lock.json) records the canonical protocol hash,
creation timestamp and the commit containing its settings. The loader checks a
reachable commit containing both matching files before that first target.
Git timestamps are editable provenance. Publishing the commit before the target
also leaves an external record; it doesn't prove a forecast was made on time.

I use 24-hour and seven-day leads. For each due lead, I select the latest whole
snapshot at or before expiry minus lead, with a gap of at most 10 minutes. I don't
search backward for a better fit. T stays tied to that selected snapshot.
I keep the existing cleaning, IV, butterfly, calendar and CDF gates.
I allow 10 minutes after the target to finish freezing. The CLI records completion
time and rejects a forecast if calculation finishes late. I record no_snapshot,
late_freeze and every rejection instead of filling a missing forecast later.
I discover expiry identities only from metadata available at the as-of cutoff.

I store the selected snapshot, gap, SVI parameters, F, T, fit and CDF diagnostics,
protocol hash, Git commit, creation time and content hash. Repeat runs return the
stored row without refitting. SQLite guards block updates, deletes and replacements.
An interrupted batch rolls back. Schema 2 adds evaluation tables to earlier databases;
it doesn't revise captured quotes. I check forecasts and scores from a clean checkout.

My primary hypothesis is that SVI lowers mean USD CRPS against the same-snapshot
ATM lognormal at the 24-hour lead. I reserve headline inference for that comparison.
The declared baseline list also includes trailing realised-vol lognormal and
GARCH(1,1)-t. My stationary-bootstrap choices are 2,000 resamples, seed 1729 and
expected block length 14 expiries, with minimum effective sample size 60.
I keep these choices fixed in the baseline extension below.

I fetch `public/get_delivery_prices` with btc_usd, offset and count, retaining every
attempt in raw gzip JSON-lines. Pages wait at least 10 seconds between successful
requests. I reject changing totals, overlaps, empty nonterminal pages and overruns.
The [official response](https://docs.deribit.com/api-reference/market-data/public-get_delivery_prices)
contains data rows with date and delivery_price, plus records_total. I validate this
schema centrally and map each date to 08:00 UTC. Retained partial pages can still
supply delivery observations; importing them doesn't establish complete history.
I compare duplicate prices and stop on conflicts. I don't overwrite the first value.

I score only after the official delivery row exists and was fetched by the scoring
cutoff. Missing delivery prices stay pending without final score rows. I never use
spot as settlement. Deribit uses the [07:30 to 08:00 UTC index TWAP](https://support.deribit.com/hc/en-us/articles/29734325712413-Settlement).
That averaging slightly reduces effective terminal variance at short maturities;
I document it without a v1 correction.

I freeze a finite scoring support from each accepted CDF's resolved check limits.
I collapse the small omitted tails onto boundary atoms and record the policy,
atom masses, omitted raw probability and tail-mean diagnostics. The Phase 5
analytic CDF still continues into its SVI wings; this scoring wrapper is explicit.
Outside frozen support, raw PIT is zero or one with a tail-exceedance flag.
I keep that PIT and a separate transformed value clipped to [1e-6,1-1e-6].

I integrate CRPS in USD, splitting at the delivery price and median. I use direct
survival in the upper tail and the price Jacobian on log grids. A wide grid plus
separate center and shoulder grids resolves narrow distributions. I report the
change under refinement and reject unresolved scores. Tests compare with closed-form
normal and lognormal CRPS, including far-tail outcomes and small total variance.
Convergence estimates remain numerical diagnostics, not certified error bounds.

My log score is negative log density on ln(delivery/frozen forward). I don't add
a density floor. Outside-support zero density is an explicit infinite loss; at
boundary atoms I leave the continuous log score undefined. I reject nonpositive
interior density. Class summaries retain infinite and undefined counts.
I compute central 50%, 80% and 90% interval coverage with inclusive boundaries.
I derive expiry class in quarterly, monthly, weekly, daily precedence, following
the [contract introduction policy](https://support.deribit.com/hc/en-us/articles/25944688876957),
and report scores and coverage separately by class and lead.

My offline tests cover successful forecasts at both leads, missing and late freezes,
future-row invariance, immutable payloads, pending delivery then scoring, conflicting
settlement prices, PIT simulation and score identities. These use synthetic inputs.
I haven't created research forecasts or scores from the saved market capture.

I can run the earlier inspection commands with either interpreter from the README:

```powershell
.\.venv\Scripts\python.exe scripts/estimate_disk.py data/smoke-01
.\.venv\Scripts\python.exe -m implied_calibration.cli coverage --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.\.venv\Scripts\python.exe -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.\.venv\Scripts\python.exe -m implied_calibration.cli cdf --db data/research.sqlite --output results/cdf.json
.\.venv\Scripts\python.exe -m implied_calibration.cli settle --db data/research.sqlite --input data/delivery-01
```

```sh
.venv/bin/python scripts/estimate_disk.py data/smoke-01
.venv/bin/python -m implied_calibration.cli coverage --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.venv/bin/python -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.venv/bin/python -m implied_calibration.cli cdf --db data/research.sqlite --output results/cdf.json
.venv/bin/python -m implied_calibration.cli settle --db data/research.sqlite --input data/delivery-01
```

## Research still to do

I'll finish the report and reproduction work in Phase 8. I've brought offline CI forward.

I haven't collected a set of settled research forecasts. I'll freeze each forecast
using only information available at its decision timestamp, then score it after
the official delivery price exists. I won't draw calibration conclusions before
I have enough recorded expiries and dependence-aware checks.

## Baselines and inference

I've fixed the remaining choices in [baselines.json](../protocol/baselines.json),
with its own [committed lock](../protocol/baselines.lock.json) tied to the original
protocol hash. I haven't changed the original protocol or lock. I use the same
first expiry, 26 October 2026 at 08:00 UTC. Both locks must have a reachable Git
witness before the first seven-day target on 19 October at 08:00 UTC.
I can't add missing baselines to an already frozen SVI forecast.

I compare the same official BTC delivery price, using each selected snapshot's
F, T and finite SVI scoring bounds. I use the same PIT, USD CRPS, log-return
log score and interval coverage rules for every model.

| Baseline | My volatility input | My drift relative to frozen F |
| --- | --- | --- |
| Same-snapshot ATM lognormal | Fitted SVI total variance at k=0 | Log mean -w/2, so the raw lognormal price mean is F |
| Trailing realised lognormal | Mean squared hourly log returns over 720 returns, scaled by T times 365 times 24 | Log mean -w/2 |
| GARCH(1,1)-t | ZeroMean fit to 2,160 hourly log returns using arch | Zero log drift, with symmetric innovations around frozen F |

My ATM choice is a flat-vol ablation of the accepted SVI slice. I don't substitute
a different quote or a nearest strike. My realised estimator keeps the second
moment without estimating a short-window return drift.

I retain `public/get_tradingview_chart_data` pages for BTC-PERPETUAL with
`resolution="60"`, request intervals of at most 720 hours and ten seconds between
pages. The [Deribit parameter schema](https://docs.deribit.com/api-reference/market-data/public-get_tradingview_chart_data)
uses `resolution`; its introductory prose calls it `chart_resolution`.
I interpret ticks as bar opens, following the endpoint's TradingView format and
[TradingView's opening-time convention](https://www.tradingview.com/pine-script-docs/concepts/time/).
I haven't verified that interpretation against a live history response yet.
I make each close available one hour after its tick, and only after its local
fetch timestamp. Both times must be at or before the option snapshot.

I require 721 or 2,161 contiguous closes, with the last close at most two hours
old. Short, stale, gapped, revised and invalid histories have separate reasons.
I keep source hashes and every observed revision. A later fetch can't supply a
past forecast. I don't forward-fill candles or overwrite a rejected baseline.
Schema 3 adds append-only history and baseline tables without revising captures.

I fit GARCH to 100 times log returns, then convert variance and residual state
back to decimal units. I require convergence, nu>2 and alpha+beta<1.
I simulate 16,384 paths with seed 271828, using variance-standardised Student-t
innovations and ordinary hourly GARCH state updates. If the last close precedes
the snapshot, I simulate the gap and discard its return contribution. For each
hour I include its innovation times the square root of that hour's overlap with
the snapshot-to-expiry interval. I treat this intra-hour allocation as an
approximation; the variance state only updates at hourly boundaries.
The [arch forecasting documentation](https://arch.readthedocs.io/en/latest/univariate/forecasting.html)
describes the hourly simulation recursion I use.

I freeze 257 empirical log-return quantiles and interpolate CDF values linearly
between them, with density equal to delta probability over delta log return.
I retain the knots, fitted state, versions, seed and path count, so scoring doesn't
refit or resimulate. Repeated knots or nonfinite paths reject the forecast.
I add the knots to CRPS integration nodes and still require refinement convergence.

Student-t log innovations don't have a finite exponential moment. I only score
the explicit bounded surrogate, collapsing its tails onto the same SVI bounds.
Zero log drift doesn't establish a finite raw price mean. I retain tail counts,
one-sided 95% binomial upper bounds for Monte Carlo tail probability, path resolution
1/16,384 and quantile probability spacing 1/256 as separate diagnostics.
The binomial bounds describe simulation error conditional on the fitted model.
They don't cover parameter uncertainty or quantile interpolation error.
Beyond the simulated extrema, the surrogate can assign zero density. I flag these
outcomes and their infinite log losses as simulation-approximation effects.

`predict` freezes each baseline once, including failures, within the original
ten-minute completion deadline. Repeat runs return stored records. `score` uses
the same retained delivery row and verifies both forecast hashes and common bounds.
With a historical `--as-of-ms`, scoring produces a repeatable preview and doesn't
insert score rows. I can inspect retained history and inference with either
interpreter from the README:

```sh
.venv/bin/python -m implied_calibration.cli history --db data/research.sqlite --input data/hourly-01
.venv/bin/python -m implied_calibration.cli history --help
.venv/bin/python -m implied_calibration.cli infer --db data/research.sqlite --output results/inference.json
```

I request live history explicitly with `history --fetch --raw-output`, plus
hour-aligned `--start-ms` and `--end-ms`. I haven't run that capture. I need to
collect it before future option snapshots to qualify the history baselines.

For inference I pair one SVI score and one baseline score per unique expiry,
sort chronologically, and retain missing-pair reasons. Negative CRPS differences
mean lower loss for SVI. I use the Politis-Romano stationary bootstrap with
2,000 resamples, seed 1729, circular continuation and geometric block restarts.
My expected block length is max(14, observed expiries inside the lead window).
I report a two-sided 95% percentile interval and a null-centred bootstrap p-value
with a plus-one correction. The [stationary-bootstrap reference](https://arch.readthedocs.io/en/latest/bootstrap/generated/arch.bootstrap.StationaryBootstrap.html)
gives the geometric block construction.

I cross-check the paired mean with an asymptotic normal Diebold-Mariano statistic
and Bartlett Newey-West variance. HAC lags are max(13, overlap count minus one).
My ESS is the minimum of n, the initial-positive monotone autocorrelation-pair
estimate and n times sample variance divided by the HAC long-run variance.
The HAC cross-check catches delayed dependence that adjacent pairs can miss.
Degenerate variances don't qualify. ESS below 60 prints "insufficient data"
and suppresses intervals, test statistics, p-values and conclusions.

For PIT I transform a separate copy clipped to [1e-6,1-1e-6] through Phi inverse.
I report clipping counts. My Berkowitz-type diagnostic jointly checks E[z]=0
and E[z squared]=1 with a two-moment HAC Wald statistic. I allow serial dependence
under the null and use the smaller ESS of the two moment series. I only test those
marginal moments; this diagnostic can't establish full normality. Its asymptotic
chi-square reference comes from HAC moment covariance, without assuming independent
PITs. I don't use an independent KS or binned chi-square test.

I reserve headline inference for the original 24-hour ATM comparison on USD CRPS.
All other leads, baselines, expiry classes, PIT diagnostics and DM checks stay
descriptive, with p-values suppressed. I retain infinite and undefined log-loss
counts rather than dropping those outcomes from a finite mean.

My offline checks cover all three baseline scores, seeded GARCH simulation,
hourly gaps, future-row invariance, immutable failures, historical previews and
Git witnesses. I check stationary-bootstrap coverage across 60 AR(1) simulations,
DM and vector HAC against hand calculations, delayed-lag ESS, and calibrated
correlated PITs. These are synthetic checks, not evidence of market calibration.
