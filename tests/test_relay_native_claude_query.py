"""Pinned native Claude query support preserves the narrow protocol boundary."""
import json
import unittest

from relay.config import RelayError
from relay.protocol import prepare_request


class NativeClaudeQueryTests(unittest.TestCase):
    def setUp(self):
        self.route = {'source_section': 'claude-api-key', 'prefix': 'd' + 'a' * 40,
                      'models': ['claude-fixture']}
        self.value = {'model': 'claude-fixture', 'messages': [{'role': 'user',
                      'content': 'keep ?beta=false&key=fixture'}],
                      'tools': [{'name': 'keep'}], 'max_tokens': 16}
        self.body = json.dumps(self.value).encode()

    def test_native_messages_beta_true_preserves_query_and_payload(self):
        path, body = prepare_request(self.route, '/v1/messages?beta=true', self.body)
        self.assertEqual(path, '/v1/messages?beta=true')
        expected = dict(self.value, model=self.route['prefix'] + '/claude-fixture')
        self.assertEqual(json.loads(body), expected)

    def test_beta_rejects_other_values_duplicates_keys_and_encodings(self):
        for query in ('beta=false', 'beta=1', 'beta=True', 'beta=', 'beta',
                      'beta=true&beta=true', 'beta=true&key=fixture', 'beta=true&alt=sse',
                      'beta=true&target=fixture', 'beta=true&', '%62eta=true', 'beta=%74rue'):
            with self.subTest(query=query), self.assertRaises(RelayError):
                prepare_request(self.route, '/v1/messages?' + query, self.body)

    def test_beta_rejects_other_paths_and_protocols(self):
        for path in ('/v1/messages/count_tokens', '/v1/responses', '/v1beta/interactions',
                     '/v1beta/models/claude-fixture:generateContent'):
            with self.subTest(path=path), self.assertRaises(RelayError):
                prepare_request(self.route, path + '?beta=true', self.body)
        for section in ('openai-compatibility', 'codex-api-key', 'gemini-api-key',
                        'interactions-api-key', 'xai-api-key'):
            with self.subTest(section=section), self.assertRaises(RelayError):
                prepare_request(dict(self.route, source_section=section),
                                '/v1/messages?beta=true', self.body)

    def test_beta_cannot_bypass_path_or_target_validation(self):
        for path in ('https://fixture.invalid/v1/messages?beta=true',
                     '//fixture.invalid/v1/messages?beta=true',
                     '/v1/%6dessages?beta=true', '/v1/../messages?beta=true',
                     '/v1/messages?beta=true#fragment'):
            with self.subTest(path=path), self.assertRaises(RelayError):
                prepare_request(self.route, path, self.body)

    def test_plain_messages_and_gemini_sse_keep_existing_contract(self):
        path, body = prepare_request(self.route, '/v1/messages', self.body)
        self.assertEqual(path, '/v1/messages')
        self.assertEqual(json.loads(body)['model'], self.route['prefix'] + '/claude-fixture')
        gemini = dict(self.route, source_section='gemini-api-key', models=['gemini-fixture'])
        path, body = prepare_request(gemini, '/v1beta/models/gemini-fixture:streamGenerateContent?alt=sse', b'{}')
        self.assertEqual(path, '/v1beta/models/' + gemini['prefix'] + '/gemini-fixture:streamGenerateContent?alt=sse')
        self.assertEqual(body, b'{}')
        for query in ('alt=json', 'alt=sse&alt=sse', 'alt=sse&key=fixture'):
            with self.subTest(query=query), self.assertRaises(RelayError):
                prepare_request(gemini, '/v1beta/models/gemini-fixture:streamGenerateContent?' + query, b'{}')


if __name__ == '__main__':
    unittest.main()
