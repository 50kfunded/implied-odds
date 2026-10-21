# Research note

I present the current matched HAR experiment in my [LaTeX paper](latex/main.pdf),
[source](latex/main.tex) and [machine-readable results](incremental-results.json). I retain the earlier study
below so its observations, source failure and protocol choices stay visible.

I use 365 completed labels for both HAR regressions and one matched evaluation cohort.
The augmentation's mean QLIKE difference is -0.01424 with interval [-0.03490, +0.00505].
I have no clear incremental result. My [future plan](../protocol/incremental-variance-v1.json)
starts on 21 October 2026. My [inference amendment](../protocol/inference-validation-v1.json)
disables formal claims after the declared simulations exposed poor size and no gate passes
at one year. I keep the fixed terminal date even if the result stays inconclusive.

My [SVI stopping amendment](../protocol/forward-inference-v1.json) keeps interim inference
descriptive, fixes the expiry cohort and pins terminal data availability. I preserve
the original locks. I publish counts, rejection reasons and local hash anchors in
my [scoreboard snapshot](scoreboard.html); these hashes have no independent timestamp.

I run bounded supervised sessions with
`python scripts/collect_variance.py --seconds 600` for the HAR inputs, and the
`collect` command below for SVI. I check `data/variance-forward/status.json` and
`data/live/status.json` for completed cycles and failures. I stop both jobs after
validation. I publish counts with `python scripts/publish_scoreboard.py`.
I don't backfill a missed pre-target forecast after restarting this PC.

I'm studying whether BTC option-implied probabilities line up with realised
settlement frequencies across expiries. Risk-neutral probabilities can differ
from realised frequencies because of risk premia. I won't treat a difference
as evidence of a trading edge.

## Historical variance study

I compared 1,967 daily BTC variance forecasts from 1 May 2021 through 18 September
2026 with their subsequent 30-day outcomes. DVOL had 29.2% lower mean QLIKE than
trailing 30-day realised variance in this historical sample. A correction fitted
only on completed outcomes added almost no average gain. I keep these findings
descriptive: one vendor OHLC check failed, and the fixed non-overlapping sample's
interval includes zero. I don't claim full probability calibration or profit.

| Origin year | n | Mean QLIKE difference, DVOL minus trailing | Descriptive 95% interval |
| --- | ---: | ---: | ---: |
| 2021, from May | 245 | +0.04457 | -0.06661 to +0.14294 |
| 2022 | 365 | -0.06185 | -0.17404 to +0.03208 |
| 2023 | 365 | -0.08108 | -0.17223 to -0.00340 |
| 2024 | 366 | -0.05973 | -0.10594 to -0.01562 |
| 2025 | 365 | -0.02701 | -0.08824 to +0.02987 |
| 2026, through 18 September | 261 | -0.13211 | -0.19544 to -0.06318 |
| Full sample | 1,967 | -0.05463 | -0.09374 to -0.01459 |

I get mean QLIKE 0.13236 for DVOL and 0.18699 for trailing variance. The paired
median difference is +0.00094, and DVOL wins on 968 origins, or 49.2%. Its lower
average loss reflects the severity of errors rather than winning on most days.
DVOL averages 59.52% annualised volatility against 51.97% subsequently realised,
a gap of 7.55 volatility points; it's above realised volatility on 73.4% of origins.
These gaps aren't returns, option mispricing estimates or a causal risk-premium measure.

I score annualised variances with `QLIKE = actual/forecast - log(actual/forecast) - 1`.
At each 00:00 UTC origin, I use the previous completed daily DVOL close divided by
100 and squared. My trailing forecast uses the preceding 720 hourly log returns.
The target uses exactly 720 future returns. Each window needs 721 completed closes;
a candle's hourly tick is its opening time, and its close becomes available one hour later.
I annualise the mean squared hourly return by 8,760, without subtracting a sample mean.
Missing inputs exclude and count the origin; I don't fill them. No origins are missing here.

