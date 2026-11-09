"""Automatic probe markers and private quota headers never become business traffic."""
import hashlib
import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from relay import server
from relay.config import RelayError

try:
    import aiohttp
    from aiohttp import web
    from multidict import CIMultiDict
except ImportError:
    aiohttp = None


class AutomaticMarkerContractTests(unittest.TestCase):
    def route(self, section='openai-compatibility', model='gpt-test'):
        rid = 'a' * 40
        return {'id': rid, 'prefix': 'r' + rid, 'diagnostic_prefix': 'd' + rid,
                'automatic_prefix': 'a' + rid, 'source_section': section, 'models': [model]}

    def marker(self, route, automatic=True):
        return ('a' if automatic else 'd') + route['id'] + '-' + hashlib.sha256(route['models'][0].encode()).hexdigest()

    def test_automatic_marker_resolves_same_model_under_native_d_guard(self):
        route = self.route()
        marker = self.marker(route)
        resolved, path, data, marked = server.resolve_diagnostic_request(
            route, '/v1/chat/completions', json.dumps({'model': marker, 'messages': [{'content': marker}]}).encode())
        self.assertTrue(marked)
        self.assertTrue(resolved.get('automatic_probe'))
        self.assertEqual(resolved['prefix'], route['diagnostic_prefix'])
        self.assertEqual(json.loads(data)['model'], route['models'][0])
        self.assertEqual(json.loads(data)['messages'][0]['content'], marker)
        self.assertEqual(path, '/v1/chat/completions')

    def test_manual_marker_remains_manual_and_ordinary_model_is_unchanged(self):
        route = self.route()
        resolved, _, _, marked = server.resolve_diagnostic_request(
            route, '/v1/chat/completions', json.dumps({'model': self.marker(route, False)}).encode())
        self.assertTrue(marked)
        self.assertFalse(resolved.get('automatic_probe', False))
        ordinary = json.dumps({'model': route['models'][0]}).encode()
        resolved, _, result, marked = server.resolve_diagnostic_request(route, '/v1/chat/completions', ordinary)
        self.assertFalse(marked)
        self.assertEqual(resolved, route)
        self.assertEqual(result, ordinary)

    def test_automatic_gemini_path_marker_is_exactly_route_and_model_bound(self):
        route = self.route('gemini-api-key', 'gemini-test')
        marker = self.marker(route)
        resolved, path, _, marked = server.resolve_diagnostic_request(route, '/v1beta/models/' + marker + ':generateContent', b'{}')
        self.assertTrue(marked)
        self.assertTrue(resolved.get('automatic_probe'))
        self.assertEqual(path, '/v1beta/models/gemini-test:generateContent')
        self.assertEqual(len(marker), 106)

    def test_foreign_unknown_and_legacy_automatic_models_fail_closed(self):
        route = self.route()
        for marker in ('a' + 'b' * 40 + '-' + hashlib.sha256(b'gpt-test').hexdigest(),
                       route['automatic_prefix'] + '-' + '0' * 64,
                       route['automatic_prefix'] + '/gpt-test'):
            with self.subTest(marker=marker), self.assertRaises(RelayError):
                server.resolve_diagnostic_request(route, '/v1/chat/completions', json.dumps({'model': marker}).encode())

    def test_automatic_ticket_is_removed_in_both_request_and_response_headers(self):
        private = {'X-Relay-Automatic-Probe': 'opaque-synthetic-ticket',
                   'X-CPA2SUB2API-Auto-Output-Cap': 'caller-controlled', 'Content-Type': 'application/json'}
        for request in (False, True):
            with self.subTest(request=request):
                cleaned = server._headers(private, request=request, consumer_token='synthetic')
                self.assertNotIn('x-relay-automatic-probe', {key.lower() for key, _ in cleaned})
                self.assertNotIn('x-cpa2sub2api-auto-output-cap', {key.lower() for key, _ in cleaned})
                self.assertIn(('Content-Type', 'application/json'), cleaned)

    def test_native_claude_and_openai_automatic_requests_are_low_cost(self):
        for section, model, path, field in (
            ('claude-api-key', 'claude-test', '/v1/messages?beta=true', 'max_tokens'),
            ('openai-compatibility', 'gpt-test', '/v1/chat/completions', 'max_tokens'),
            ('codex-api-key', 'gpt-test', '/v1/responses', 'max_output_tokens')):
            with self.subTest(section=section):
                route = self.route(section, model)
                route['prefix'] = route['diagnostic_prefix']
                prompt = [{'role': 'user', 'content': 'Reply OK'}]
                body = json.dumps({'model': route['prefix'] + '/' + model, field: 1024, 'messages': prompt, 'n': 4}).encode()
                actual, payload = server.prepare_automatic_request(route, path, body)
                value = json.loads(payload)
                self.assertEqual(actual, model)
                self.assertEqual(value[field], 16)
                self.assertEqual(value['messages'], prompt)
                if section == 'openai-compatibility':
                    self.assertEqual(value['n'], 1)

    def test_gemini_automatic_budget_and_candidates_are_bounded(self):
        route = self.route('gemini-api-key', 'gemini-test')
        route['prefix'] = route['diagnostic_prefix']
        original = {'contents': [{'parts': [{'text': 'Reply OK'}]}],
                    'generationConfig': {'maxOutputTokens': 512, 'candidateCount': 8, 'temperature': 0}}
        actual, payload = server.prepare_automatic_request(
            route, '/v1beta/models/' + route['prefix'] + '/gemini-test:streamGenerateContent?alt=sse', json.dumps(original).encode())
        value = json.loads(payload)
        self.assertEqual(actual, 'gemini-test')
        self.assertEqual(value['generationConfig'], {'maxOutputTokens': 16, 'candidateCount': 1, 'temperature': 0})
        self.assertEqual(value['contents'], original['contents'])

    def test_unknown_models_non_inference_paths_and_invalid_budgets_do_not_admit(self):
        route = self.route()
        route['prefix'] = route['diagnostic_prefix']
        bodies = [{'model': route['prefix'] + '/foreign-model'},
                  {'model': route['prefix'] + '/gpt-test', 'max_tokens': True},
                  {'model': route['prefix'] + '/gpt-test', 'max_tokens': -1}]
        for value in bodies:
            with self.subTest(value=value), self.assertRaises(RelayError):
                server.prepare_automatic_request(route, '/v1/chat/completions', json.dumps(value).encode())
        with self.assertRaises(RelayError):
            server.prepare_automatic_request(route, '/v1/responses/compact', json.dumps({'model': route['prefix'] + '/gpt-test'}).encode())

    def test_only_known_consumer_skip_codes_can_be_unwrapped(self):
        known = 'automatic_probe_unsafe_profile'
        plain = {'error': {'code': known, 'message': 'private source details'}}
        wrapped = {'error': {'message': json.dumps(plain)}}
        self.assertEqual(server.automatic_skip_code(json.dumps(plain).encode()), known)
        self.assertEqual(server.automatic_skip_code(json.dumps(wrapped).encode()), known)
        for value in ({'error': {'code': 'source_bad_request', 'message': json.dumps(plain)}},
                      {'error': {'code': 'automatic_probe_unknown_remote_error'}},
                      {'message': 'automatic_probe_unsafe_profile'},
                      {'error': {'message': 'private source details'}}):
            with self.subTest(value=value):
                self.assertIsNone(server.automatic_skip_code(json.dumps(value).encode()))
        self.assertIsNone(server.automatic_skip_code(b'{' * 70000))


