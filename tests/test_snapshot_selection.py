"""I check that a forecast only cleans its selected snapshot and keeps each expiry gate."""

from unittest.mock import patch

from implied_calibration import cleaning
from implied_calibration.cleaning import clean_database
from implied_calibration.fitting import fit_database
from implied_calibration.predictions import freeze_predictions
from test_phase2 import BASE, DatabaseFixture, ingest, quote
from test_phase3 import draft, synthetic_chain
from test_phase6 import COMMIT, DAY, EXPIRY, locked_fixture


class SnapshotSelectionTests(DatabaseFixture):
    def seed_chain(self, stamp, mark_iv=60):
        rows = synthetic_chain()
        self.seed([r['instrument_name'] for r in rows], stamp=stamp)
        identity = self.book(stamp=stamp, rows=[quote(r['instrument_name'], bid_price=r['bid_btc'],
            ask_price=r['ask_btc'], mark_price=r['mark_btc'], mark_iv=mark_iv, underlying_price=100) for r in rows])
        ingest(self.recording, self.store)
        return identity

    def test_old_flag_stays_rejected_without_vetoing_a_new_snapshot(self):
        self.seed_chain(BASE, 47)
        current = self.seed_chain(BASE + 1000)
        whole = clean_database(self.store.path, draft(), BASE + 1000)
        self.assertEqual(whole['group_status_counts'], {'investigate': 1, 'ok': 1})
        with patch('implied_calibration.cleaning._clean_quote', wraps=cleaning._clean_quote) as cleaned:
            result = fit_database(self.store.path, draft(), BASE + 1000, snapshot_id=current)
        self.assertEqual(result['n_primary_accepted'], 1)
        self.assertEqual(len(cleaned.call_args_list), 6)
        self.assertEqual({args.args[0]['snapshot_id'] for args in cleaned.call_args_list}, {current})
        fitted = fit_database(self.store.path, draft(), BASE + 1000)
        self.assertEqual(fitted['n_fit_attempted'], 1)
        self.assertEqual(fitted['fit_status_counts'], {'blocked_upstream': 1, 'ok': 1})

    def test_selected_investigation_keeps_its_original_rejection(self):
        selected = self.seed_chain(BASE, 47)
        self.seed_chain(BASE + 1000)
        with patch('implied_calibration.fitting.fit_svi', side_effect=AssertionError('Rejected group fitted')):
            result = fit_database(self.store.path, draft(), BASE + 1000, snapshot_id=selected)
        self.assertEqual(result['status'], 'blocked_upstream')
        self.assertIn('iv_convention_investigation', result['groups'][0]['cleaning_reasons'])

    def test_sql_cutoff_excludes_a_selected_future_snapshot(self):
        selected = self.seed_chain(BASE + 1000)
        with patch('implied_calibration.cleaning._clean_quote', side_effect=AssertionError('Future row read')):
            result = clean_database(self.store.path, draft(), BASE, snapshot_id=selected)
        self.assertEqual(result['n_quotes'], 0)

    def test_a_shared_selected_snapshot_is_fitted_once_for_two_due_targets(self):
        self.book(stamp=EXPIRY - DAY)
        ingest(self.recording, self.store)
        groups = [{'expiry_ms': expiry, 'fetched_at_ms': EXPIRY - DAY, 'cdf_status': 'fit_rejected',
                   'forward_usd': 100., 't_years': 1 / 365, 'fit': {'params': None},
                   'distribution': None, 'cdf_message': 'Synthetic rejection'} for expiry in (EXPIRY, EXPIRY + 6 * DAY)]
        with patch('implied_calibration.predictions.cdf_database', return_value={'groups': groups}) as fitted:
            result = freeze_predictions(self.store.path, locked_fixture(), EXPIRY - DAY, EXPIRY - DAY,
                                        COMMIT, expiries=[EXPIRY, EXPIRY + 6 * DAY])
        self.assertEqual(fitted.call_count, 1)
        self.assertEqual(sum(r['status'] == 'fit_rejected' for r in result['predictions']), 2)