My [separate study choices](../protocol/historical-variance.json) were committed
before retrieval, in `b46b161` and `6a7c9be`. This is a retrospective analysis plan,
not a historical preregistration or a witness of unrevised market-data vintages.
I retrieved [the full retained inputs](../recordings/historical-variance/manifest.json)
on 19 October 2026 in 76 successful public requests. I retained all 48,626 hourly
price candles and 1,996 daily DVOL candles; there are no conflicting duplicates.
I don't insert these historical observations into the prospective forecast database.

I use 5,000 paired stationary-bootstrap draws with seed 314159 and 60-day expected
blocks, plus a 59-day Newey-West cross-check. Paired-loss ESS is 126.8 against a
minimum of 60. All intervals stay descriptive because the source gate failed.
Annual ESS ranges from 7.0 to 30.1, so I don't make annual significance claims.
The 30/90-day block intervals are respectively [-0.09396, -0.01605] and
[-0.09292, -0.01557]. A fixed non-overlapping sample anchored on 1 May 2021 has
66 outcomes, difference -0.03883 and interval [-0.09374, +0.01071]. Non-overlap
doesn't establish independence. I withhold intervals if an accepted series has
internal calendar gaps; forcing a bootstrap restart there would distort row weights.

My declared timing sensitivities keep the same origins: a one-day older DVOL
close gives difference -0.05126, and moving the entire future window one hour
later gives -0.05460. Neither sensitivity changes the primary result or its gate.

From 1 January 2023, I fit a scale using the latest 365 fully completed origin
outcomes: `scale = mean(realised_variance / dvol_variance)`. I include a training
outcome only when its 30-day target has ended. This scale minimises training QLIKE;
it isn't the ratio of the two means. Future-outcome mutation tests check that it
can't use unfinished targets. On the same 1,357 evaluation origins, mean QLIKE is
0.09582 corrected, 0.09611 raw DVOL and 0.16670 trailing variance. Corrected minus
raw is -0.00028, with descriptive interval [-0.01619, +0.01516]. I don't claim a
meaningful correction gain. Its 365-origin training window can create dependence
beyond the primary 60-day block, so the correction remains a secondary diagnostic.

I checked daily versus hourly OHLC on three dates fixed before retrieval. The
perpetual daily bars actually run 08:00 to 08:00 UTC, and the endpoint returns
both bars intersecting a midnight-to-midnight request. I corrected the reader
before calculating losses and checked all six retained daily price bars against
their actual 24 hourly bars. All six match. DVOL daily bars use 00:00 UTC; two of
three match all OHLC fields. On 5 September 2026, the daily open is 37.33 while
the first hourly open is 37.96. High, low and close match. I kept the failed check
and all original bytes. A [separate three-request refetch](../recordings/historical-alignment-audit/manifest.json)
returns the same discrepancy. I don't relax the all-OHLC gate after seeing losses.
The affected field isn't a forecast input, but the unresolved vendor inconsistency
still prevents my formal superiority claim. The rebuilt report flags it visibly.

