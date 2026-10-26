"""A changed JSON number cannot silently leave a stale manuscript macro."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from implied_calibration.paper_numbers import check_numbers, expression_value, render_numbers


ROOT = Path(__file__).resolve().parents[1]


class PaperNumberTests(unittest.TestCase):
    def test_every_computed_macro_matches_retained_json(self):
        summary = json.loads((ROOT/'report/incremental-results.json').read_text(encoding='utf-8'))
        source = (ROOT/'report/latex/main.tex').read_text(encoding='utf-8')
        v2_path = ROOT/'report/v2-results.json'
        v2 = json.loads(v2_path.read_text(encoding='utf-8')) if v2_path.exists() else None
        self.assertGreaterEqual(check_numbers(source, summary, v2), 162)

    def test_result_drift_fails_and_render_uses_the_new_json_value(self):
        summary = json.loads((ROOT/'report/incremental-results.json').read_text(encoding='utf-8'))
        changed = deepcopy(summary)
        changed['summary']['metrics']['qlike_dvol_minus_har']['mean'] = -.03123
        source = '% $.summary.metrics.qlike_dvol_minus_har.mean; format .5f\n\\newcommand{\\RawHarMean}{-0.02937}'
        with self.assertRaisesRegex(ValueError, 'drifted'):
            check_numbers(source, changed)
        rendered, count = render_numbers(source, changed)
        self.assertEqual(count, 1)
        self.assertIn('{-0.03123}', rendered)

    def test_selector_language_rejects_executable_expressions(self):
        with self.assertRaises(ValueError):
            expression_value('__import__("os").getcwd()', {})
        self.assertEqual(expression_value('min($.values[*]) * 100', {'values': [.2, .1]}), 10.)
        self.assertEqual(expression_value('len($.values)', {'values': [.2, .1]}), 2)

    def test_v2_values_require_the_separate_source(self):
        source = '% v2.$.n; format ,d\n\\newcommand{\\VTwoOrigins}{1{,}176}'
        with self.assertRaisesRegex(ValueError, 'separate v2'):
            check_numbers(source, {})
        self.assertEqual(check_numbers(source, {}, {'n': 1176}), 1)
