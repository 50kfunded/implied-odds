# The apparent frequency reversal does not survive refitting

I rebuild 1,184 matched origins using nested 5-, 15- and 60-minute closes from the same Deribit perpetual histories. Each frequency gets its own past variance features, 30-day targets and 730 completed training labels. I also score unchanged hourly forecasts against each target.

| Return grid, minutes | Refitted ratio HAR | Refitted ratio HAR + DVOL | Added DVOL difference | Unchanged hourly difference |
| --- | ---: | ---: | ---: | ---: |
| 5 | 0.06743231 | 0.06574703 | -0.00168528 | +0.00061854 |
| 15 | 0.07699143 | 0.07053392 | -0.00645750 | -0.00054990 |
| 60 | 0.06500295 | 0.06374317 | -0.00125978 | -0.00125978 |

Changing the target while holding hourly forecasts fixed changes the relative-risk ranking. After refitting the entire pipeline, adding DVOL lowers point error at all three frequencies. The tempting frequency-induced sign reversal therefore fails this check. These are inspected historical outcomes, and neither sign supplies a calibrated significance claim.

Hourly nesting matches every retained hourly close exactly. The source retains trade candles, rather than a certified efficient-price series; five-minute variance still misses the intrabar path. No returns are clipped or replaced. The archived research also examined hourly grid phases and selected event days; those inspected diagnostics do not establish a new forecast effect.

[Computed results and source counts](frequency-results.json) · [Retained response hashes](../recordings/frequency-2026-10-28/manifest.json) · [Main method](progressive-evidence.md)
