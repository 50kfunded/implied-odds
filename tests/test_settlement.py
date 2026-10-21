"""Retained official-delivery-page checks use synthetic responses."""

from unittest.mock import Mock

from implied_calibration.client import ApiResponse, ClientError, validate_response
from implied_calibration.settlement import delivery_expiry_ms, fetch_delivery_pages, ingest_deliveries
from test_phase1 import envelope
from test_phase2 import DatabaseFixture

EXPIRY = delivery_expiry_ms('2026-12-05')


def delivery_page(stamp, entries=None, offset=0, count=1000, total=None):
    entries = [{'date': '2026-12-05', 'delivery_price': 101.0}] if entries is None else entries
    return {'endpoint': 'public/get_delivery_prices', 'params': {'index_name': 'btc_usd', 'offset': offset, 'count': count},
            'fetched_at_ms': stamp, 'request_sent_ms': stamp, 'response_received_ms': stamp, 'latency_ms': 0,
            'http_status': 200, 'ok': True, 'reason': 'ok',
            'body': envelope({'data': entries, 'records_total': len(entries) if total is None else total})}



class SettlementTests(DatabaseFixture):
    def test_delivery_pages_are_idempotent_and_keep_source_hash(self):
        self.write(delivery_page(EXPIRY+100))
        first = ingest_deliveries(self.recording, self.store.path, EXPIRY+100)
        second = ingest_deliveries(self.recording, self.store.path, EXPIRY+100)
        self.assertEqual((first['added'],second['added'],second['unchanged']), (1,0,1))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_observations').fetchone()[0], 1)

    def test_conflicting_price_rolls_back_and_early_observation_is_rejected(self):
        self.write(delivery_page(EXPIRY+100))
        ingest_deliveries(self.recording, self.store.path, EXPIRY+100)
        self.write(delivery_page(EXPIRY+200,[{'date':'2026-12-05','delivery_price':102.}]))
        with self.assertRaises(ValueError):
            ingest_deliveries(self.recording, self.store.path, EXPIRY+200)
        self.assertEqual(self.store.connection.execute('SELECT delivery_price FROM delivery_prices').fetchone()[0], 101.)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_sources').fetchone()[0], 1)

    def test_date_maps_to_eight_utc_and_cannot_be_ingested_before_expiry(self):
        self.assertEqual(delivery_expiry_ms('2026-12-05'), EXPIRY)
        self.write(delivery_page(EXPIRY-1))
        with self.assertRaisesRegex(ValueError, 'predates'):
            ingest_deliveries(self.recording, self.store.path, EXPIRY)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM delivery_prices').fetchone()[0], 0)

    def test_central_schema_rejects_bad_fields_dates_and_testnet(self):
        for result in ({'data':[]}, {'data':[{'date':'2026-02-30','delivery_price':1.}], 'records_total':1},
                       {'data':[{'date':'2026-12-05','delivery_price':0.}], 'records_total':1},
                       {'data':[], 'records_total':0, 'extra':1}):
            with self.assertRaises(ClientError):
                validate_response('public/get_delivery_prices',200,envelope(result))
        body = delivery_page(EXPIRY+1)['body']
        body['testnet'] = True
        with self.assertRaises(ClientError):
            validate_response('public/get_delivery_prices',200,body)

    def test_pagination_advances_and_paces_without_network(self):
        client, sleep = Mock(), Mock()
        client.request.side_effect = [ApiResponse({'data':[{'date':'2026-12-05','delivery_price':101.}], 'records_total':2},None),
                                      ApiResponse({'data':[{'date':'2026-12-04','delivery_price':100.}], 'records_total':2},None)]
        report = fetch_delivery_pages(client,count=1,sleep=sleep)
        self.assertEqual(report['records'],2)
        self.assertEqual(client.request.call_args_list[1].args[1]['offset'],1)
        sleep.assert_called_once_with(10.)

    def test_pagination_rejects_empty_repeat_total_change_and_overrun(self):
        first = {'data':[{'date':'2026-12-05','delivery_price':101.}], 'records_total':2}
        for second in ({'data':[], 'records_total':2}, first,
                       {'data':[{'date':'2026-12-04','delivery_price':100.}], 'records_total':3}):
            client = Mock()
            client.request.side_effect = [ApiResponse(first,None),ApiResponse(second,None)]
            with self.assertRaises(ValueError):
                fetch_delivery_pages(client,count=1,sleep=Mock())
        client = Mock()
        client.request.return_value = ApiResponse({'data':[{},{}], 'records_total':1},None)
        with self.assertRaises(ValueError):
            fetch_delivery_pages(client,count=2,sleep=Mock())

