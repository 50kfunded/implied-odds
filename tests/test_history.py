"""I check hourly-candle provenance, schema and rolling-window availability offline."""

from copy import deepcopy
from unittest.mock import Mock

import numpy as np

from implied_calibration.client import ApiResponse, ClientError, validate_response
from implied_calibration.history import ENDPOINT, HOUR_MS, fetch_history, ingest_history, trailing_returns
from test_phase1 import envelope
from test_phase2 import DatabaseFixture


def history_page(start, prices, stamp, *, ticks=None):
    ticks = [start+i*HOUR_MS for i in range(len(prices))] if ticks is None else ticks
    result = {'status':'ok','ticks':ticks,'open':prices,'high':prices,'low':prices,'close':prices,
              'volume':[1.]*len(prices),'cost':[1.]*len(prices)}
    return {'endpoint':ENDPOINT,'params':{'instrument_name':'BTC-PERPETUAL','resolution':'60',
            'start_timestamp':start,'end_timestamp':ticks[-1]+HOUR_MS-1},'fetched_at_ms':stamp,
            'request_sent_ms':stamp,'response_received_ms':stamp,'latency_ms':0,'http_status':200,
            'ok':True,'reason':'ok','body':envelope(result)}


class HourlyHistoryTests(DatabaseFixture):
    def test_ingestion_is_idempotent_and_excludes_incomplete_candles(self):
        self.write(history_page(0,[100.,101.,102.],2*HOUR_MS))
        first = ingest_history(self.recording,self.store.path,2*HOUR_MS)
        second = ingest_history(self.recording,self.store.path,2*HOUR_MS)
        self.assertEqual((first['added'],second['added'],second['unchanged']),(2,0,2))
        self.assertEqual(first['reason_counts'],{'unfinished_candle':1})
        rows = self.store.connection.execute('SELECT * FROM hourly_observations ORDER BY open_ms').fetchall()
        self.assertEqual(rows[0]['close_ms'],HOUR_MS)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM history_sources').fetchone()[0],1)

    def test_history_requires_both_close_time_and_fetch_time_before_cutoff(self):
        self.write(history_page(0,[100.,101.,103.],3*HOUR_MS+1))
        ingest_history(self.recording,self.store.path,4*HOUR_MS)
        early = trailing_returns(self.store.connection,3*HOUR_MS,2,2*HOUR_MS)
        self.assertEqual(early['status'],'short_history')
        late = trailing_returns(self.store.connection,3*HOUR_MS+1,2,2*HOUR_MS)
        self.assertEqual(late['status'],'ok')
        np.testing.assert_allclose(late['returns'],np.diff(np.log([100.,101.,103.])))

    def test_future_revision_does_not_change_old_asof_but_rejects_later_window(self):
        self.write(history_page(0,[100.,101.,102.],3*HOUR_MS))
        ingest_history(self.recording,self.store.path,3*HOUR_MS)
        before = trailing_returns(self.store.connection,3*HOUR_MS,2,2*HOUR_MS)
        self.write(history_page(0,[100.,105.,102.],3*HOUR_MS+1))
        ingest_history(self.recording,self.store.path,4*HOUR_MS)
        after = trailing_returns(self.store.connection,3*HOUR_MS,2,2*HOUR_MS)
        np.testing.assert_array_equal(before.pop('returns'),after.pop('returns'))
        self.assertEqual(before,after)
        self.assertEqual(trailing_returns(self.store.connection,3*HOUR_MS+1,2,2*HOUR_MS)['status'],'revised_history')

    def test_gap_stale_and_no_data_have_explicit_reasons(self):
        self.write(history_page(0,[100.,101.,103.],4*HOUR_MS,ticks=[0,HOUR_MS,3*HOUR_MS]))
        ingest_history(self.recording,self.store.path,4*HOUR_MS)
        self.assertEqual(trailing_returns(self.store.connection,4*HOUR_MS,2,2*HOUR_MS)['status'],'history_gap')
        self.assertEqual(trailing_returns(self.store.connection,7*HOUR_MS,2,2*HOUR_MS)['status'],'stale_history')
        no_data = history_page(0,[100.],HOUR_MS)
        no_data['body'] = envelope({'status':'no_data'})
        self.write(no_data)
        self.assertEqual(ingest_history(self.recording,self.store.path,7*HOUR_MS)['reason_counts']['no_data'],1)

    def test_schema_rejects_bad_lengths_keys_order_ohlc_and_testnet(self):
        good = history_page(0,[100.,101.],2*HOUR_MS)['body']
        bad = []
        for key,value in (('close',[100.]),('ticks',[0,0]),('ticks',[0,1]),('high',[1.,1.]),('unexpected',[])):
            body = deepcopy(good)
            body['result'][key] = value
            bad.append(body)
        body=deepcopy(good)
        del body['result']['open']
        bad.append(body)
        body=deepcopy(good)
        body['testnet']=True
        bad.append(body)
        for body in bad:
            with self.assertRaises(ClientError):
                validate_response(ENDPOINT,200,body)
        with self.assertRaisesRegex(ClientError,'missing keys=.*open'):
            validate_response(ENDPOINT,200,bad[-2])

    def test_fetch_chunks_are_disjoint_and_paced_without_network(self):
        client,sleep=Mock(),Mock()
        client.request.return_value=ApiResponse({'status':'no_data'},None)
        report=fetch_history(client,0,721*HOUR_MS,sleep=sleep)
        self.assertEqual(report['pages'],2)
        first,second=(call.args[1] for call in client.request.call_args_list)
        self.assertEqual(first['end_timestamp']+1,second['start_timestamp'])
        self.assertEqual(second['resolution'],'60')
        sleep.assert_called_once_with(10.)
        with self.assertRaises(ValueError):
            fetch_history(client,1,HOUR_MS)
