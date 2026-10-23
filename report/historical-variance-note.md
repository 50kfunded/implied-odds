# Earlier DVOL reference comparison

I retain this earlier DVOL-versus-trailing comparison separately from the matched HAR study in the [current paper](latex/main.pdf). Its estimates and source limits are unchanged.

## Historical variance study

The earlier comparison used 1,967 daily BTC variance forecasts from 1 May 2021 through 18 September
2026 with their subsequent 30-day outcomes. DVOL had 29.2% lower mean QLIKE than
trailing 30-day realised variance in this historical sample. A correction fitted
only on completed outcomes added almost no average gain. The source and scope limits below apply to these results.

| Origin year | n | Mean QLIKE difference, DVOL minus trailing | Descriptive 95% interval |
| --- | ---: | ---: | ---: |
| 2021, from May | 245 | +0.04457 | -0.06661 to +0.14294 |
| 2022 | 365 | -0.06185 | -0.17404 to +0.03208 |
| 2023 | 365 | -0.08108 | -0.17223 to -0.00340 |
| 2024 | 366 | -0.05973 | -0.10594 to -0.01562 |
| 2025 | 365 | -0.02701 | -0.08824 to +0.02987 |
| 2026, through 18 September | 261 | -0.13211 | -0.19544 to -0.06318 |
| Full sample | 1,967 | -0.05463 | -0.09374 to -0.01459 |

Mean QLIKE is 0.13236 for DVOL and 0.18699 for trailing variance. The paired
median difference is +0.00094, and DVOL wins on 968 origins, or 49.2%. Its lower
average loss reflects the severity of errors rather than winning on most days.
DVOL averages 59.52% annualised volatility against 51.97% subsequently realised,
a gap of 7.55 volatility points; it's above realised volatility on 73.4% of origins.

Annualised variances are scored with `QLIKE = actual/forecast - log(actual/forecast) - 1`.
At each 00:00 UTC origin, the DVOL forecast is the previous completed daily close
divided by 100 and squared. The trailing forecast uses the preceding 720 hourly log returns.
The target uses exactly 720 future returns. Each window needs 721 completed closes;
a candle's hourly tick is its opening time, and its close becomes available one hour later.
The mean squared hourly return is annualised by 8,760, without subtracting a sample mean.
Missing inputs exclude and count the origin rather than being filled. No origins are missing here.

The [separate study choices](../protocol/historical-variance.json) were committed
before retrieval, in `b46b161` and `6a7c9be`. This is a retrospective analysis plan,
not a historical preregistration or a witness of unrevised market-data vintages.
Retrieval of [the full retained inputs](../recordings/historical-variance/manifest.json) took place
on 19 October 2026 in 76 successful public requests. All 48,626 hourly price candles
and 1,996 daily DVOL candles are retained, with no conflicting duplicates. These
historical observations stay outside the prospective forecast database.

Uncertainty uses 5,000 paired stationary-bootstrap draws with seed 314159 and 60-day expected
blocks, plus a 59-day Newey-West cross-check. Its heteroskedasticity and autocorrelation
consistent, or HAC, variance allows serially dependent loss differences. Paired-loss ESS is 126.8 against a
minimum of 60. ESS means effective sample size: a dependence-adjusted diagnostic,
not an observed count of independent episodes. Annual ESS ranges from 7.0 to 30.1,
below that threshold.
The 30/90-day block intervals are respectively [-0.09396, -0.01605] and
[-0.09292, -0.01557]. A fixed non-overlapping sample anchored on 1 May 2021 has
66 outcomes, difference -0.03883 and interval [-0.09374, +0.01071]. Non-overlap
doesn't establish independence. Intervals are withheld when an accepted series has
internal calendar gaps; forcing a bootstrap restart there would distort row weights.

The declared timing sensitivities keep the same origins: a one-day older DVOL
close gives difference -0.05126, and moving the entire future window one hour
later gives -0.05460. Neither sensitivity changes the primary result or its gate.

From 1 January 2023, a scale is fitted using the latest 365 fully completed origin
outcomes: `scale = mean(realised_variance / dvol_variance)`. Training includes an
outcome only when its 30-day target has ended. This scale minimises training QLIKE;
it isn't the ratio of the two means. Future-outcome mutation tests check that it
can't use unfinished targets. On the same 1,357 evaluation origins, mean QLIKE is
0.09582 corrected, 0.09611 raw DVOL and 0.16670 trailing variance. Corrected minus
raw is -0.00028, with descriptive interval [-0.01619, +0.01516], leaving the
correction gain uncertain. Its 365-origin training window can create dependence
beyond the primary 60-day block, so the correction remains a secondary diagnostic.

Daily versus hourly OHLC checks use three dates fixed before retrieval. The
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

The source conventions follow the official [DVOL endpoint](https://docs.deribit.com/api-reference/market-data/public-get_volatility_index_data),
[price-candle endpoint](https://docs.deribit.com/api-reference/market-data/public-get_tradingview_chart_data)
and [DVOL methodology](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/).
DVOL uses a 365-day convention.
Its methodology truncates wings, can use marks or trades and smooths the output.
Method changes and later historical revisions can matter. BTC-PERPETUAL returns
are a proxy for spot-index returns, so jumps, funding and basis can affect this study.

