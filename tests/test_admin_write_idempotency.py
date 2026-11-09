"""New logical writes are distinct; retries preserve one intent's identity."""
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlsplit

import tool


def api():
    return tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url='http://admin.example.invalid',
                            sub2api_admin_key='synthetic'))


class AdminWriteIdempotencyTests(unittest.TestCase):
    def test_delete_then_recreate_same_payload_cannot_replay_deleted_id(self):
        cache, alive = {}, {}
        created = []
        def server(method, url, headers, body, **kwargs):
            path = urlsplit(url).path
            if method == 'POST':
                key = headers['Idempotency-Key']
                if key in cache:
                    return cache[key]
                aid = len(created)+1
                created.append(aid)
                alive[aid] = dict(body)
                cache[key] = (200, json.dumps({'data':{'id':aid}}))
                return cache[key]
            self.assertEqual(method, 'DELETE')
            del alive[int(path.rsplit('/',1)[1])]
            return 200, '{}'
        client = api()
        payload = {'name':'synthetic-group','platform':'openai'}
        with patch.object(tool, '_http', side_effect=server):
            first = client.create_group(payload)['data']['id']
            client._call('DELETE', '/api/v1/admin/groups/%d' % first)
            second = client.create_group(payload)['data']['id']
        self.assertNotEqual(first, second)
        self.assertIn(second, alive)
        self.assertEqual(created, [1,2])

    def test_new_identical_mutating_helper_calls_get_new_keys(self):
        client = api()
        calls = [lambda:client.create_proxy({'name':'synthetic'}),
                 lambda:client.batch_accounts([{'name':'synthetic'}]),
                 lambda:client.update_account(7, {'status':'active'}),
                 lambda:client.update_group(7, {'name':'synthetic'}),
                 lambda:client.bulk_update_accounts([7], {'schedulable':True}),
                 lambda:client.create_test_plan({'account_id':7,'cron_expression':'0 3 * * 0'})]
        for call in calls:
            with patch.object(tool, '_http', return_value=(200,'{}')) as transport:
                call(); call()
            keys = [item.args[2]['Idempotency-Key'] for item in transport.call_args_list]
            self.assertEqual(len(keys), 2)
            self.assertNotEqual(keys[0], keys[1])

    def test_account_batch_delete_then_recreate_same_payload_gets_a_new_id(self):
        cache, alive, created = {}, set(), []
        def server(method, url, headers, body, **kwargs):
            if method == 'DELETE':
                alive.remove(int(urlsplit(url).path.rsplit('/',1)[1]))
                return 200,'{}'
            self.assertEqual(urlsplit(url).path, '/api/v1/admin/accounts/batch')
            key = headers['Idempotency-Key']
            if key not in cache:
                aid = len(created)+1
                created.append(aid)
                alive.add(aid)
                cache[key] = (200,json.dumps({'data':{'success':[{'id':aid,'name':'synthetic-account'}]}}))
            return cache[key]
        client, payload = api(), [{'name':'synthetic-account'}]
        with patch.object(tool, '_http', side_effect=server):
            first = client.batch_accounts(payload)['data']['success'][0]['id']
            client._call('DELETE', '/api/v1/admin/accounts/%d' % first)
            second = client.batch_accounts(payload)['data']['success'][0]['id']
        self.assertNotEqual(first, second)
        self.assertIn(second, alive)
        self.assertEqual(created, [1,2])

    def test_bulk_update_same_payload_executes_again_after_operator_change(self):
        state, cache, writes = {'schedulable':False}, {}, []
        def server(method, url, headers, body, **kwargs):
            self.assertEqual(method, 'POST')
            key = headers['Idempotency-Key']
            if key not in cache:
                state.update({k:v for k,v in body.items() if k != 'account_ids'})
                writes.append(dict(body))
                cache[key] = (200,'{}')
            return cache[key]
        client = api()
        with patch.object(tool, '_http', side_effect=server):
            client.bulk_update_accounts([7], {'schedulable':True})
            self.assertTrue(state['schedulable'])
            state['schedulable'] = False
            client.bulk_update_accounts([7], {'schedulable':True})
        self.assertTrue(state['schedulable'])
        self.assertEqual(len(writes), 2)

    def test_rate_limit_retry_preserves_the_same_key(self):
        error = tool.ApiError(429, 'limited', '<redacted>')
        error.retry_after = '1'
        with patch.object(tool, '_http', side_effect=[error, (200,'{}')]) as transport, \
                patch.object(tool.time, 'sleep'):
            api().create_group({'name':'synthetic'})
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args_list[0].args[2]['Idempotency-Key'],
                         transport.call_args_list[1].args[2]['Idempotency-Key'])

    def test_network_transport_retry_preserves_the_same_key(self):
        keys = []
        class Response:
            status, headers = 200, {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *args): return b'{}'
        class Opener:
            def open(self, request, timeout):
                keys.append(next(value for name,value in request.header_items() if name.lower() == 'idempotency-key'))
                if len(keys) == 1: raise URLError('synthetic interrupted connection')
                return Response()
        settings = dict(tool.DEFAULTS, http_retries=2, http_use_proxy=False)
        with patch.object(tool, 'load_settings', return_value=settings), \
                patch.object(tool.urllib.request, 'build_opener', return_value=Opener()), \
                patch.object(tool.time, 'sleep'):
            self.assertEqual(api().create_group({'name':'synthetic'}), {})
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_raw_mutating_calls_fill_one_key_reads_and_explicit_intent_are_preserved(self):
        client = api()
        with patch.object(tool, '_http', return_value=(200,'{}')) as transport:
            client._call('POST', '/api/v1/admin/synthetic', {'value':1})
            client._call('GET', '/api/v1/admin/synthetic')
            client._call('POST', '/api/v1/admin/synthetic', {'value':1}, idem='explicit-logical-intent')
        headers = [item.args[2] for item in transport.call_args_list]
        self.assertTrue(headers[0].get('Idempotency-Key'))
        self.assertNotIn('Idempotency-Key', headers[1])
        self.assertEqual(headers[2]['Idempotency-Key'], 'explicit-logical-intent')

    def test_all_raw_mutation_methods_get_independent_intents(self):
        client = api()
        for method in ('POST','PUT','PATCH','DELETE'):
            with self.subTest(method=method), patch.object(tool, '_http', return_value=(200,'{}')) as transport:
                client._call(method, '/api/v1/admin/synthetic', {'value':1})
                client._call(method, '/api/v1/admin/synthetic', {'value':1})
            first, second = [call.args[2]['Idempotency-Key'] for call in transport.call_args_list]
            self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()