I follow the official [DVOL endpoint](https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data),
[price-candle endpoint](https://docs.deribit.com/api-reference/market-data/public-get_tradingview_chart_data)
and [DVOL methodology](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/).
DVOL is a 30-day option-implied volatility index with a 365-day convention.
Its methodology truncates wings, can use marks or trades and smooths the output.
Method changes and later historical revisions can matter. BTC-PERPETUAL returns
are a proxy for spot-index returns, so jumps, funding and basis can affect this study.

## Evidence so far

I captured production option quotes and hourly BTC-PERPETUAL history on
18 October 2026 at 20:42 UTC. My [snapshot manifest](../recordings/live-02/manifest.json)
retains both inputs. I found 928 quotes in 12 expiry slices, with 383 usable OTM
quotes and 2,162 completed hourly closes available before capture. Four CDFs passed;
three slices failed butterfly checks and five failed calendar checks.
My [real smile figure](market-smile.svg) shows 19 October at 08:00 UTC, with 11
strikes, 0.346 vol-point RMSE and 7.01% extrapolated probability mass.
I select the earliest accepted expiry in the latest retained snapshot for the figure,
without choosing on fit error. The rebuilt `snapshot/snapshot.json` lists every slice,
quote rejection reason, fit diagnostic and source commit. These are exploratory
market fits, separate from frozen forecasts and settled calibration outcomes.

I've checked the recording and ingestion pipeline on a local ten-minute capture
from 16 October 2026. These are engineering checks. Repeated quotes aren't
independent outcomes, and this capture doesn't establish calibration.
I include the unchanged raw recording and heartbeat under `recordings/`, with
source hashes in the [manifest](../recordings/manifest.json). I rebuild the database
locally. My offline tests use generated data and hash-checked public inputs.

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

I call the BTC option summary and `btc_usd` index each cycle, plus instruments
on the first cycle and once per UTC hour. I only use the production API.
The index response isn't a settlement price. Each raw JSON-lines record keeps
the parsed body unchanged, endpoint, parameters, HTTP status, local UTC millisecond
request/response/fetch times, latency and attempt outcome. Deribit's microsecond
timings stay inside the body. A network failure has a null body and HTTP status;
invalid JSON also keeps its response text. My schema rules live in
[client.py](../src/implied_calibration/client.py), and my timestamped joins live in
[store.py](../src/implied_calibration/store.py).

My default interval is 300 seconds, with a minimum of 10. Polls start on UTC
epoch-aligned boundaries. I omit `--seconds` for continuous recording; an in-flight
request can finish after a duration expires. Ctrl+C records an interruption.
Recording returns 1 for failed attempts, gaps, clock changes or no cycles.

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
I keep that group's flag and rejection; I haven't relaxed any threshold to clear it.
I can rebuild the full row-level diagnostic from the included capture.

I corrected the forecast gate's scope after review. The earlier implementation
used one historical flag to veto every later group. I now apply the same fixed
checks to each snapshot and expiry, with aggregate medians kept as diagnostics.
`phase4_ready` means at least one group is eligible, not that every group passed.
For a forecast I filter its selected snapshot in SQL before cleaning, fit all
eligible expiries in that snapshot for the same calendar checks, and reuse that
snapshot's fit results within the freeze call. I haven't changed either locked
protocol, any numeric threshold, the calendar grid or the inference settings.

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

The `fit` command cleans captured rows as of the requested cutoff before fitting.
It keeps every expiry group in its output, including rejected or failed fits.
The original historical gate blocked my ten-minute capture before the optimiser.
The corrected per-group gate allows eligible market slices and retains flagged groups.
I haven't changed the locked cleaning thresholds. The synthetic example runs without that capture.

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
returns exit 2 when no CDF passes. My earlier ten-minute smoke capture has no
research forecasts; the later 18 October snapshot supplies the four exploratory
CDFs reported above. Finite grids can miss violations,
and PAVA doesn't preserve option prices or the martingale mean. My scoring convention
for accepted analytic CDFs is below.

I run the synthetic smile example with
`.venv/bin/python scripts/synthetic_svi.py --output results/synthetic-svi.json --plot results/synthetic-svi.png`
on macOS or Linux, or replace the interpreter with `.\.venv\Scripts\python.exe`
in PowerShell. I keep the equivalent CDF example commands in the pipeline section below.

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
atom masses, omitted raw probability and tail-mean diagnostics. The
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

## Research still to do

I've completed the implementation and offline reporting. I still need settled SVI forecast outcomes.

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
My live capture has hour-aligned ticks and completed prices, but those fields alone
don't independently prove the bar anchoring.
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

I can request live history with `history --fetch --raw-output`, plus hour-aligned
`--start-ms` and `--end-ms`. I've now captured it before a real option snapshot.
The collector automates this ordering for future forecasts.

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

## Reproducing the reports

`scripts/reproduce.py` verifies every included sample hash and rejects unlisted
files. All included recordings total 1,837,121 bytes, below 5 MB. I ingest them
into a temporary SQLite database, rebuild the report, then remove that temporary
database. I don't create research forecasts during reproduction or change the
working research database. I keep generated files under ignored `results/`.

I write `results/reproduction/market/report.html` and `report.json`, plus six PNG
panels: smile against bid/ask, CDF against baselines, PIT, tail reliability,
cumulative paired CRPS differences and coverage. The included sample retains
its quote-level flags and zero research scores, so unavailable panels show
"insufficient data". Reporting never refits or replaces a frozen forecast.

The separate `historical/` study rebuilds every historical variance outcome and its
refetch audit, with CSV, JSON, an embedded SVG and a self-contained HTML report.
I retain the source failure and suppress the formal claim on every rebuild.

The separate `snapshot/` diagnostic refits my retained market snapshot and
writes a real smile SVG plus all slice results. It doesn't create predictions,
baselines or scores. The synthetic report stays separate from both market outputs.

I write a separate `synthetic/` report with 240 generated independent lognormal
outcomes, fixed seed 314159 and generated hourly GARCH history. It uses one fixed
fitted distribution and baseline set to exercise the figures. It isn't a rolling
market study, and its generated dates and scores aren't registered as research
records. Every synthetic panel and report carries that label.

The PIT histogram uses raw PITs with ten equal bins. Its 95% binomial iid reference
range is `binom.ppf([.025,.975], n, .1)`, using the
[SciPy binomial distribution](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binom.html).
That range is pointwise under an independent uniform null. I don't treat it as
a confidence band or calibration test for overlapping research outcomes.

For reliability I use fixed 1%, 5%, 10% and 25% lower and upper quantiles from
frozen SVI forecasts at the primary lead. I count delivery <= lower quantile and
delivery >= upper quantile. Inclusive boundary atoms can exceed the nominal
probability. Each event series needs ESS >= 60 before I show a stationary-bootstrap
interval. Raw frequencies remain descriptive, with "insufficient data" visible.

For CRPS I plot cumulative SVI-minus-ATM paired means at up to 32 display prefixes.
Every prefix gets its own overlap-aware bootstrap and ESS gate. I label the
intervals pointwise; they aren't simultaneous bands or extra headline tests.
Coverage stays split by model, lead and expiry class, and I retain infinite or
undefined log-loss counts. I only show a smile and CDF from one hash-linked frozen
forecast, its exact quote snapshot and its own baseline records.

Reports record the Git commit, working-tree state, dependency versions and figure
hashes. I use fixed seeds, Matplotlib defaults and fixed PNG metadata. Repeated
builds in the same checkout and software environment should reproduce the same
report and figure bytes. Fonts, platform and dependency versions can change
rendering across environments. HTML reports embed their PNGs and need no network.

My final checklist lives in each report JSON. I check both committed locks before
their first eligible decisions, verify retained sample hashes during reproduction,
and count reasons for rejected quotes. A standalone SQLite report can't prove
raw retention, so it says that explicitly. Undated ingestion issues only enter an
as-of report when their retained source has a bounded fetch timestamp.

## Running the pipeline

I use the environment setup in the README. I start one local collection job with:

```powershell
.\.venv\Scripts\python.exe -m implied_calibration.cli collect --output data/live --db data/live.sqlite --report-output results/live
```

```sh
.venv/bin/python -m implied_calibration.cli collect --output data/live --db data/live.sqlite --report-output results/live
```

I add `--seconds 600` for a bounded run. The default keeps collecting while the
process and PC stay awake. Before option capture I fetch 2,162 completed hourly
closes, then refresh hourly with enough overlap to fill any sleep or outage gap.
A short or partial bootstrap stays in recovery mode. I request metadata and index
before the book, ingest that immutable cycle, freeze due targets, fetch official
deliveries hourly and score any settled forecasts. A new score rebuilds the report.
At 08:00, the newly completed book response is after the target, so the previous
eligible snapshot is normally selected. Newly fetched history can't qualify it.

`data/live/status.json` records the PID, commit, last cycle and failure counters.
`data/live/heartbeat/` records sessions, skipped slots and clock changes. Each cycle
has a fresh immutable segment. OS leases prevent two jobs sharing a capture folder
or database and release after a crash; I don't delete a live process's lock file.
After a crash I rerun the same command. A new session preserves the old segments
and starts at the next unused slot. I restart after changing source or commits;
the collector records `research_checkout_changed` and skips freezing if they change.

I stop after the current cycle with:

```powershell
.\.venv\Scripts\python.exe -m implied_calibration.cli collect-stop --output data/live
```

```sh
.venv/bin/python -m implied_calibration.cli collect-stop --output data/live
```

My first eligible expiry is still 26 October. The 18 October metadata doesn't list
it yet. The earliest listed eligible seven-day target is 23 October at 08:00 UTC,
for 30 October delivery, subject to the same acceptance checks. I don't invent
missing historical forecasts or move the locked date to create quicker results.
The original 14-outcome bootstrap block and ESS minimum also stay fixed. A different
noise-aware gate, block length or tail interval would be separate sensitivity work.

These lower-level commands remain available. The offline reproducer doesn't call an API.

```powershell
.\.venv\Scripts\python.exe -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.\.venv\Scripts\python.exe -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.\.venv\Scripts\python.exe scripts/estimate_disk.py data/smoke-01
.\.venv\Scripts\python.exe -m implied_calibration.cli coverage --db data/research.sqlite
.\.venv\Scripts\python.exe -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.\.venv\Scripts\python.exe -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.\.venv\Scripts\python.exe -m implied_calibration.cli cdf --db data/research.sqlite --output results/cdf.json
.\.venv\Scripts\python.exe -m implied_calibration.cli history --db data/research.sqlite --input data/hourly-01
.\.venv\Scripts\python.exe -m implied_calibration.cli predict --db data/research.sqlite --output results/predictions.json
.\.venv\Scripts\python.exe -m implied_calibration.cli settle --db data/research.sqlite --fetch --raw-output data/delivery-01
.\.venv\Scripts\python.exe -m implied_calibration.cli settle --db data/research.sqlite --input data/delivery-01
.\.venv\Scripts\python.exe -m implied_calibration.cli score --db data/research.sqlite --output results/scores.json
.\.venv\Scripts\python.exe -m implied_calibration.cli infer --db data/research.sqlite --output results/inference.json
.\.venv\Scripts\python.exe -m implied_calibration.cli report --db data/research.sqlite --output results/report
.\.venv\Scripts\python.exe scripts/synthetic_cdf.py --output results/synthetic-cdf.json --plot results/synthetic-cdf.png
```

```sh
.venv/bin/python -m implied_calibration.cli record --output data/smoke-01 --interval 60 --seconds 600
.venv/bin/python -m implied_calibration.cli ingest --input data/smoke-01 --db data/research.sqlite
.venv/bin/python scripts/estimate_disk.py data/smoke-01
.venv/bin/python -m implied_calibration.cli coverage --db data/research.sqlite
.venv/bin/python -m implied_calibration.cli clean --db data/research.sqlite --output results/clean.json
.venv/bin/python -m implied_calibration.cli fit --db data/research.sqlite --output results/fit.json
.venv/bin/python -m implied_calibration.cli cdf --db data/research.sqlite --output results/cdf.json
.venv/bin/python -m implied_calibration.cli history --db data/research.sqlite --input data/hourly-01
.venv/bin/python -m implied_calibration.cli predict --db data/research.sqlite --output results/predictions.json
.venv/bin/python -m implied_calibration.cli settle --db data/research.sqlite --fetch --raw-output data/delivery-01
.venv/bin/python -m implied_calibration.cli settle --db data/research.sqlite --input data/delivery-01
.venv/bin/python -m implied_calibration.cli score --db data/research.sqlite --output results/scores.json
.venv/bin/python -m implied_calibration.cli infer --db data/research.sqlite --output results/inference.json
.venv/bin/python -m implied_calibration.cli report --db data/research.sqlite --output results/report
.venv/bin/python scripts/synthetic_cdf.py --output results/synthetic-cdf.json --plot results/synthetic-cdf.png
```

`clean`, `fit`, `cdf`, `predict`, `score`, `infer` and `report` return 2 when their
gate is closed or nothing qualifies. I run `predict` within ten minutes of a due
lead, after retaining the option snapshot and any required hourly history.
