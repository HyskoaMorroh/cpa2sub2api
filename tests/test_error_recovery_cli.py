"""Explicit error recovery consumes exact fresh proof without clearing real cooldowns."""
import contextlib
import copy
import io
import json
import pathlib
import unittest
from unittest.mock import patch

try:
    from tests import test_relay_probe_cli as relay_tests
except ImportError:
    import test_relay_probe_cli as relay_tests
import tool
import request_relay


class ErrorAdmin:
    def __init__(self, account):
        self.row = copy.deepcopy(account)
        self.row.update(status='error', schedulable=False, error_message='synthetic transient channel unavailable')
        self.base, self.key, self.user_agent, self.timeout = 'http://admin.invalid', 'synthetic', 'fixture', 1
        self.writes, self.on_clear, self.fail_bulk = [], None, False

    def _call(self, method, path):
        if method == 'GET':
            return copy.deepcopy(self.row)
        assert (method, path) == ('POST', '/api/v1/admin/accounts/7/clear-error')
        self.writes.append('clear-error')
        self.row.update(status='active', error_message='')
        if self.on_clear:
            self.on_clear()
        return copy.deepcopy(self.row)

    def bulk_update_accounts(self, ids, payload):
        assert ids == [7]
        self.writes.append('bulk')
        if self.fail_bulk:
            raise RuntimeError('synthetic failure')
        self.row.update(payload)


class ErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        relay_tests.RelayProbeCliTests.setUp(self)
        self.source_path = pathlib.Path(self.directory.name) / 'current-config.yaml'
        raw = {'openai-compatibility': [{'name': 'fixture', 'base-url': 'https://source.example/v1',
               'rebuild-mid-system-message': True, 'api-key-entries': [{'api-key': 'synthetic-source-key'}],
               'models': [{'name': 'gpt-6'}]}]}
        self.source_path.write_text(tool._yaml().safe_dump(raw), encoding='utf-8')
        source_patch = patch.object(tool, 'CONFIG_YAML', str(self.source_path))
        source_patch.start()
        self.addCleanup(source_patch.stop)
    def invoke(self, api, success=True):
        with patch.object(tool, 'load_settings', return_value=self.settings), \
             patch.object(tool, 'Sub2Api', return_value=api), \
             patch.object(tool, 'BULK_HAS_SCHEDULABLE', True), \
             patch.object(request_relay, 'verify_runtime'), \
             patch.object(tool, '_http', return_value=(200, '{"choices":[{"message":{"content":"5"}}]}') if success else (503, '{}')) as http, \
             contextlib.redirect_stdout(io.StringIO()):
            result = tool.probe_cli(['recover', '7'])
        return result, http.call_count

    def test_error503_does_not_clear_or_restore_but200_clears_and_restores(self):
        failed = ErrorAdmin(self.api.row)
        self.assertEqual(self.invoke(failed, False), (1, 1))
        self.assertEqual(failed.writes, [])
        self.assertEqual(failed.row['status'], 'error')
        successful = ErrorAdmin(self.api.row)
        self.assertEqual(self.invoke(successful), (0, 1))
        self.assertEqual(successful.writes, ['clear-error', 'bulk'])
        self.assertEqual((successful.row['status'], successful.row['schedulable'], successful.row['error_message']),
                         ('active', True, ''))

    def test_error_recovery_stops_after_clear_if_authority_or_account_changes(self):
        for change in ('authority', 'manual', 'newerror', 'cooldown'):
            with self.subTest(change=change):
                api = ErrorAdmin(self.api.row)
                original = copy.deepcopy(self.settings)
                def altered():
                    if change == 'authority':
                        self.settings['request_relay_enabled'] = False
                    elif change == 'manual':
                        api.row['status'] = 'inactive'
                    elif change == 'newerror':
                        api.row.update(status='error', error_message='new synthetic error')
                    else:
                        api.row['temp_unschedulable_until'] = '2999-01-01T00:00:00Z'
                api.on_clear = altered
                result, count = self.invoke(api)
                self.assertEqual(result, 1)
                self.assertEqual(count, 1)
                self.assertEqual(api.writes, ['clear-error'])
                self.assertFalse(api.row['schedulable'])
                self.settings.clear()
                self.settings.update(original)

    def test_partial_clear_then_bulk_failure_is_not_reported_as_recovery(self):
        api = ErrorAdmin(self.api.row)
        api.fail_bulk = True
        self.assertEqual(self.invoke(api), (1, 1))
        self.assertEqual(api.row['status'], 'active')
        self.assertFalse(api.row['schedulable'])

    def test_future_unknown_and_extra_runtime_block_prevent_even_the_probe(self):
        for block in ({'temp_unschedulable_until': '2999-01-01T00:00:00Z'},
                      {'rate_limited_at': '2000-01-01T00:00:00Z'},
                      {'rate_limit_reset_at': 'invalid'}, {'overload_until': '2999-01-01T00:00:00Z'},
                      {'extra': dict(self.api.row.get('extra') or {}, model_rate_limits={'synthetic': {}})}):
            with self.subTest(block=block):
                api = ErrorAdmin(self.api.row)
                api.row.update(block)
                self.assertEqual(self.invoke(api), (1, 0))
                self.assertEqual(api.writes, [])

    def test_runtime_deadlines_expire_but_unparseable_or_unbounded_history_is_blocked(self):
        now = 2000000000
        self.assertFalse(tool._recovery_runtime_block({'temp_unschedulable_until': '2000-01-01T00:00:00Z'}, now=now))
        self.assertFalse(tool._recovery_runtime_block({'rate_limited_at': '2000-01-01T00:00:00Z',
                                                      'rate_limit_reset_at': '2000-01-02T00:00:00Z'}, now=now))
        for account in ({'temp_unschedulable_until': 'invalid'}, {'temp_unschedulable_until': '2999-01-01T00:00:00Z'},
                        {'rate_limited_at': '2000-01-01T00:00:00Z'}, {'temp_unschedulable_reason': 'unknown'}):
            self.assertTrue(tool._recovery_runtime_block(account, now=now))

    def test_error_is_not_probed_by_nonexplicit_jobs_and_closed_consent(self):
        api = ErrorAdmin(self.api.row)
        policy = dict(self.settings, explicit_recovery_account_ids=[7])
        with patch.object(tool, '_http', side_effect=AssertionError('implicit error probe')):
            evidence, _ = tool.probe_import_recovery(api, {'target': 7}, policy)
        self.assertEqual(evidence, {})
        for denied in ('master', 'site', 'permanent'):
            with self.subTest(denied=denied):
                original = copy.deepcopy(self.settings)
                if denied == 'master':
                    self.settings['model_probe_enabled'] = False
                elif denied == 'site':
                    self.settings['online_probe_hosts'] = {}
                with patch.object(tool.probe_policy, 'permanently_denied', return_value=denied == 'permanent'):
                    self.assertEqual(self.invoke(ErrorAdmin(self.api.row)), (1, 0))
                self.settings.clear()
                self.settings.update(original)

    def test_error_with_scheduling_already_true_is_not_cleared_implicitly(self):
        api = ErrorAdmin(self.api.row)
        api.row['schedulable'] = True
        self.assertEqual(self.invoke(api), (1, 0))
        self.assertEqual(api.writes, [])
        self.assertEqual(api.row['status'], 'error')

    def test_fresh_error_message_change_after_success_blocks_clear(self):
        api = ErrorAdmin(self.api.row)
        def success(*args, **kwargs):
            api.row['error_message'] = 'a different synthetic error occurred'
            return 200, '{"choices":[{"message":{"content":"5"}}]}'
        with patch.object(tool, 'load_settings', return_value=self.settings), \
             patch.object(tool, 'Sub2Api', return_value=api), patch.object(tool, 'BULK_HAS_SCHEDULABLE', True), \
             patch.object(request_relay, 'verify_runtime'), patch.object(tool, '_http', side_effect=success), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.probe_cli(['recover', '7']), 1)
        self.assertEqual(api.writes, [])

    def test_source_revocation_after_success_prevents_clear_error(self):
        api = ErrorAdmin(self.api.row)
        def success(*args, **kwargs):
            self.source_path.write_text('openai-compatibility: []\n', encoding='utf-8')
            return 200, '{"choices":[{"message":{"content":"5"}}]}'
        with patch.object(tool, 'load_settings', return_value=self.settings), \
             patch.object(tool, 'Sub2Api', return_value=api), patch.object(tool, 'BULK_HAS_SCHEDULABLE', True), \
             patch.object(request_relay, 'verify_runtime'), patch.object(tool, '_http', side_effect=success), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.probe_cli(['recover', '7']), 1)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.row['status'], 'error')

    def test_source_weight_zero_or_changed_models_prevents_old_model_proof(self):
        original = self.source_path.read_text(encoding='utf-8')
        for change in ('zero_weight', 'models'):
            with self.subTest(change=change):
                document = tool._yaml().safe_load(original)
                entry = document['openai-compatibility'][0]
                if change == 'zero_weight':
                    document['routing'] = {'strategy': 'weighted-round-robin'}
                    entry['api-key-entries'][0]['weight'] = 0
                else:
                    entry['models'] = [{'name': 'gpt-99'}]
                self.source_path.write_text(tool._yaml().safe_dump(document), encoding='utf-8')
                api = ErrorAdmin(self.api.row)
                self.assertEqual(self.invoke(api), (1, 0))
                self.assertEqual(api.writes, [])
        self.source_path.write_text(original, encoding='utf-8')

    def test_known_expired_runtime_metadata_can_be_cleared_but_errors_are_still_tested_once(self):
        api = ErrorAdmin(self.api.row)
        api.row.update(temp_unschedulable_until='2000-01-01T00:00:00Z', temp_unschedulable_reason='expired synthetic',
                       rate_limited_at='2000-01-01T00:00:00Z', rate_limit_reset_at='2000-01-02T00:00:00Z')
        def clear_expired():
            for field in ('temp_unschedulable_until', 'temp_unschedulable_reason', 'rate_limited_at', 'rate_limit_reset_at'):
                api.row[field] = None
        api.on_clear = clear_expired
        self.assertEqual(self.invoke(api), (0, 1))
        self.assertEqual(api.writes, ['clear-error', 'bulk'])

    def test_clear_and_bulk_429_waits_revalidate_before_any_retry(self):
        for stage in ('clear', 'bulk'):
            with self.subTest(stage=stage):
                row = ErrorAdmin(self.api.row).row
                live = copy.deepcopy(self.settings)
                api = tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url='http://admin.invalid', sub2api_admin_key='synthetic'))
                original = api._call
                sends = []
                def call(method, path, body=None, **kwargs):
                    if method == 'GET':
                        return copy.deepcopy(row)
                    return original(method, path, body, **kwargs)
                def http(method, url, headers, body, **kwargs):
                    if '/r/' in url:
                        return 200, '{"choices":[{"message":{"content":"5"}}]}'
                    sends.append('clear' if url.endswith('/clear-error') else 'bulk')
                    if sends[-1] == 'clear' and stage == 'bulk':
                        row.update(status='active', error_message='')
                        return 200, '{}'
                    error = tool.ApiError(429, 'limited', '<redacted>')
                    error.retry_after = '1'
                    raise error
                def revoke(seconds):
                    if stage == 'clear':
                        live['request_relay_enabled'] = False
                    else:
                        row['temp_unschedulable_until'] = '2999-01-01T00:00:00Z'
                with patch.object(api, '_call', side_effect=call), patch.object(tool, 'Sub2Api', return_value=api), \
                     patch.object(tool, 'load_settings', side_effect=lambda: copy.deepcopy(live)), \
                     patch.object(tool, 'BULK_HAS_SCHEDULABLE', True), patch.object(request_relay, 'verify_runtime'), \
                     patch.object(tool, '_http', side_effect=http), patch.object(tool.time, 'sleep', side_effect=revoke), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.probe_cli(['recover', '7']), 1)
                self.assertEqual(sends, ['clear'] if stage == 'clear' else ['clear', 'bulk'])
                self.assertFalse(row['schedulable'])


if __name__ == '__main__':
    unittest.main()
