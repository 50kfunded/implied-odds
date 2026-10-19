"""I test clock alignment, offline report bytes and failed-alignment claims."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from implied_calibration.historical_data import HistoricalInputs
from implied_calibration.historical_report import alignment_checks, write_historical_report
from implied_calibration.variance_study import DAY_MS, HOUR_MS
from test_phase1 import OfflineTest
from test_variance_study import BASE, generated


def aligned_inputs(days=3):
    prices, dvol, config = generated(days)
    config['alignment_check_days_ms'] = [BASE, BASE + DAY_MS, BASE + 2 * DAY_MS]
    hourly_dvol, daily_prices = {}, {}
    for day in config['alignment_check_days_ms']:
        hourly_dvol.update({day + hour * HOUR_MS: (20., 20., 20., 20.) for hour in range(24)})
        bars = [prices[day + hour * HOUR_MS] for hour in range(24)]
        daily_prices[day] = (bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars), bars[-1][3])
    manifest = {'captured_from_ms': BASE + 60 * DAY_MS, 'files': {}}
    return HistoricalInputs(prices, dvol, hourly_dvol, daily_prices, manifest, 0, 0), config


class HistoricalReportTests(OfflineTest):
    def test_alignment_checks_every_ohlc_field_and_all_24_hours(self):
        inputs, config = aligned_inputs(5)
        self.assertTrue(alignment_checks(inputs, config['alignment_check_days_ms'])['passed'])
        modified = dict(inputs.alignment_prices)
        for field in range(4):
            candle = list(inputs.alignment_prices[BASE])
            candle[field] *= 1.01
            modified[BASE] = tuple(candle)
            self.assertFalse(alignment_checks(replace(inputs, alignment_prices=modified),
                                              config['alignment_check_days_ms'])['passed'])
        hourly = dict(inputs.alignment_dvol)
        del hourly[BASE + 12 * HOUR_MS]
        self.assertFalse(alignment_checks(replace(inputs, alignment_dvol=hourly),
                                          config['alignment_check_days_ms'])['passed'])

    def test_reports_reproduce_bytes_and_failed_alignment_withholds_claim(self):
        inputs, config = aligned_inputs(5)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'inputs' / 'manifest.json'
            with patch('implied_calibration.historical_report.load_study', return_value=config), \
                    patch('implied_calibration.historical_report.read_study', return_value=inputs):
                for name in ('one', 'two'):
                    result = write_historical_report(manifest, root / 'study.json', root / name,
                                                     provenance={'git_commit': 'a' * 40, 'working_tree_dirty': False})
                for filename in result['report_files']:
                    self.assertEqual((root / 'one' / filename).read_bytes(), (root / 'two' / filename).read_bytes())
                report = json.loads((root / 'one' / 'report.json').read_text())
                self.assertTrue(report['alignment']['passed'])
                self.assertFalse(report['synthetic'])
                self.assertEqual(report['primary']['status'], 'insufficient data')
                self.assertEqual(report['figure_sha256'], hashlib.sha256((root / 'one' / 'historical-variance.svg').read_bytes()).hexdigest())
                with self.assertRaisesRegex(ValueError, 'outside'):
                    write_historical_report(manifest, root / 'study.json', root / 'inputs' / 'output')
            with patch('implied_calibration.historical_report.load_study', return_value=config), \
                    patch('implied_calibration.historical_report.read_study', return_value=replace(inputs, alignment_dvol={})):
                write_historical_report(manifest, root / 'study.json', root / 'failed')
                report = json.loads((root / 'failed' / 'report.json').read_text())
                self.assertEqual(report['primary']['status'], 'alignment check failed')