@unittest.skipIf(aiohttp is None, 'optional relay runtime dependencies unavailable')
class AutomaticAdmissionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = server.RelayRuntime(self.directory.name, '/unused-in-admission-test')
        rid = 'a' * 40
        self.route = {'id': rid, 'prefix': 'r' + rid, 'diagnostic_prefix': 'd' + rid,
                      'automatic_prefix': 'a' + rid, 'source_section': 'openai-compatibility',
                      'models': ['gpt-test', 'gpt-other'], 'token': 'synthetic-route-token',
                      'original_source': {'base_url': 'https://source.example/v1'}}
        self.bundle = {'generation': 'f' * 64, 'routes': {rid: self.route},
                       'consumer_port': 0, 'state': {'consumer_token': 'synthetic'}}
        self.runtime.bundle = self.bundle
        self.runtime.accepting = True
        self.runtime.child = type('Child', (), {'returncode': None})()
        self.runtime.consumer_guard_proof = {'feature_id': server.GUARD_FEATURE,
            'automatic_output_cap': 16, 'automatic_output_header': server.AUTOMATIC_OUTPUT_HEADER}
        self.runtime._http = AsyncMock(return_value=web.json_response({'ok': True}))
        self.allowed = True
        self.policy = patch.object(server, 'diagnostic_policy_allowed', side_effect=lambda route: self.allowed)
        self.current = patch.object(server, 'load_bundle', side_effect=lambda state_dir: self.bundle)
        self.policy.start()
        self.current.start()

    async def asyncTearDown(self):
        self.policy.stop()
        self.current.stop()
        self.runtime.child = None
        await self.runtime.close()
        self.directory.cleanup()

    def request(self, *, automatic=True, token='synthetic-route-token', ticket=None, model='gpt-test', generation=None, duplicate=False):
        marker = ('a' if automatic else 'd') + self.route['id'] + '-' + hashlib.sha256(model.encode()).hexdigest()
        headers = CIMultiDict({'Authorization': 'Bearer ' + token})
        if ticket is not None:
            headers.add(server.AUTOMATIC_PROBE_HEADER, ticket)
            if duplicate:
                headers.add(server.AUTOMATIC_PROBE_HEADER, ticket)
            marker = model
        if generation is not None:
            headers['X-Relay-Generation'] = generation
        body = json.dumps({'model': marker, 'messages': [{'role': 'user', 'content': 'Reply OK'}], 'max_tokens': 256}).encode()
        async def read():
            return body
        return type('Request', (), {'headers': headers, 'match_info': {'route': self.route['id']},
                    'method': 'POST', 'raw_path': '/r/' + self.route['id'] + '/v1/chat/completions',
                    'read': staticmethod(read), 'transport': None})()

    def initialize(self):
        import automatic_probe_budget as budget
        budget.initialize_budget(self.directory.name, lambda: True)
        return budget

    def claim(self, model='gpt-test', limit=1):
        budget = self.initialize()
        binding = {'source_id': self.route['id'], 'model': model,
                   'generation': self.bundle['generation'], 'limit': limit}
        return budget, budget.claim_batch(self.directory.name, self.route['original_source']['base_url'], [binding], lambda: True)

    async def test_auto_without_initialized_ledger_is_400_and_zero_forward(self):
        response = await self.runtime.forward(self.request())
        self.assertEqual(response.status, 400)
        self.assertTrue(json.loads(response.body)['error'].startswith('automatic_probe_'))
        self.runtime._http.assert_not_awaited()

    async def test_two_concurrent_native_automatic_requests_share_one_weekly_slot(self):
        self.initialize()
        responses = await asyncio.gather(self.runtime.forward(self.request()), self.runtime.forward(self.request()))
        self.assertEqual(sorted(response.status for response in responses), [200, 400])
        rejected = next(response for response in responses if response.status == 400)
        self.assertEqual(json.loads(rejected.body)['error'], 'automatic_probe_not_due')
        self.assertEqual(self.runtime._http.await_count, 1)
        args = self.runtime._http.await_args.args
        self.assertEqual(args[1]['prefix'], self.route['diagnostic_prefix'])
        self.assertEqual(json.loads(args[4])['max_tokens'], 16)
        self.assertEqual(json.loads(args[4])['n'], 1)

    async def test_manual_d_requests_do_not_claim_or_consume_automatic_week(self):
        self.initialize()
        for _ in range(2):
            response = await self.runtime.forward(self.request(automatic=False))
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(self.runtime._http.await_args.args[4])['max_tokens'], 256)
        automatic = await self.runtime.forward(self.request())
        self.assertEqual(automatic.status, 200)
        rejected = await self.runtime.forward(self.request())
        self.assertEqual(rejected.status, 400)
        self.assertEqual(self.runtime._http.await_count, 3)

    async def test_import_ticket_is_exactly_bound_and_cannot_be_replayed(self):
        _, ticket = self.claim()
        rejected = await self.runtime.forward(self.request(ticket=ticket, model='gpt-other'))
        self.assertEqual(rejected.status, 400)
        self.runtime._http.assert_not_awaited()
        accepted = await self.runtime.forward(self.request(ticket=ticket))
        self.assertEqual(accepted.status, 200)
        repeated = await self.runtime.forward(self.request(ticket=ticket))
        self.assertEqual(repeated.status, 400)
        self.assertEqual(self.runtime._http.await_count, 1)

    async def test_auth_policy_model_readiness_and_generation_fail_before_claim(self):
        budget = self.initialize()
        with patch.object(budget, 'claim_batch', side_effect=AssertionError('must not claim an unauthorized request')):
            self.assertEqual((await self.runtime.forward(self.request(token='wrong'))).status, 401)
            self.allowed = False
            self.assertEqual((await self.runtime.forward(self.request())).status, 400)
            self.allowed = True
            self.runtime.accepting = False
            self.assertEqual((await self.runtime.forward(self.request())).status, 400)
            self.runtime.accepting = True
            self.assertEqual((await self.runtime.forward(self.request(model='foreign'))).status, 400)
            self.assertEqual((await self.runtime.forward(self.request(generation='0' * 64))).status, 400)
        self.runtime._http.assert_not_awaited()

    async def test_repeated_header_and_revocation_after_consume_are_source_zero(self):
        budget, ticket = self.claim()
        self.assertEqual((await self.runtime.forward(self.request(ticket=ticket, duplicate=True))).status, 400)
        consume = budget.consume_ticket
        def revoke(*args, **kwargs):
            consume(*args, **kwargs)
            self.allowed = False
        with patch.object(budget, 'consume_ticket', side_effect=revoke):
            response = await self.runtime.forward(self.request(ticket=ticket))
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error'], 'automatic_probe_authority_revoked')
        self.runtime._http.assert_not_awaited()

    async def test_distinct_credentials_on_same_canonical_host_share_week(self):
        self.initialize()
        self.assertEqual((await self.runtime.forward(self.request())).status, 200)
        self.route = dict(self.route, id='b' * 40, prefix='r' + 'b' * 40,
                          diagnostic_prefix='d' + 'b' * 40, automatic_prefix='a' + 'b' * 40,
                          original_source={'base_url': 'HTTP://SOURCE.EXAMPLE.:9443/other/path'})
        self.bundle['routes'][self.route['id']] = self.route
        response = await self.runtime.forward(self.request())
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['error'], 'automatic_probe_not_due')
        self.assertEqual(self.runtime._http.await_count, 1)

    async def test_old_bundle_on_disk_cannot_spend_a_ticket_or_forward(self):
        _, ticket = self.claim()
        changed = dict(self.bundle, generation='0' * 64)
        with patch.object(server, 'load_bundle', return_value=changed):
            response = await self.runtime.forward(self.request(ticket=ticket))
        self.assertEqual(response.status, 400)
        self.runtime._http.assert_not_awaited()
        accepted = await self.runtime.forward(self.request(ticket=ticket))
        self.assertEqual(accepted.status, 200)
        self.assertEqual(self.runtime._http.await_count, 1)


