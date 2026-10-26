# Claims, code and tests

I keep the original historical results separate from the declared exploratory v2 comparisons. The [paper](latex/main.pdf) gives the interpretation; [v2 results](v2-results.md) retain every declared model and inference outcome.

| Claim I can support | Evidence and code | Tests |
| --- | --- | --- |
| Completed labels prevent training on future targets. | `review_models.completed_rows`, `incremental.fit_forecasts`; origin-level training cutoffs in the rebuilt records. | `test_review_models.py`, `test_incremental.py` |
| The corrected strict-window comparison matches 1,208 origins. | `review_report.window_check`; `v2-results.json.window_check`. | `test_review_models.py` |
| Model choice changes point rankings on 1,176 common origins. | `review_models.benchmark_records`, `review_report.benchmark_summary`; all 17 means and paired intervals in `v2-results.json`. | `test_review_models.py`, `test_review_report.py` |
| Log back-transformation uses training-only smearing; QLIKE fits stay positive. | `review_models.log_smearing`, `qlike_fit`; public specification and amendment precede evaluation. | `test_review_models.py` checks leakage, gradients and positivity. |
| Constrained combination weights use completed reconstructed past-only forecast errors. | `review_models.vintage_forecasts`, `bates_granger`; retained-vintage limitation in the paper. | `test_review_models.py` |
| Subsampling fails the declared finite-sample calibration checks. | `inference_reference.calibration_report`; common synthetic draws and Monte Carlo intervals in `v2-results.json`. | `test_inference_reference.py`, `test_review_report.py` |
| The original v1 numerical summary is preserved. | `review_report.validate_v1_rebuild` checks exact structure, counts and hashes, with tiny floating round-off recorded separately, before v2 runs. | `test_review_report.py` rejects material and structural drift; full offline rebuild in CI. |
| Numeric manuscript macros cannot silently drift from either summary. | `paper_numbers.check_numbers`; `scripts/build_paper_numbers.py --check`. | `test_paper_numbers.py` mutates a result and requires failure. |
| Public collection rejects late freezes and invalid restored records. | `public_variance.validate_current_forecast`, `freeze_once`, `retain_server_receipt`; disabled workflow in `.github/workflows/freeze-variance.yml`. | `test_public_variance.py`, `test_variance_forward.py` |
| Adding old SVI snapshots doesn't add prediction-time IV inversions. | `pipeline.predict` and the snapshot-indexed quote query. | `test_operational_fixes.py` |

These checks don't establish a best forecast, valid nominal market-test size, net trading returns, original unrevised input vintages or continuous hosted collection. ETH loading support is tested, but no ETH research result is included.
