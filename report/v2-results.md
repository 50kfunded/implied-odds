# Declared exploratory v2 comparisons

I added these comparisons after inspecting v1 and the review. The public declaration and amendment precede this evaluation. I preserve the v1 results and prospective locks.

## Matched window check

All rows use the same 1,208 origins. Raw DVOL has no estimation window.

| Window | HAR mean QLIKE | HAR + DVOL mean QLIKE | Raw DVOL mean QLIKE |
| --- | ---: | ---: | ---: |
| 365 | 0.12544 | 0.10987 | 0.09468 |
| 730 | 0.11741 | 0.10248 | 0.09468 |
| expanding | 0.14069 | 0.09763 | 0.09468 |

## Declared benchmark universe

I scheduled 1,176 origins; 1,176 have all declared forecasts. Intervals are descriptive and are withheld across calendar gaps. The table uses the balanced cohort; coverage counts also retain failed fits.

| Model | Coverage | Mean QLIKE | Difference from HAR + DVOL 365 | 95% interval | ESS |
| --- | ---: | ---: | ---: | --- | ---: |
| dvol | 1176 | 0.09425 | -0.01616 | [-0.04389, +0.01034] | 45.01 |
| har_365 | 1176 | 0.12562 | +0.01521 | [-0.00734, +0.03929] | 45.30 |
| har_dvol_365 | 1176 | 0.11041 | reference | — | — |
| har_730 | 1176 | 0.11365 | +0.00324 | [-0.03132, +0.03438] | 53.41 |
| har_dvol_730 | 1176 | 0.10025 | -0.01016 | [-0.03471, +0.01088] | 53.23 |
| har_expanding | 1176 | 0.13670 | +0.02630 | [-0.01845, +0.07057] | 45.78 |
| har_dvol_expanding | 1176 | 0.09524 | -0.01517 | [-0.04025, +0.00708] | 51.93 |
| log_har_smear | 1176 | 0.10888 | -0.00152 | [-0.03011, +0.02495] | 58.67 |
| log_har_dvol_smear | 1176 | 0.09278 | -0.01763 | [-0.03587, -0.00120] | 47.98 |
| qlike_har | 1176 | 0.11046 | +0.00005 | [-0.02967, +0.02739] | 57.13 |
| qlike_har_dvol | 1176 | 0.09485 | -0.01556 | [-0.03402, +0.00072] | 47.43 |
| ridge_qlike_har_dvol | 1176 | 0.09478 | -0.01563 | [-0.03448, +0.00072] | 47.99 |
| ewma_094 | 1176 | 0.20788 | +0.09747 | [+0.04983, +0.14254] | 92.43 |
| garch_11 | 1176 | 0.14341 | +0.03300 | [-0.02151, +0.09647] | 22.05 |
| egarch_111 | 1176 | 0.14901 | +0.03860 | [-0.01929, +0.10391] | 28.07 |
| bates_granger | 1176 | 0.09719 | -0.01321 | [-0.03559, +0.00667] | 50.09 |
| mz_dvol | 1176 | 0.09726 | -0.01315 | [-0.03890, +0.01051] | 48.54 |

The focal QLIKE HAR + DVOL minus QLIKE HAR comparison has mean -0.01561, interval [-0.03517, +0.00359] and ESS 56.85.

I retain 0 failed origin/model fits in the JSON. A failure is not silently replaced by another forecast.

## Inference references on the original cohort

| Contrast | Mean | DM-HLN p, lag 29 | DM-HLN p, lag 59 | Subsampling interval |
| --- | ---: | ---: | ---: | --- |
| dvol_minus_har | -0.02937 | 0.0189 | 0.0412 | [-0.04591, +0.00530] |
| har_dvol_minus_har | -0.01424 | 0.1214 | 0.1876 | [-0.03324, +0.01005] |
| dvol_minus_har_dvol | -0.01513 | 0.2267 | 0.2621 | [-0.03431, +0.01156] |
| qlike_har_dvol_minus_qlike_har on the separate v2 cohort | -0.01561 | 0.1002 | 0.1220 | [-0.03368, -0.00158] |

I report these p-values as references. HLN, subsampling and MCS do not remove the inspected-sample, dependence or generated-forecast limitations.

The Bates-Granger row uses reconstructed past-only HAR forecasts from the retained data vintage. These are not original unrevised historical vintages.

## Synthetic calibration

| n | Process | Stationary percentile | DM-HLN 29 | DM-HLN 59 | Subsampling |
| ---: | --- | ---: | ---: | ---: | ---: |
| 365 | overlap_30 | 12.5% | 9.0% | 7.5% | 16.5% |
| 365 | overlap_30_plus_slow_365 | 51.5% | 46.5% | 45.0% | 55.5% |
| 365 | overlap_30_plus_clustered_tails | 17.5% | 11.0% | 10.5% | 20.0% |
| 1357 | overlap_30 | 11.0% | 12.0% | 10.0% | 15.5% |
| 1357 | overlap_30_plus_slow_365 | 45.0% | 52.0% | 48.0% | 45.0% |
| 1357 | overlap_30_plus_clustered_tails | 12.0% | 12.5% | 9.0% | 13.0% |

