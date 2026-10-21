"""Offline cleaning checks use synthetic option chains and captured-row fixtures."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

from implied_calibration.blackscholes import price_usd
from implied_calibration.cleaning import clean_database, clean_expiry, effective_tick
from implied_calibration.cli import main
from implied_calibration.ingest import ingest
from implied_calibration.instruments import parse_name
from implied_calibration.protocol import load_protocol, validate_protocol
from test_phase2 import BASE, DatabaseFixture, quote


def draft():
    return load_protocol(Path(__file__).resolve().parents[1] / 'protocol' / 'protocol.json')


def synthetic_chain():
    rows = []
    for strike, side in [(70, 'P'), (80, 'P'), (90, 'P'), (100, 'C'), (110, 'C'), (120, 'C')]:
        name = f'BTC-5DEC26-{strike}-{side}'
        expiry = parse_name(name).expiry_ms
        option_type = 'put' if side == 'P' else 'call'
        t = (expiry - BASE) / (365 * 24 * 3600 * 1000)
        mid = price_usd(100, strike, t, 0.6, option_type) / 100
        rows.append({'snapshot_id': 'synthetic', 'instrument_name': name,
                     'expiry_ms': expiry, 'strike': float(strike), 'option_type': option_type,
                     'fetched_at_ms': BASE, 'metadata_fetched_at_ms': BASE - 20,
                     'metadata_time': BASE - 20, 'metadata_expiry_ms': expiry,
                     'metadata_strike': float(strike), 'metadata_option_type': option_type,
                     'identity_status': 'verified', 'is_active': 1,
                     'tick_size': 1e-6, 'tick_size_steps': None,
                     'underlying_price': 100.0, 'underlying_index': 'SYN.synthetic',
                     'bid_btc': mid * 0.99, 'ask_btc': mid * 1.01,
                     'mark_btc': mid, 'mark_iv_pct': 60.0, 'interest_rate': 0.0})
    return rows


class CleaningTests(DatabaseFixture):
    def result(self, rows=None, protocol=None):
        return clean_expiry(synthetic_chain() if rows is None else rows, protocol or draft())

    def test_flat_vol_chain_units_midpoint_price_and_atm_side(self):
        group = self.result()
        self.assertEqual(group['status'], 'ok')
        self.assertEqual((group['candidate_strikes'], group['strikes_below_forward'],
                          group['strikes_above_forward']), (6, 3, 2))
        for row in group['quotes']:
            self.assertEqual(row['status'], 'usable')
            self.assertAlmostEqual(row['iv_mid'], 0.6, places=12)
            self.assertAlmostEqual(row['mid_btc'], (row['bid_btc'] + row['ask_btc']) / 2)
            self.assertNotAlmostEqual(row['iv_mid'], (row['iv_bid'] + row['iv_ask']) / 2, places=9)
            self.assertEqual(row['mark_iv_pct'], 60)

    def test_each_quote_filter_gets_a_reason(self):
        cases = [({'bid_btc': None}, 'no_bid'), ({'bid_btc': 0}, 'no_bid'),
                 ({'ask_btc': None}, 'no_ask'), ({'ask_btc': 0}, 'no_ask'),
                 ({'bid_btc': -1}, 'invalid_bid'), ({'ask_btc': -1}, 'invalid_ask'),
                 ({'bid_btc': 0.2, 'ask_btc': 0.1}, 'crossed'),
                 ({'bid_btc': 1e-8, 'ask_btc': 1.1e-8}, 'mid_below_ticks'),
                 ({'bid_btc': 0.01, 'ask_btc': 0.1}, 'spread_too_wide'),
                 ({'underlying_price': None}, 'invalid_forward'),
                 ({'underlying_price': 101.0}, 'inconsistent_forward'),
                 ({'identity_status': 'missing_metadata', 'metadata_time': None}, 'unverified_metadata'),
                 ({'is_active': 0}, 'inactive_instrument'),
                 ({'tick_size': 0}, 'invalid_tick_metadata'),
                 ({'metadata_strike': 71}, 'metadata_identity_mismatch'),
                 ({'option_type': 'call', 'metadata_option_type': 'call'}, 'not_otm')]
        for change, reason in cases:
            with self.subTest(reason=reason, change=str(change)):
                rows = synthetic_chain()
                rows[0].update(change)
                group = self.result(rows)
                target = next(row for row in group['quotes'] if row['instrument_name'] == rows[0]['instrument_name'])
                self.assertIn(reason, target['reasons'])
                self.assertEqual(len(group['quotes']), len(rows))

    def test_maturity_boundaries_and_expiry(self):
        for hours, reason in [(0, 'expired'), (-1, 'expired'), (5.99, 'maturity_too_short'), (6, None)]:
            rows = synthetic_chain()
            for row in rows:
                row['fetched_at_ms'] = row['expiry_ms'] - int(hours * 3600000)
            group = self.result(rows)
            self.assertEqual(len(group['quotes']), 6)
            self.assertEqual(reason in group['quotes'][0]['reasons'] if reason else
                             'maturity_too_short' not in group['quotes'][0]['reasons'], True)

    def test_tick_step_strict_boundary_and_threshold_choices(self):
        steps = [{'above_price': 0.005, 'tick_size': 0.0005}, {'above_price': 0.01, 'tick_size': 0.001}]
        self.assertEqual(effective_tick(0.005, 0.0001, steps), 0.0001)
        self.assertEqual(effective_tick(math.nextafter(0.005, 1), 0.0001, steps), 0.0005)
        self.assertEqual(effective_tick(0.01, 0.0001, steps), 0.0005)
        self.assertEqual(effective_tick(0.011, 0.0001, steps), 0.001)
        with self.assertRaises(ValueError):
            effective_tick(1, 0.0001, list(reversed(steps)))
        choices = deepcopy(draft().content)
        choices['cleaning']['minimum_mid_ticks'] = 1e8
        group = self.result(protocol=validate_protocol(choices))
        self.assertTrue(all('mid_below_ticks' in row['reasons'] for row in group['quotes']))

    def test_group_gates_count_unique_strikes_and_strict_sides(self):
        group = self.result(synthetic_chain()[:-1])
        self.assertIn('insufficient_strikes', group['reasons'])
        self.assertIn('insufficient_sides', group['reasons'])
        self.assertTrue(all(row['status'] == 'rejected' for row in group['quotes']))
        rows = synthetic_chain()
        rows.append({**rows[0], 'instrument_name': 'synthetic-duplicate'})
        group = self.result(rows)
        self.assertEqual(sum('duplicate_otm_strike' in r['reasons'] for r in group['quotes']), 2)
        self.assertTrue(all(r['quote_status'] == 'rejected' for r in group['quotes']
                            if 'duplicate_otm_strike' in r['reasons']))
        self.assertIn('insufficient_strikes', group['reasons'])

    def test_inversion_failures_are_visible(self):
        rows = synthetic_chain()
        rows[0].update(bid_btc=1.1, ask_btc=1.2)
        target = next(row for row in self.result(rows)['quotes'] if row['strike'] == 70)
        self.assertEqual(target['iv_mid_reason'], 'at_or_above_upper_bound')
        self.assertIn('iv_mid_at_or_above_upper_bound', target['reasons'])
        self.assertIsNone(target['iv_mid'])
        json.dumps(self.result(rows), allow_nan=False)

    def test_mark_convention_and_mid_mark_gap_both_gate_progress(self):
        rows = synthetic_chain()
        for row in rows:
            row['mark_iv_pct'] = 61
        group = self.result(rows)
        self.assertEqual(group['iv_diagnostic']['status'], 'investigate_convention')
        self.assertIn('iv_convention_investigation', group['reasons'])
        self.assertAlmostEqual(group['iv_diagnostic']['mark_price_minus_mark_vol_points']['median'], -1)
        for row in rows:
            row['mark_iv_pct'] = 50
            row['mark_btc'] = price_usd(100, row['strike'],
                                      (row['expiry_ms'] - BASE) / (365 * 24 * 3600 * 1000),
                                      0.5, row['option_type']) / 100
        group = self.result(rows)
        self.assertEqual(group['iv_diagnostic']['status'], 'investigate_mid_mark_gap')
        self.assertIn('iv_mid_mark_investigation', group['reasons'])
        self.assertAlmostEqual(group['iv_diagnostic']['mid_minus_mark_vol_points']['median'], 10)

    def test_missing_marks_are_flagged_and_insufficient_diagnostics_block(self):
        rows = synthetic_chain()
        rows[0]['mark_iv_pct'] = None
        group = self.result(rows)
        self.assertIn('iv_diagnostic_unavailable', group['reasons'])
        target = next(row for row in group['quotes'] if row['strike'] == 70)
        self.assertIn('missing_or_invalid_mark_iv', target['flags'])
        self.assertIsNone(target['mid_minus_mark_vol_points'])

    def test_forward_median_precedes_price_filters_and_interest_is_only_logged(self):
        rows = synthetic_chain()
        rows[0]['underlying_price'] = 101
        rows[0]['bid_btc'] = None
        rows[1]['interest_rate'] = 99
        group = self.result(rows)
        self.assertEqual(group['forward_usd'], 100)
        self.assertEqual(group['quote_forwards']['max'], 101)
        target = next(row for row in group['quotes'] if row['strike'] == 80)
        self.assertAlmostEqual(target['iv_mid'], 0.6)
        self.assertIn('nonzero_interest_rate_ignored', target['flags'])

    def seed_chain(self):
        chain = synthetic_chain()
        names = [row['instrument_name'] for row in chain]
        self.seed(names)
        identity = self.book(rows=[quote(row['instrument_name'], bid_price=row['bid_btc'],
                                         ask_price=row['ask_btc'], mark_price=row['mark_btc'],
                                         mark_iv=row['mark_iv_pct'], underlying_price=100,
                                         interest_rate=0) for row in chain])
        ingest(self.recording, self.store)
        return identity

    def test_as_of_rows_and_exact_metadata_do_not_borrow_the_future(self):
        identity = self.seed_chain()
        before = clean_database(self.store.path, draft(), BASE)
        names = [row['instrument_name'] for row in synthetic_chain()]
        self.seed(names, stamp=BASE + 100)
        self.book(stamp=BASE + 100, rows=[quote(name) for name in names])
        ingest(self.recording, self.store)
        self.store.connection.execute('UPDATE instruments SET tick_size=1 WHERE fetched_at_ms>?', (BASE,))
        self.store.connection.commit()
        self.assertEqual(before, clean_database(self.store.path, draft(), BASE))
        self.assertEqual(before['n_quotes'], 6)
        self.assertTrue(all(row['snapshot_id'] == identity for g in before['groups'] for row in g['quotes']))
        self.assertEqual(clean_database(self.store.path, draft(), BASE - 1)['n_quotes'], 0)

    def test_unverified_rows_retained_and_cleaning_does_not_write_database(self):
        self.book()
        ingest(self.recording, self.store)
        before = self.store.counts()
        report = clean_database(self.store.path, draft(), BASE)
        self.assertEqual(report['n_quotes'], 1)
        self.assertIn('unverified_metadata', report['groups'][0]['quotes'][0]['reasons'])
        self.assertFalse(report['phase4_ready'])
        self.assertEqual(before, self.store.counts())

    def test_cli_writes_strict_json_and_returns_diagnostic_exit_code(self):
        self.book()
        ingest(self.recording, self.store)
        output = self.root / 'clean.json'
        argv = ['implied-calibration', 'clean', '--db', str(self.store.path), '--as-of-ms', str(BASE),
                '--protocol', str(Path(__file__).resolve().parents[1] / 'protocol' / 'protocol.json'),
                '--output', str(output)]
        with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        parsed = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(parsed['n_quotes'], 1)
        self.assertNotIn('NaN', output.read_text(encoding='utf-8'))
        self.assertFalse((self.root / 'missing.sqlite').exists())
        with self.assertRaises(sqlite3.OperationalError):
            clean_database(self.root / 'missing.sqlite', draft(), BASE)
        self.assertFalse((self.root / 'missing.sqlite').exists())

    def test_protocol_rejects_missing_extra_bad_types_and_duplicate_keys(self):
        for section, key, value in [('cleaning', 'minimum_mid_ticks', 0),
                                    ('cleaning', 'minimum_unique_strikes', True),
                                    ('inversion', 'relative_tolerance', 1e-20),
                                    ('inversion', 'max_iterations', 3.5),
                                    ('diagnostics', 'minimum_mark_comparisons', 0),
                                    ('cleaning', 'minimum_mid_ticks', 10 ** 400)]:
            data = deepcopy(draft().content)
            data[section][key] = value
            with self.subTest(key=key, value=str(value)), self.assertRaises(ValueError):
                validate_protocol(data)
        data = deepcopy(draft().content)
        del data['cleaning']['minimum_mid_ticks']
        with self.assertRaisesRegex(ValueError, 'missing keys=.*minimum_mid_ticks'):
            validate_protocol(data)
        data = deepcopy(draft().content)
        data['extra'] = 1
        with self.assertRaisesRegex(ValueError, 'unexpected keys=.*extra'):
            validate_protocol(data)
        duplicate = self.root / 'duplicate.json'
        duplicate.write_text('{"version":1,"version":1}', encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            load_protocol(duplicate)
        self.assertEqual(draft().content_hash, validate_protocol(deepcopy(draft().content)).content_hash)

    def test_default_cutoff_is_latest_capture_and_mutated_protocol_is_rejected(self):
        self.seed_chain()
        self.assertEqual(clean_database(self.store.path, draft()),
                         clean_database(self.store.path, draft(), BASE))
        mutated = draft()
        mutated.content['cleaning']['minimum_mid_ticks'] = 3
        with self.assertRaisesRegex(ValueError, 'changed after hashing'):
            self.result(protocol=mutated)
