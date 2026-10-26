# DVOL methods and collection

The [paper](latex/main.pdf) is the main account. The numerical rebuild uses retained hourly BTC-PERPETUAL closes and daily DVOL closes, with a direct 30-day realised-variance target. The [earlier reference comparison](historical-variance-note.md) keeps its original results; the separate [SVI study](../studies/svi/README.md) has its own documentation.

At each historical midnight origin, HAR uses daily, weekly and monthly realised-variance features and the latest 365 labels whose complete targets have ended. HAR + DVOL adds the previous completed daily DVOL close divided by 100 and squared. Both level regressions include an intercept and use training-only feature scaling. Rescaled DVOL uses the mean realised/raw-DVOL variance ratio on those exact same labels; the fixed blend averages HAR variance and raw DVOL variance equally. The [extension specification](../protocol/exploratory-dvol-v1.json) was saved before these added comparisons were run and after inspection of the original ranking. The original prospective protocol is unchanged.

`python scripts/build_research.py` writes the full `report.json`, compact `summary.json`, original matched `forecasts.csv`, separate `extensions-forecasts.csv`, and figure assets. Both full and compact JSON retain the specification hash, annual means, signed episode contributions, block sensitivities and in-sample scalar checks. Full JSON also keeps both forecast-record sets. The original summary fields and matched forecasts are unchanged by the additions.

Each row of the new table is compared with HAR + DVOL on the same 1,357 origins. Paired stationary-bootstrap intervals use 5,000 draws, seed 271828, 60-day expected blocks and 59-lag Neweyâ€“West cross-checks; block sensitivities use 30, 90, 180 and 365 days. Effective sample size is the minimum of the count, a positive monotone autocorrelation-pair estimate and the HAC variance-ratio estimate. The fixed threshold of 60 isn't a validated significance cutoff. All historical comparisons remain exploratory.

## Collection and provenance

Forecasts retain immutable raw capture segments with code and input hashes. Recoverable delivery-pagination failures retry in fresh segments; conflicting immutable prices stop collection. Cleaning is cached by input, protocol, code and runtime fingerprints, and reports render outside the forecast loop. The [variance scoreboard](variance-scoreboard.json) records counts and rejected origins.

Local hash chains have no independent public timestamp. The disabled [public evidence workflow](../.github/workflows/freeze-variance.yml) adds a GitHub artifact digest and server metadata witness before the deadline, then publishes immutable records on a separate `forecast-evidence` branch. This is an implemented design awaiting a hosted-runner probe and activation. A fresh heartbeat records activity; it doesn't establish that forecasts were accepted. Missed forecast origins stay missing after a restart.

For a bounded DVOL input recording, I run:

```sh
python scripts/collect_variance.py --seconds 600
python scripts/publish_scoreboard.py
```

The collector stops after the declared duration. Local status is in `data/variance-forward/status.json`. The [prospective plan](../protocol/incremental-variance-v1.json) and [inference amendment](../protocol/inference-validation-v1.json) retain their original cohort, stopping rule and source checks. The terminal analysis is fixed even if the result is inconclusive.

The original vendor all-OHLC check fails on an unused DVOL open, while all three audited closes match. The sample does not establish original unrevised data vintages, statistical superiority, net trading returns or a causal variance premium. The paper keeps the detailed data, scope and inference limits in one place.

## Declared v2 comparisons

The [v2 declaration](../protocol/exploratory-review-v2.json) and [focal-contrast amendment](../protocol/exploratory-review-v2a.json) were public before evaluation. Both follow inspection of v1. `python scripts/build_review.py` checks the original summary, then writes all declared model forecasts, failures, matched intervals, MCS, inference calibration and conditional power results under `results/review-v2/`. The [complete table](v2-results.md) and [JSON](v2-results.json) retain the outcomes, including the failed subsampling calibration. Runtime is kept separately from the deterministic numerical summary.

The v1 guard requires identical structure, counts, strings and hashes. Floating results allow relative tolerance `1e-12` and absolute tolerance `1e-14` for platform round-off; every accepted difference is recorded in `v1_rebuild_validation`. A larger numerical change fails the rebuild. My retained Windows rebuild is exactly equal. Published values and the original v1 JSON remain unchanged.

Log-HAR uses training-only residual smearing. Positive log-link QLIKE fits target the scoring objective directly; ridge penalises slopes only. Batesâ€“Granger-style weights use completed reconstructed past-only HAR forecast errors from the retained vintage. This doesn't recover original unrevised vintages. EGARCH scores the first seeded simulation batch and retains a second independent batch as a Monte Carlo diagnostic. Every model is scored on the same balanced cohort; missing fits are retained in coverage counts rather than silently substituted.

## Public collection design

The workflow runs only after `VARIANCE_FORWARD_ENABLED` is set to `true`. I first dispatch `preflight`, which makes three paced public API requests and retains its result. A local mocked test cannot establish that Deribit accepts a GitHub runner's network address.

Freeze attempts are scheduled at 00:13, 00:23 and 00:33 UTC, inside the existing 00:10â€“00:50 window. Three bounded retries allow transient recovery, while late starts fail without backfill. The narrow artifact contains the current forecast, its durability receipt and source hashes. Canonical raw inputs and receipts persist on the evidence branch. A separate daily 12:17 refresh scores matured targets independently of whether that day's freeze succeeded, including after the last forecast origin and before the analysis cutoff. Failed attempts retain separate records and diagnostics; corrupt unreferenced segments are quarantined, while damage to accepted source evidence stops collection.

GitHub warns that [scheduled runs can be delayed or dropped and public schedules can be disabled after 60 days of inactivity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Off-peak minutes and retries reduce exposure; they don't guarantee delivery. I would check Actions status monthly, watch failures and missing accepted origins, and use an external host monitor before calling this continuous operation. A watchdog on the same scheduler cannot independently detect a scheduler outage.

The public-state copier rejects replacements, symlinks and non-whitelisted paths, with a 500 MB input budget. Immutable records grow with time, so the JSON counts and branch size need review before that budget is reached. [GitHub artifacts](https://docs.github.com/en/rest/actions/artifacts) are temporary witnesses, not the canonical archive. A verifier should query the artifact ID and run URL, match the server digest to the downloaded bundle, and check the forecast hash. A copied local receipt alone is not independent evidence. A commit's editable author date alone is also insufficient.

OpenTimestamps is an optional extra proof, not implemented anchoring. The free [client](https://github.com/opentimestamps/opentimestamps-client) can stamp a frozen JSON file without waiting; later upgrades may complete a Bitcoin proof. Pending proofs are not confirmed anchors, and confirmation after the origin doesn't by itself establish timely forecast publication. The original protocol and locks remain unchanged.

For a read-only audit, both PowerShell and bash accept:

```sh
python scripts/audit_forward.py --as-of 2026-10-25T14:15:00Z
```

The audit validates hashes and receipts, counts accepted forecasts and settled scores, lists permanently missing origins in UTC and reads local status separately. A stale `status.json` or previously published scoreboard is not a current count. The inactive [prospective v2 draft](../protocol/prospective-v2-draft.json) proposes operational missing-day tolerance; it doesn't relax v1's terminal requirements or enable formal inference.
