"""Diagnostic redirect and executable provenance must fail closed."""
import hashlib
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from unittest.mock import AsyncMock

from relay import server
from relay.config import RelayError

try:
    import aiohttp
    from aiohttp import web
    from multidict import CIMultiDict
except ImportError:
    aiohttp = None


class DiagnosticGuardContractTests(unittest.TestCase):
    def route(self, letter, source='https://site.example'):
        route_id = letter * 40
        return route_id, {'id': route_id, 'prefix': 'r' + route_id,
                          'diagnostic_prefix': 'd' + route_id,
                          'original_source': {'base_url': source}, 'blocked': False}

    def test_prefixes_are_derived_from_route_ids_and_skip_permanent_deny(self):
        first, allowed = self.route('a')
        second, denied = self.route('b', 'https://denied.example')
        with patch('probe_policy.permanently_denied', side_effect=lambda source: source == 'https://denied.example'), \
             patch.dict(os.environ, {server.DIAGNOSTIC_PREFIX_ENV: '["caller-controlled"]'}):
            self.assertEqual(server.diagnostic_prefixes({'routes': {first: allowed, second: denied}}), ['d' + first])

    def test_mismatched_diagnostic_prefix_is_rejected(self):
        route_id, route = self.route('a')
        route['diagnostic_prefix'] = 'd' + 'b' * 40
        with self.assertRaisesRegex(RelayError, 'invalid_diagnostic'):
            server.diagnostic_prefixes({'routes': {route_id: route}})

    def test_business_or_malformed_prefix_is_never_exported(self):
        for prefix in ('r' + 'a' * 40, 'd-anything', 'd' + 'A' * 40):
            route_id, route = self.route('a')
            route['diagnostic_prefix'] = prefix
            with self.subTest(prefix=prefix), self.assertRaises(RelayError):
                server.diagnostic_prefixes({'routes': {route_id: route}})

    def provenance(self, directory):
        binary = Path(directory) / 'synthetic-consumer'
        binary.write_bytes(b'synthetic executable bytes')
        metadata = {'schema_version': 1, 'feature_id': server.GUARD_FEATURE,
                    'upstream_commit': server.GUARD_SOURCE_COMMIT,
                    'automatic_output_cap': 16,
                    'automatic_output_header': 'X-CPA2SUB2API-Auto-Output-Cap',
                    'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
        path = Path(directory) / 'provenance.json'
        path.write_text(json.dumps(metadata))
        return binary, path, metadata

    def reply(self):
        return json.dumps({'feature_id': server.GUARD_FEATURE,
                           'source_commit': server.GUARD_SOURCE_COMMIT,
                           'automatic_output_cap': 16,
                           'automatic_output_header': 'X-CPA2SUB2API-Auto-Output-Cap',
                           'prefix_env': server.DIAGNOSTIC_PREFIX_ENV})

    def test_missing_wrong_or_caller_controlled_output_cap_provenance_is_rejected(self):
        for field, value in (('automatic_output_cap', None), ('automatic_output_cap', 128), ('automatic_output_cap', 16.0),
                             ('automatic_output_header', 'caller-controlled')):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                binary, path, metadata = self.provenance(directory)
                if value is None:
                    del metadata[field]
                else:
                    metadata[field] = value
                path.write_text(json.dumps(metadata))
                with patch.object(server.subprocess, 'run', side_effect=AssertionError('must reject unsupported cap before query')):
                    with self.assertRaisesRegex(RelayError, 'provenance_invalid'):
                        server.verify_consumer_guard(binary, path)

    def test_digest_and_executable_reply_are_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, path, metadata = self.provenance(directory)
            with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, self.reply(), '')):
                self.assertEqual(server.verify_consumer_guard(binary, path)['binary_sha256'], metadata['binary_sha256'])

    def test_wrong_binary_digest_is_rejected_before_query(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, path, _ = self.provenance(directory)
            binary.write_bytes(b'old unpatched executable')
            with patch.object(server.subprocess, 'run', side_effect=AssertionError('must not execute wrong binary')):
                with self.assertRaisesRegex(RelayError, 'binary_digest_mismatch'):
                    server.verify_consumer_guard(binary, path)

    def test_legacy_help_or_wrong_feature_cannot_claim_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, path, _ = self.provenance(directory)
            for text in ('legacy help text', '{}', self.reply().replace(server.GUARD_SOURCE_COMMIT, 'b' * 40)):
                with self.subTest(text=text), patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')):
                    with self.assertRaises(RelayError):
                        server.verify_consumer_guard(binary, path)

    def test_feature_query_does_not_inherit_caller_prefix_list(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, path, _ = self.provenance(directory)
            with patch.dict(os.environ, {server.DIAGNOSTIC_PREFIX_ENV: '["caller-controlled"]', server.GUARD_QUERY_ENV: 'evil'}), \
                 patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, self.reply(), '')) as run:
                server.verify_consumer_guard(binary, path)
            self.assertEqual(run.call_args.kwargs['env'][server.GUARD_QUERY_ENV], '1')
            self.assertNotIn(server.DIAGNOSTIC_PREFIX_ENV, run.call_args.kwargs['env'])

    def test_old_ready_reply_without_guard_cannot_authorize_import(self):
        bundle = {'generation': 'a' * 64, 'routes': {'route': {}},
                  'state': {'control_token': 'synthetic'}}
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'ready': True, 'generation': bundle['generation'], 'route_count': 1}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(server.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RelayError, 'not_ready'):
                server.verify_runtime('http://127.0.0.1:8380', bundle)

    def test_model_marker_restores_only_authenticated_configured_model(self):
        rid, route = self.route('a')
        route.update(source_section='openai-compatibility', models=['gpt-test'])
        marker = route['diagnostic_prefix'] + '-' + hashlib.sha256(b'gpt-test').hexdigest()
        body = json.dumps({'model': marker, 'messages': [{'content': marker}]}).encode()
        resolved, path, data, marked = server.resolve_diagnostic_request(route, '/v1/chat/completions', body)
        self.assertTrue(marked)
        self.assertEqual(resolved['prefix'], route['diagnostic_prefix'])
        self.assertEqual(json.loads(data)['model'], 'gpt-test')
        self.assertEqual(json.loads(data)['messages'][0]['content'], marker)
        self.assertEqual(path, '/v1/chat/completions')

    def test_gemini_marker_is_path_safe_and_restores_model_path(self):
        _, route = self.route('a')
        route.update(source_section='gemini-api-key', models=['gemini-test'])
        marker = route['diagnostic_prefix'] + '-' + hashlib.sha256(b'gemini-test').hexdigest()
        self.assertEqual(len(marker), 106)
        path = '/v1beta/models/' + marker + ':generateContent'
        _, actual, _, marked = server.resolve_diagnostic_request(route, path, b'{}')
        self.assertTrue(marked)
        self.assertEqual(actual, '/v1beta/models/gemini-test:generateContent')

    def test_foreign_unknown_and_legacy_slash_markers_are_rejected(self):
        _, route = self.route('a')
        route.update(source_section='openai-compatibility', models=['gpt-test'])
        for marker in ('d' + 'b' * 40 + '-' + hashlib.sha256(b'gpt-test').hexdigest(),
                       route['diagnostic_prefix'] + '-' + '0' * 64,
                       route['diagnostic_prefix'] + '/gpt-test'):
            with self.subTest(marker=marker), self.assertRaises(RelayError):
                server.resolve_diagnostic_request(route, '/v1/chat/completions', json.dumps({'model': marker}).encode())