These are null rejection rates at nominal 5% in the declared synthetic processes. Monte Carlo intervals remain in the JSON. They do not estimate the false-positive rate in the market sample.

## Conditional power planning

| Expected block days | n | Assumed mean | DM-HLN rejection | Subsampling rejection |
| ---: | ---: | ---: | ---: | ---: |
| 30 | 365 | +0.00000 | 10.6% | 16.1% |
| 30 | 365 | -0.01424 | 18.4% | 25.0% |
| 30 | 730 | +0.00000 | 9.2% | 12.5% |
| 30 | 730 | -0.01424 | 26.2% | 29.2% |
| 30 | 1825 | +0.00000 | 6.2% | 7.9% |
| 30 | 1825 | -0.01424 | 45.8% | 44.6% |
| 30 | 2920 | +0.00000 | 8.2% | 10.8% |
| 30 | 2920 | -0.01424 | 65.3% | 61.1% |
| 60 | 365 | +0.00000 | 14.1% | 18.2% |
| 60 | 365 | -0.01424 | 21.5% | 26.6% |
| 60 | 730 | +0.00000 | 10.7% | 14.0% |
| 60 | 730 | -0.01424 | 21.6% | 23.6% |
| 60 | 1825 | +0.00000 | 8.4% | 12.6% |
| 60 | 1825 | -0.01424 | 39.0% | 39.2% |
| 60 | 2920 | +0.00000 | 7.9% | 12.3% |
| 60 | 2920 | -0.01424 | 58.0% | 54.1% |
| 180 | 365 | +0.00000 | 13.1% | 16.3% |
| 180 | 365 | -0.01424 | 21.2% | 25.7% |
| 180 | 730 | +0.00000 | 8.9% | 12.0% |
| 180 | 730 | -0.01424 | 16.9% | 17.2% |
| 180 | 1825 | +0.00000 | 3.3% | 7.8% |
| 180 | 1825 | -0.01424 | 34.3% | 37.5% |
| 180 | 2920 | +0.00000 | 3.4% | 6.7% |
| 180 | 2920 | -0.01424 | 53.8% | 57.8% |
| 365 | 365 | +0.00000 | 13.7% | 16.4% |
| 365 | 365 | -0.01424 | 21.9% | 26.2% |
| 365 | 730 | +0.00000 | 6.7% | 8.5% |
| 365 | 730 | -0.01424 | 15.2% | 14.5% |
| 365 | 1825 | +0.00000 | 1.4% | 3.5% |
| 365 | 1825 | -0.01424 | 31.3% | 35.3% |
| 365 | 2920 | +0.00000 | 1.5% | 4.3% |
| 365 | 2920 | -0.01424 | 51.0% | 63.6% |

The inspected effect and dependence are uncertain. Neither this resampling nor the normal approximation predicts the years needed in a future market.

## Dependent phase display

| Contrast | Negative phases out of 30 | Minimum mean | Median mean | Maximum mean |
| --- | ---: | ---: | ---: | ---: |
| dvol_minus_har | 30 | -0.04973 | -0.03190 | -0.00668 |
| har_dvol_minus_har | 29 | -0.03066 | -0.01612 | +0.00435 |
| dvol_minus_har_dvol | 29 | -0.02611 | -0.01566 | +0.00221 |

All phases reuse the same history. Favourable counts are a dependent stability display; no binomial test or independence claim applies.

## Model Confidence Set

```json
{
  "status": "exploratory",
  "settings": {
    "size": 0.1,
    "method": "R",
    "bootstrap": "stationary",
    "block_size": 60,
    "reps": 5000,
    "seed": 515151,
    "scope": "I report the surviving set as a diagnostic conditional on this inspected candidate set and dependence assumptions, not as proof of a best model."
  },
  "n": 1176,
  "included": [
    "dvol",
    "har_dvol_365",
    "har_dvol_730",
    "har_dvol_expanding",
    "log_har_dvol_smear",
    "qlike_har_dvol",
    "ridge_qlike_har_dvol",
    "garch_11",
    "egarch_111",
    "bates_granger",
    "mz_dvol"
  ],
  "excluded": [
    "har_365",
    "har_730",
    "har_expanding",
    "log_har_smear",
    "qlike_har",
    "ewma_094"
  ],
  "pvalues": {
    "ewma_094": 0.0,
    "qlike_har": 0.0054,
    "log_har_smear": 0.0074,
    "har_expanding": 0.035,
    "har_730": 0.0724,
    "har_365": 0.0856,
    "egarch_111": 0.1796,
    "garch_11": 0.236,
    "har_dvol_365": 0.3678,
    "qlike_har_dvol": 0.6856,
    "ridge_qlike_har_dvol": 0.8036,
    "har_dvol_730": 0.8042,
    "mz_dvol": 0.9756,
    "bates_granger": 0.9756,
    "har_dvol_expanding": 0.9756,
    "dvol": 0.9756,
    "log_har_dvol_smear": 1.0
  },
  "scope": "I report the surviving set as a diagnostic conditional on this inspected candidate set and dependence assumptions, not as proof of a best model."
}
```

I report this set conditional on the inspected candidate universe and bootstrap assumptions. Membership does not establish a best model or a trading result.
