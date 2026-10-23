# DVOL methods and collection

The [paper](latex/main.pdf) is the main account. The numerical rebuild uses retained hourly BTC-PERPETUAL closes and daily DVOL closes, with a direct 30-day realised-variance target. The [earlier reference comparison](historical-variance-note.md) keeps its original results; the separate [SVI study](../studies/svi/README.md) has its own documentation.

At each historical midnight origin, HAR uses daily, weekly and monthly realised-variance features and the latest 365 labels whose complete targets have ended. HAR + DVOL adds the previous completed daily DVOL close divided by 100 and squared. Both level regressions include an intercept and use training-only feature scaling. Rescaled DVOL uses the mean realised/raw-DVOL variance ratio on those exact same labels; the fixed blend averages HAR variance and raw DVOL variance equally. The [extension specification](../protocol/exploratory-dvol-v1.json) was saved before these added comparisons were run and after inspection of the original ranking. The original prospective protocol is unchanged.

`python scripts/build_research.py` writes the full `report.json`, compact `summary.json`, original matched `forecasts.csv`, separate `extensions-forecasts.csv`, and figure assets. Both full and compact JSON retain the specification hash, annual means, signed episode contributions, block sensitivities and in-sample scalar checks. Full JSON also keeps both forecast-record sets. The original summary fields and matched forecasts are unchanged by the additions.

Each row of the new table is compared with HAR + DVOL on the same 1,357 origins. Paired stationary-bootstrap intervals use 5,000 draws, seed 271828, 60-day expected blocks and 59-lag Newey–West cross-checks; block sensitivities use 30, 90, 180 and 365 days. Effective sample size is the minimum of the count, a positive monotone autocorrelation-pair estimate and the HAC variance-ratio estimate. The fixed threshold of 60 isn't a validated significance cutoff. All historical comparisons remain exploratory.

## Collection and provenance

Forecasts retain immutable raw capture segments with code and input hashes. Recoverable delivery-pagination failures retry in fresh segments; conflicting immutable prices stop collection. Cleaning is cached by input, protocol, code and runtime fingerprints, and reports render outside the forecast loop. The [variance scoreboard](variance-scoreboard.json) records counts and rejected origins.

Local hash chains have no independent public timestamp. A fresh heartbeat records activity but doesn't establish that collection is healthy. Short supervised recordings demonstrate bounded validation; continuous collection needs an awake, online host. Missed forecast origins stay missing after a restart.

For a bounded DVOL input recording, I run:

```sh
python scripts/collect_variance.py --seconds 600
python scripts/publish_scoreboard.py
```

The collector stops after the declared duration. Local status is in `data/variance-forward/status.json`. The [prospective plan](../protocol/incremental-variance-v1.json) and [inference amendment](../protocol/inference-validation-v1.json) retain their original cohort, stopping rule and source checks. The terminal analysis is fixed even if the result is inconclusive.

The original vendor all-OHLC check fails on an unused DVOL open, while all three audited closes match. The sample does not establish original unrevised data vintages, statistical superiority, net trading returns or a causal variance premium. The paper keeps the detailed data, scope and inference limits in one place.