@unittest.skipIf(aiohttp is None, 'optional relay runtime dependencies unavailable')
class DiagnosticRedirectRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.status = 307
        self.content_type = 'text/event-stream'
        self.hits = 0
        self.secret_location = 'https://private.invalid/path?key=synthetic-private-value'
        async def reply(request):
            self.hits += 1
            return web.Response(status=self.status, headers={'Location': self.secret_location,
                                'Content-Type': self.content_type}, body=b'synthetic-private-body')
        app = web.Application()
        app.router.add_post('/v1/chat/completions', reply)
        self.consumer = web.AppRunner(app)
        await self.consumer.setup()
        site = web.TCPSite(self.consumer, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = server.RelayRuntime(self.temporary.name, '/unused-in-http-test')
        self.runtime.client = aiohttp.ClientSession(trust_env=False)
        self.request = type('Request', (), {'headers': CIMultiDict(), 'transport': None})()
        rid = 'a' * 40
        self.route = {'id': rid, 'prefix': 'd' + rid, 'diagnostic_prefix': 'd' + rid,
                      'models': ['gpt-test'], 'source_section': 'openai-compatibility'}
        self.bundle = {'consumer_port': self.port, 'state': {'consumer_token': 'synthetic'}}

    def forward_request(self, model, token='synthetic-route-token'):
        body = json.dumps({'model': model}).encode()
        async def read():
            return body
        return type('Request', (), {'headers': CIMultiDict({'Authorization': 'Bearer ' + token}),
                    'match_info': {'route': self.route['id']},
                    'raw_path': '/r/' + self.route['id'] + '/v1/chat/completions',
                    'method': 'POST', 'content_type': 'application/json', 'content_length': len(body),
                    'read': staticmethod(read), 'transport': None})()

    def activate_forward_fixture(self):
        self.route['prefix'] = 'r' + self.route['id']
        self.route['token'] = 'synthetic-route-token'
        self.route['original_source'] = {'base_url': 'https://site.example'}
        self.bundle.update(routes={self.route['id']: self.route}, generation='a' * 64)
        self.runtime.bundle = self.bundle
        self.runtime.accepting = True
        self.runtime.child = type('Child', (), {'returncode': 0})()
        self.runtime.child.returncode = None
        self.runtime.consumer_guard_proof = {'feature_id': server.GUARD_FEATURE}
        return self.route['diagnostic_prefix'] + '-' + hashlib.sha256(b'gpt-test').hexdigest()

    async def asyncTearDown(self):
        # This fixture never starts an OS child; avoid lifecycle cleanup of it.
        self.runtime.child = None
        await self.runtime.close()
        await self.consumer.cleanup()
        self.temporary.cleanup()

    async def test_all_redirect_statuses_become_sanitized_502_before_stream_prepare(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.status = status
                response = await self.runtime._http(self.request, self.route, self.bundle, '/v1/chat/completions', b'{}')
                self.assertEqual(response.status, 502)
                self.assertNotIn('Location', response.headers)
                self.assertNotIn(b'synthetic-private', response.body)
                self.assertEqual(json.loads(response.body), {'error': 'upstream_redirect_blocked'})
        self.assertEqual(self.hits, 5)

    async def test_ordinary_business_redirect_response_is_unchanged(self):
        self.route['prefix'] = 'r' + self.route['id']
        self.status = 307
        # A non-streaming ordinary response does not need a downstream prepare.
        self.request.prepare = None
        async def simple(request):
            return web.Response(status=307, headers={'Location': self.secret_location}, body=b'ordinary-body')
        ordinary = web.Application()
        ordinary.router.add_post('/v1/chat/completions', simple)
        runner = web.AppRunner(ordinary)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        bundle = dict(self.bundle, consumer_port=site._server.sockets[0].getsockname()[1])
        try:
            response = await self.runtime._http(self.request, self.route, bundle, '/v1/chat/completions', b'{}')
            self.assertEqual(response.status, 307)
            self.assertEqual(response.headers['Location'], self.secret_location)
            self.assertEqual(response.body, b'ordinary-body')
        finally:
            await runner.cleanup()

    async def test_local_consumer_websocket_redirect_never_reaches_target(self):
        target_hits = 0
        async def target(request):
            nonlocal target_hits
            target_hits += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close()
            return ws
        target_app = web.Application()
        target_app.router.add_get('/redirected', target)
        target_runner = web.AppRunner(target_app)
        await target_runner.setup()
        target_site = web.TCPSite(target_runner, '127.0.0.1', 0)
        await target_site.start()
        target_port = target_site._server.sockets[0].getsockname()[1]
        async def redirect(request):
            return web.Response(status=307, headers={'Location': 'http://127.0.0.1:%d/redirected' % target_port})
        app = web.Application()
        app.router.add_get('/v1/responses', redirect)
        redirect_runner = web.AppRunner(app)
        await redirect_runner.setup()
        redirect_site = web.TCPSite(redirect_runner, '127.0.0.1', 0)
        await redirect_site.start()
        port = redirect_site._server.sockets[0].getsockname()[1]
        await self.runtime.client.close()
        await self.runtime.start()
        try:
            with self.assertRaisesRegex(RelayError, 'consumer_redirect_blocked'):
                await self.runtime._websocket(self.request, self.route, dict(self.bundle, consumer_port=port))
            self.assertEqual(target_hits, 0)
        finally:
            await redirect_runner.cleanup()
            await target_runner.cleanup()

    async def test_local_policy_and_unready_rejections_are_400_and_zero_consumer(self):
        marker = self.activate_forward_fixture()
        with patch.object(server, 'diagnostic_policy_allowed', return_value=False):
            response = await self.runtime.forward(self.forward_request(marker))
            self.assertEqual(response.status, 400)
        with patch.object(server, 'diagnostic_policy_allowed', return_value=True):
            self.runtime.accepting = False
            response = await self.runtime.forward(self.forward_request(marker))
            self.assertEqual(response.status, 400)
            self.runtime.accepting = True
            self.runtime.consumer_guard_proof = None
            response = await self.runtime.forward(self.forward_request(marker))
            self.assertEqual(response.status, 400)
        self.assertEqual(self.hits, 0)

    async def test_invalid_marker_and_bad_diagnostic_token_are_local_400(self):
        marker = self.activate_forward_fixture()
        response = await self.runtime.forward(self.forward_request('d' + 'b' * 40 + '-' + '0' * 64))
        self.assertEqual(response.status, 400)
        response = await self.runtime.forward(self.forward_request(marker, token='wrong'))
        self.assertEqual(response.status, 400)
        response = await self.runtime.forward(self.forward_request('gpt-test', token='wrong'))
        self.assertEqual(response.status, 401)
        self.assertEqual(self.hits, 0)

    async def test_real_consumer_credential_errors_are_not_disguised(self):
        marker = self.activate_forward_fixture()
        self.content_type = 'application/json'
        with patch.object(server, 'diagnostic_policy_allowed', return_value=True):
            for status in (401, 403):
                self.status = status
                response = await self.runtime.forward(self.forward_request(marker))
                self.assertEqual(response.status, status)
        self.assertEqual(self.hits, 2)


@unittest.skipIf(aiohttp is None, 'optional relay runtime dependencies unavailable')
class DiagnosticAuthenticationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = server.RelayRuntime(self.temporary.name, '/unused-auth-boundary-test')
        self.rid = 'a' * 40
        self.route = {'id': self.rid, 'prefix': 'r' + self.rid, 'diagnostic_prefix': 'd' + self.rid,
                      'token': 'valid-local-route-token', 'source_section': 'openai-compatibility',
                      'models': ['gpt-test'], 'original_source': {'base_url': 'https://site.example'}}
        self.runtime.bundle = {'routes': {self.rid: self.route}, 'generation': 'b' * 64}
        self.runner = web.AppRunner(server.create_app(self.runtime), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.client = aiohttp.ClientSession(trust_env=False)

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        await self.runtime.close()
        self.temporary.cleanup()

    def marker(self, automatic=False, legacy=False):
        digest = hashlib.sha256(b'gpt-test').hexdigest()
        return ('a' if automatic else 'd') + self.rid + '-' + (digest if legacy else digest[:58])

    async def post(self, path, model=None, extra=None, body=None, content_type='application/json'):
        headers = {'Authorization': 'Bearer wrong-local-route-token', 'Content-Type': content_type}
        headers.update(extra or {})
        if body is None:
            body = json.dumps({'model': model}).encode()
        async with self.client.post(self.base + path, data=body, headers=headers) as response:
            return response.status, await response.json()

    async def test_known_unknown_bad_token_diagnostic_results_do_not_enumerate_routes(self):
        original = self.runtime.bundle
        for bundle in (original, None):
            self.runtime.bundle = bundle
            for automatic in (False, True):
                for legacy in (False, True):
                    marker = self.marker(automatic, legacy)
                    replies = []
                    for rid in (self.rid, '0' * 40, 'not-a-route'):
                        replies.append(await self.post('/r/' + rid + '/v1/chat/completions', marker))
                    self.assertTrue(all(status == 400 for status, _ in replies))
                    self.assertEqual(replies[0][1], replies[1][1])
                    self.assertEqual(replies[1][1], replies[2][1])
        self.runtime.bundle = original
        self.assertEqual(self.runtime.active, 0)
        self.assertIsNone(self.runtime.child)

    async def test_reserved_malformed_and_unknown_model_markers_remain_local_failures(self):
        for marker in ('d' + self.rid + '/gpt-test', self.marker() + '0', 'a' + 'c' * 40 + '-' + '0' * 58):
            for rid in (self.rid, '0' * 40):
                status, _ = await self.post('/r/' + rid + '/v1/chat/completions', marker)
                self.assertEqual(status, 400)

    async def test_gemini_uri_marker_is_detected_without_a_body_model(self):
        for rid in (self.rid, '0' * 40, 'not-a-route'):
            path = '/r/' + rid + '/v1beta/models/' + self.marker() + ':generateContent'
            status, _ = await self.post(path, body=b'{}')
            self.assertEqual(status, 400)

    async def test_exact_and_automatic_selectors_are_local_even_with_unreadable_body(self):
        for headers in ({'X-Relay-Exact-Key': 'true'}, {'X-Relay-Exact-Key': ''}, {'X-Relay-Automatic-Probe': 'synthetic-ticket'}):
            for rid in (self.rid, '0' * 40, 'not-a-route'):
                status, _ = await self.post('/r/' + rid + '/v1/chat/completions', extra=headers, body=b'not-json', content_type='application/octet-stream')
                self.assertEqual(status, 400)

    async def test_ordinary_unknown_and_bad_authentication_still_return_identical_401(self):
        replies = [await self.post('/r/' + rid + '/v1/chat/completions', 'gpt-test')
                   for rid in (self.rid, '0' * 40, 'not-a-route')]
        self.assertEqual(replies, [(401, {'error': 'unauthorized'})] * 3)
        status, _ = await self.post('/r/' + self.rid + '/v1/chat/completions', 'gpt-test',
                                  extra={'X-Relay-Generation': 'arbitrary-generation'})
        self.assertEqual(status, 401)

    async def test_invalid_json_nonmodel_and_message_text_do_not_fake_diagnostics(self):
        for raw in (b'{broken', b'[]', b'{"model":123}', json.dumps({'model': 'gpt-test', 'messages': [{'content': self.marker()}]}).encode()):
            status, _ = await self.post('/r/' + '0' * 40 + '/v1/chat/completions', body=raw)
            self.assertEqual(status, 401)

    async def test_unauthorized_body_peek_is_bounded_by_length_type_and_encoding(self):
        for length, kind, encoding in ((65537, 'application/json', 'identity'), (None, 'application/json', 'identity'),
                                       (200, 'text/plain', 'identity'), (200, 'application/json', 'gzip')):
            reader = AsyncMock(side_effect=AssertionError('unsafe unauthenticated body read'))
            request = type('Request', (), {'headers': CIMultiDict({'Authorization': 'Bearer invalid', 'Content-Encoding': encoding}),
                'match_info': {'route': '0' * 40}, 'raw_path': '/r/' + '0' * 40 + '/v1/chat/completions',
                'method': 'POST', 'content_type': kind, 'content_length': length, 'read': reader})()
            response = await self.runtime.forward(request)
            self.assertEqual(response.status, 401)
            reader.assert_not_awaited()

    async def test_slow_or_recursive_unauthorized_json_stays_local_without_admission(self):
        for exception in (asyncio.TimeoutError(), RecursionError(), ConnectionResetError(),
                          asyncio.IncompleteReadError(b'', 200),
                          web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=65537)):
            reader = AsyncMock(side_effect=exception)
            request = type('Request', (), {'headers': CIMultiDict({'Authorization': 'Bearer invalid'}),
                'match_info': {'route': '0' * 40}, 'raw_path': '/r/' + '0' * 40 + '/v1/chat/completions',
                'method': 'POST', 'content_type': 'application/json', 'content_length': 200, 'read': reader})()
            response = await self.runtime.forward(request)
            self.assertEqual(response.status, 401)
            self.assertEqual(self.runtime.active, 0)


if __name__ == '__main__':
    unittest.main()