@unittest.skipIf(aiohttp is None, 'optional relay runtime dependencies unavailable')
class AutomaticConsumerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.headers = []
        self.response_status = 200
        self.response_value = {'ok': True}
        async def reply(request):
            self.headers.append(list(request.headers.items()))
            return web.json_response(self.response_value, status=self.response_status,
                headers={server.AUTOMATIC_OUTPUT_HEADER: 'reflected', server.AUTOMATIC_PROBE_HEADER: 'reflected'})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', reply)
        self.consumer = web.AppRunner(app)
        await self.consumer.setup()
        site = web.TCPSite(self.consumer, '127.0.0.1', 0)
        await site.start()
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = server.RelayRuntime(self.directory.name, '/unused-in-boundary-test')
        self.runtime.client = aiohttp.ClientSession(trust_env=False)
        self.runtime.consumer_guard_proof = {'feature_id': server.GUARD_FEATURE, 'automatic_output_cap': 16,
            'automatic_output_header': server.AUTOMATIC_OUTPUT_HEADER}
        rid = 'a' * 40
        self.route = {'id': rid, 'prefix': 'd' + rid, 'diagnostic_prefix': 'd' + rid,
                      'models': ['gpt-test'], 'source_section': 'openai-compatibility'}
        self.bundle = {'consumer_port': site._server.sockets[0].getsockname()[1], 'state': {'consumer_token': 'synthetic'}}
        headers = CIMultiDict({server.AUTOMATIC_PROBE_HEADER: 'opaque-private-ticket'})
        headers.add(server.AUTOMATIC_OUTPUT_HEADER, '128')
        headers.add(server.AUTOMATIC_OUTPUT_HEADER, 'caller-controlled')
        self.request = type('Request', (), {'headers': headers, 'transport': None})()

    async def asyncTearDown(self):
        await self.runtime.close()
        await self.consumer.cleanup()
        self.directory.cleanup()

    async def test_only_admitted_automatic_transport_sends_exact_private_cap(self):
        response = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}', automatic=True)
        outgoing = self.headers[0]
        self.assertEqual([value for key, value in outgoing if key.lower() == server.AUTOMATIC_OUTPUT_HEADER.lower()], ['16'])
        self.assertFalse(any(key.lower() == server.AUTOMATIC_PROBE_HEADER.lower() for key, _ in outgoing))
        self.assertNotIn(server.AUTOMATIC_OUTPUT_HEADER, response.headers)
        self.assertNotIn(server.AUTOMATIC_PROBE_HEADER, response.headers)

    async def test_manual_transport_never_forwards_caller_cap_or_ticket(self):
        await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}')
        reserved = {server.AUTOMATIC_OUTPUT_HEADER.lower(), server.AUTOMATIC_PROBE_HEADER.lower()}
        self.assertFalse(any(key.lower() in reserved for key, _ in self.headers[0]))

    async def test_missing_actual_consumer_capability_rejects_before_private_hop(self):
        del self.runtime.consumer_guard_proof['automatic_output_cap']
        with self.assertRaisesRegex(RelayError, 'automatic_probe_output_cap_unavailable'):
            await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}', automatic=True)
        self.assertEqual(self.headers, [])

    async def test_known_cap_skip_is_sanitized_only_for_automatic_400(self):
        self.response_status = 400
        nested = {'error': {'code': 'automatic_probe_unsafe_profile', 'message': 'private source diagnostics'}}
        self.response_value = {'error': {'message': json.dumps(nested)}}
        response = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}', automatic=True)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body), {'error': 'automatic_probe_unsafe_profile'})
        self.assertNotIn(b'private source', response.body)
        ordinary = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}')
        self.assertEqual(json.loads(ordinary.body), self.response_value)
        self.response_value = {'error': {'code': 'source_bad_request', 'message': 'source diagnosis'}}
        unknown = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}', automatic=True)
        self.assertEqual(json.loads(unknown.body), self.response_value)
        self.response_status = 403
        self.response_value = nested
        auth_error = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}', automatic=True)
        self.assertEqual(auth_error.status, 403)
        self.assertEqual(json.loads(auth_error.body), nested)


if __name__ == '__main__':
    unittest.main()
