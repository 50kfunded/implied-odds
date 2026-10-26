"""Offline source-integrity and pagination checks for retrospective inputs."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock

from implied_calibration.client import ClientError, validate_response
from implied_calibration.historical_data import DAY_MS, HOUR_MS, DVOL, fetch_dvol, load_study, read_study
from implied_calibration.raw import RawWriter
from implied_calibration.store import digest
from test_phase1 import OfflineTest, envelope


class HistoricalDataTests(OfflineTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = load_study(Path(__file__).resolve().parents[1] / 'protocol/historical-variance.json')

    def test_eth_configuration_requires_matching_asset_and_pagination_currency(self):
        config = {**self.config, 'dvol_currency': 'ETH', 'price_instrument': 'ETH-PERPETUAL'}
        path = self.root/'eth-study.json'
        path.write_text(json.dumps(config))
        self.assertEqual(load_study(path)['dvol_currency'], 'ETH')
        path.write_text(json.dumps({**config, 'price_instrument': 'BTC-PERPETUAL'}))
        with self.assertRaises(ValueError):
            load_study(path)
        client = Mock()
        client.request.return_value.result = {'data': [], 'continuation': None}
        fetch_dvol(client, DAY_MS, 3*DAY_MS, currency='ETH')
        self.assertEqual(client.request.call_args.args[1]['currency'], 'ETH')

    def test_eth_reader_rejects_btc_recordings_even_with_a_matching_config_hash(self):
        path = self.sample()
        config = {**self.config, 'dvol_currency': 'ETH', 'price_instrument': 'ETH-PERPETUAL'}
        manifest = json.loads(path.read_text())
        manifest['config_hash'] = digest(config)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'source'):
            read_study(path, config)

    def test_dvol_schema_rejects_keys_types_order_and_ohlc(self):
        good = {'data': [[DAY_MS, 60., 62., 58., 61.]], 'continuation': None}
        self.assertEqual(validate_response(DVOL, 200, envelope(good)), good)
        bad = [dict(good, continuation=True), dict(good, extra=1), {'data': []},
               dict(good, data=[[DAY_MS, 60., 59., 58., 61.]]),
               dict(good, data=[[DAY_MS, 60., 62., 61., 61.]]),
               dict(good, data=[[True, 60., 62., 58., 61.]]),
               dict(good, data=good['data'] * 2), dict(good, data=[[DAY_MS, 0., 0., 0., 0.]])]
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ClientError):
                validate_response(DVOL, 200, envelope(result))
        with self.assertRaisesRegex(ClientError, 'testnet'):
            validate_response(DVOL, 200, envelope(good, testnet=True))

    def test_dvol_pagination_uses_continuation_and_is_paced(self):
        client, sleep = Mock(), Mock()
        client.request.side_effect = [Mock(result={'data': [[2 * DAY_MS, 60, 61, 59, 60]], 'continuation': DAY_MS}),
                                      Mock(result={'data': [[DAY_MS, 60, 61, 59, 60]], 'continuation': None})]
        result = fetch_dvol(client, DAY_MS, 3 * DAY_MS, sleep=sleep)
        self.assertEqual(result['pages'], 2)
        self.assertEqual(client.request.call_args_list[1].args[1]['end_timestamp'], DAY_MS)
        sleep.assert_called_once_with(10.)
        self.assertEqual(client.request.call_args_list[0].args[1]['resolution'], '1D')

    def test_dvol_pagination_rejects_no_progress_and_outside_request(self):
        client = Mock()
        client.request.return_value.result = {'data': [[DAY_MS, 60, 61, 59, 60]], 'continuation': 2 * DAY_MS - 1}
        with self.assertRaisesRegex(ValueError, 'progress'):
            fetch_dvol(client, DAY_MS, 2 * DAY_MS)
        client.request.return_value.result = {'data': [[3 * DAY_MS, 60, 61, 59, 60]], 'continuation': None}
        with self.assertRaisesRegex(ValueError, 'outside'):
            fetch_dvol(client, DAY_MS, 2 * DAY_MS)

    def sample(self, conflict=False):
        stamp = 4 * DAY_MS
        writer = RawWriter(self.root / 'dvol/raw')
        for close in (60., 61. if conflict else 60.):
            writer.append({'endpoint': DVOL, 'params': {'currency': 'BTC', 'resolution': '1D',
                'start_timestamp': DAY_MS, 'end_timestamp': 2 * DAY_MS - 1}, 'fetched_at_ms': stamp,
                'request_sent_ms': stamp, 'response_received_ms': stamp, 'http_status': 200, 'ok': True,
                'body': envelope({'data': [[DAY_MS, 60., 62., 58., close]], 'continuation': None})})
        files = {p.relative_to(self.root).as_posix(): {'bytes': p.stat().st_size,
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in self.root.rglob('*.gz')}
        manifest = {'schema_version': 1, 'kind': 'historical_dvol_variance_inputs', 'config_hash': digest(self.config),
                    'captured_from_ms': stamp, 'captured_until_ms': stamp, 'files': files, 'failed_attempts': 0}
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(manifest), encoding='utf-8')
        return path

    def test_identical_overlap_is_counted_but_conflicting_revision_is_rejected(self):
        path = self.sample()
        result = read_study(path, self.config)
        self.assertEqual(result.duplicate_candles, 1)
        self.assertEqual(len(result.dvol), 1)
        for p in (self.root / 'dvol/raw').glob('*.gz'):
            p.unlink()
        path = self.sample(conflict=True)
        with self.assertRaisesRegex(ValueError, 'revision'):
            read_study(path, self.config)

    def test_source_tampering_and_unlisted_files_fail_before_analysis(self):
        path = self.sample()
        extra = self.root / 'dvol/raw/extra.gz'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            read_study(path, self.config)
        extra.unlink()
        raw = next((self.root / 'dvol/raw').glob('*.gz'))
        raw.write_bytes(raw.read_bytes() + b'extra')
        with self.assertRaisesRegex(ValueError, 'hash'):
            read_study(path, self.config)

    def test_study_requires_the_boundary_prices_and_full_horizon(self):
        for field, value in [('price_start_ms', self.config['origin_start_ms']),
                             ('price_end_ms', self.config['origin_end_ms']), ('horizon_hours', 24),
                             ('expected_block_days', 14), ('price_resolution', '3600')]:
            changed = deepcopy(self.config)
            changed[field] = value
            path = self.root / 'study.json'
            path.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_study(path)

    def test_daily_price_alignment_accepts_only_intersecting_08_utc_sessions(self):
        for offset, accepted in ((-16, True), (8, True), (0, False), (-40, False), (32, False)):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                day, stamp = 10 * DAY_MS, 20 * DAY_MS
                writer = RawWriter(root / 'alignment/raw')
                values = {'status': 'ok', 'ticks': [day + offset * HOUR_MS], **{name: [100.] for name in
                          ('open', 'high', 'low', 'close', 'volume', 'cost')}}
                writer.append({'endpoint': 'public/get_tradingview_chart_data', 'params': {'instrument_name': 'BTC-PERPETUAL',
                    'resolution': '1D', 'start_timestamp': day, 'end_timestamp': day + DAY_MS - 1},
                    'fetched_at_ms': stamp, 'request_sent_ms': stamp, 'response_received_ms': stamp,
                    'http_status': 200, 'ok': True, 'body': envelope(values)})
                manifest = {'schema_version': 1, 'kind': 'historical_dvol_variance_inputs', 'config_hash': digest(self.config),
                    'captured_from_ms': stamp, 'captured_until_ms': stamp, 'failed_attempts': 0,
                    'files': {p.relative_to(root).as_posix(): {'bytes': p.stat().st_size,
                    'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.rglob('*.gz')}}
                path = root / 'manifest.json'
                path.write_text(json.dumps(manifest), encoding='utf-8')
                if accepted:
                    self.assertEqual(len(read_study(path, self.config).alignment_prices), 1)
                else:
                    with self.assertRaisesRegex(ValueError, 'timing'):
                        read_study(path, self.config)
