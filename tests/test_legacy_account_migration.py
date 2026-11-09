"""Legacy identities are resolved before creates; migrations preserve intent."""
import copy
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

import tool


class Api:
    def __init__(self, rows):
        self.rows = {r['id']: copy.deepcopy(r) for r in rows}
        self.writes, self.deleted, self.created = [], [], []
        self.factory_payloads = []
        self.plans = {}
        self.groups = {}
        self.next_id = 100
        self.fail_create = False
        self.fail_delete = False
        self.fail_delete_after_effect = False

    def list_accounts(self, page=1, size=100):
        return {'data': {'items': list(copy.deepcopy(self.rows).values()), 'total': len(self.rows)}}

    def _call(self, method, path, body=None):
        aid = int(path.rsplit('/', 1)[1])
        if aid not in self.rows:
            raise tool.ApiError(404, 'missing', '<redacted>')
        return copy.deepcopy(self.rows[aid])

    def update_account(self, aid, change):
        self.writes.append((aid, copy.deepcopy(change)))
        change = copy.deepcopy(change)
        if 'credentials' in change:
            incoming = change['credentials']
            for key in tool.SENSITIVE_CREDENTIAL_FIELDS:
                if key not in incoming and key in self.rows[aid].get('credentials', {}):
                    incoming[key] = copy.deepcopy(self.rows[aid]['credentials'][key])
        self.rows[aid].update(copy.deepcopy(change))

    def bulk_update_accounts(self, ids, change):
        for aid in ids:
            self.update_account(aid, change)

    def batch_accounts(self, payloads):
        if self.fail_create:
            raise RuntimeError('synthetic create failure')
        results = []
        for payload in payloads:
            self.factory_payloads.append(copy.deepcopy(payload))
            self.next_id += 1
            aid = self.next_id
            row = copy.deepcopy(payload)
            row.update(id=aid, status='active', schedulable=True)
            for key in ('upstream_billing_probe_enabled', 'upstream_billing_rate_sync_enabled'):
                row.get('extra', {}).pop(key, None)  # Official native create removes system billing defaults.
            if not row.get('group_ids'):
                row['group_ids'] = [999]  # Native empty-list default group binding.
            self.rows[aid] = row
            self.created.append(aid)
            results.append({'id': aid, 'name': row['name'], 'success': True})
        return {'data': {'results': results}}

    def list_test_plans(self, aid):
        return copy.deepcopy(self.plans.get(aid, []))

    def create_group(self, payload):
        gid = 500 + len(self.groups)
        self.groups[gid] = dict(copy.deepcopy(payload), id=gid)
        return {'data': copy.deepcopy(self.groups[gid])}

    def list_groups(self):
        return list(copy.deepcopy(self.groups).values())

    def delete_group(self, gid):
        self.groups.pop(gid, None)

    def delete_account(self, aid):
        if self.fail_delete and aid == 7:
            raise RuntimeError('synthetic delete failure')
        self.deleted.append(aid)
        self.rows.pop(aid, None)
        if self.fail_delete_after_effect and aid == 7:
            raise RuntimeError('synthetic lost DELETE response')


class RedactedApi(Api):
    def __init__(self, rows, deny_export=False):
        super().__init__(rows)
        self.deny_export = deny_export
        self.exports = []

    def public(self, row):
        result = copy.deepcopy(row)
        result['credentials'] = {k: v for k, v in result.get('credentials', {}).items()
                                 if k not in tool.SENSITIVE_CREDENTIAL_FIELDS}
        return result

    def list_accounts(self, page=1, size=100):
        return {'data': {'items': [self.public(r) for r in self.rows.values()], 'total': len(self.rows)}}

    def _call(self, method, path, body=None):
        if path.startswith('/api/v1/admin/accounts/data?ids='):
            if self.deny_export:
                raise tool.ApiError(403, 'step-up required', '<redacted>')
            aid = int(path.split('ids=', 1)[1].split('&', 1)[0])
            self.exports.append(aid)
            return {'data': {'accounts': [copy.deepcopy(self.rows[aid])], 'proxies': []}, 'code': 0}
        return self.public(super()._call(method, path, body))

    def reveal_account_credentials(self, account):
        return tool.Sub2Api.reveal_account_credentials(self, account)


class LegacyAccountMigrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(tool.DEFAULTS, source_scope='default', model_probe_enabled=False,
                        health_rerank_enabled=False)
        records, _ = tool.collect({'claude-api-key': [
            {'api-key': 'synthetic-visible-key', 'base-url': 'https://legacy.example/v1',
             'models': [{'name': 'claude-opus-9'}]}]})
        self.record = records[0]
        self.record['_source_scope'] = 'default'
        tool.assign_names(records)
        tool.remap_priority(records)
        self.account = tool.to_account(self.record, self.cfg, {'Claude': 1}, {}, {})
        self.account.update(id=7, status='inactive', schedulable=False,
                            extra=dict(self.account['extra'], operator_note='keep'), group_ids=[1])

    def legacy(self, sentinel=True):
        account = copy.deepcopy(self.account)
        account['name'] = 'ANT-legacy.example-12'
        account['extra'].pop('cpa2sub2api_source')
        if not sentinel:
            account['notes'] = 'historical CPA import'
        return account

    def sync(self, api, current):
        return tool.sync_existing_accounts(api, [self.record], self.cfg, {'Claude': 1}, {},
                                           {self.record['name']: current})

    def test_old_sentinel_name_resolves_and_renames_same_id(self):
        old = self.legacy()
        api = Api([old])
        found, failures = tool.resolve_source_accounts([old], [self.record])
        self.assertFalse(failures)
        self.assertEqual(found[self.record['name']]['id'], 7)
        self.assertEqual(self.sync(api, found[self.record['name']]), (1, 0, []))
        self.assertEqual(api.rows[7]['name'], self.record['name'])
        self.assertEqual(tool.account_source_scope(api.rows[7]), 'default')
        self.assertEqual((api.rows[7]['status'], api.rows[7]['schedulable']), ('inactive', False))
        self.assertEqual(api.rows[7]['extra']['operator_note'], 'keep')
        self.assertFalse(api.created)

    def test_legal_type_change_is_same_id_with_reviewed_credentials(self):
        old = copy.deepcopy(self.account)
        old['type'] = 'oauth'
        old['credentials']['access_token'] = 'obsolete-synthetic-token'
        api = Api([old])
        self.assertEqual(self.sync(api, old), (1, 0, []))
        self.assertEqual(api.rows[7]['type'], 'apikey')
        self.assertEqual(api.rows[7]['credentials']['api_key'], self.account['credentials']['api_key'])
        self.assertIsNone(api.rows[7]['credentials']['access_token'])
        self.assertTrue(all(api.rows[7]['credentials'][key] is None
                            for key in tool.SENSITIVE_CREDENTIAL_FIELDS - {'api_key'}))
        self.assertFalse(api.created)

    def test_visible_wrong_key_and_untrusted_wrong_platform_are_not_owned(self):
        for mutate in ('key', 'platform', 'masked'):
            with self.subTest(mutate=mutate):
                old = self.legacy()
                if mutate == 'key':
                    old['credentials']['api_key'] = 'different-synthetic-key'
                elif mutate == 'masked':
                    old['credentials']['api_key'] = 'sk-********'
                else:
                    old['platform'] = 'openai'
                self.assertFalse(tool.account_matches_source(old, self.record))
                found, failures = tool.resolve_source_accounts([old], [self.record])
                self.assertNotIn(self.record['name'], found)

    def test_ambiguous_and_foreign_source_are_rejected(self):
        a, b = self.legacy(), self.legacy()
        b['id'] = 8
        found, failures = tool.resolve_source_accounts([a, b], [self.record])
        self.assertFalse(found)
        self.assertTrue(failures)
        foreign = copy.deepcopy(self.account)
        value = foreign['extra']['cpa2sub2api_source']
        value['scope'] = 'foreign'
        value['identity_sha256'] = tool._source_digest({k: v for k, v in value.items()
                                                       if k != 'identity_sha256'})
        found, failures = tool.resolve_source_accounts([foreign], [self.record])
        self.assertFalse(found)
        self.assertTrue(failures)

    def test_explicit_no_sentinel_claim_requires_unique_visible_binding_and_fresh_config(self):
        old = self.legacy(sentinel=False)
        api = Api([old])
        self.assertFalse(tool.account_matches_source(old, self.record))
        preview, failures = tool.claim_source_accounts(api, [7], 'default', dry_run=True,
                                                       records=[self.record])
        self.assertFalse(failures)
        self.assertFalse(api.writes)
        result, failures = tool.claim_source_accounts(api, [7], 'default', expected=preview,
                                                      records=[self.record])
        self.assertFalse(failures)
        self.assertEqual(len(result), 1)
        self.assertTrue(tool.account_matches_source(api.rows[7], self.record))
        self.assertEqual(api.rows[7]['name'], self.record['name'])
        self.assertEqual((api.rows[7]['status'], api.rows[7]['schedulable']), ('inactive', False))
        api = Api([old])
        changed = copy.deepcopy(self.record)
        changed['api_key'] = 'configuration-drift'
        result, failures = tool.claim_source_accounts(api, [7], 'default', expected=preview,
                                                      records=[changed])
        self.assertFalse(result)
        self.assertTrue(failures)
        self.assertFalse(api.writes)

    def test_platform_replacement_preserves_intent_and_exactly_deletes_old_owned_id(self):
        old = copy.deepcopy(self.account)
        old['platform'] = 'openai'
        api = Api([old, {'id': 8, 'name': 'manual', 'notes': 'leave alone'}])
        result = self.sync(api, old)
        self.assertEqual(result, (1, 0, []))
        self.assertEqual(api.deleted, [7])
        self.assertEqual(api.rows[8], {'id': 8, 'name': 'manual', 'notes': 'leave alone'})
        new = api.rows[api.created[0]]
        self.assertEqual((new['platform'], new['type'], new['name']),
                         ('anthropic', 'apikey', self.record['name']))
        self.assertEqual((new['status'], new['schedulable'], new['group_ids']), ('inactive', False, [1]))
        self.assertEqual(new['extra']['operator_note'], 'keep')
        self.assertEqual(tool.diff_account_fields(new, self.account), {})
        self.assertTrue(api.factory_payloads[0]['group_ids'])
        self.assertNotIn(1, api.factory_payloads[0]['group_ids'])
        self.assertEqual(api.factory_payloads[0]['credentials']['model_mapping'],
                         {tool.BLOCK_ALL_SENTINEL: tool.BLOCK_ALL_SENTINEL})
        self.assertFalse(api.groups)

    def test_replacement_dependency_and_failed_delete_preserve_original(self):
        for blocker in ('plan', 'create', 'delete'):
            with self.subTest(blocker=blocker):
                old = copy.deepcopy(self.account)
                old['platform'] = 'openai'
                api = Api([old])
                if blocker == 'plan':
                    api.plans[7] = [{'id': 5, 'account_id': 7, 'enabled': False}]
                api.fail_create = blocker == 'create'
                api.fail_delete = blocker == 'delete'
                result = self.sync(api, old)
                self.assertEqual(result[:2], (0, 0))
                self.assertTrue(result[2])
                self.assertEqual(api.rows, {7: old})

    def test_legal_type_preserves_existing_plan_and_rejects_shadow_dependencies(self):
        old = copy.deepcopy(self.account)
        old['type'] = 'oauth'
        api = Api([old])
        api.plans[7] = [{'id': 5, 'account_id': 7, 'enabled': False}]
        self.assertEqual(self.sync(api, old), (1, 0, []))
        self.assertEqual(api.plans[7], [{'id': 5, 'account_id': 7, 'enabled': False}])
        api = Api([old, {'id': 8, 'name': 'foreign-shadow', 'parent_account_id': 7}])
        self.assertEqual(self.sync(api, old)[:2], (0, 0))
        self.assertFalse(api.writes)

    def test_no_sentinel_claim_rejects_duplicate_candidate_and_masked_key(self):
        old = self.legacy(sentinel=False)
        other = copy.deepcopy(old)
        other.update(id=8, name='ANT-legacy.example-13')
        for rows in ([old, other], [dict(old, credentials=dict(old['credentials'], api_key='sk-****'))]):
            api = Api(rows)
            found, failures = tool.claim_source_accounts(api, [7], 'default', dry_run=True,
                                                         records=[self.record])
            self.assertFalse(found)
            self.assertTrue(failures)
            self.assertFalse(api.writes)

    def test_deleted_old_lost_response_is_verified_without_deleting_replacement(self):
        old = copy.deepcopy(self.account)
        old['platform'] = 'openai'
        api = Api([old])
        api.fail_delete_after_effect = True
        self.assertEqual(self.sync(api, old), (1, 0, []))
        self.assertEqual(api.deleted, [7])
        self.assertEqual(set(api.rows), set(api.created))
        self.assertEqual(api.writes[-1][1], {'status': 'inactive', 'schedulable': False})

    def test_import_resolves_old_name_before_create_and_is_idempotent(self):
        old = self.legacy()
        api = Api([old])
        plan = {'groups': {'Claude': {}}, 'proxies': [],
                'accounts_raw': [self.record], 'config': self.cfg}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(tool, 'load_settings', return_value=self.cfg), \
             patch.object(tool, 'OUT_DIR', directory), \
             patch.object(tool, 'ensure_groups', return_value=({'Claude': 1}, [])), \
             patch.object(tool, 'ensure_proxies', return_value={}), \
             patch.object(tool, 'plan_fallback_chain', return_value={}), \
             patch.object(tool, 'ensure_test_plans', return_value=(0, [])), \
             patch.object(tool, 'probe_import_recovery', return_value=({}, [])), \
             patch.object(tool, 'reclaim_schedulable', return_value=(0, 0, None)), \
             patch.object(tool, 'revive_proven_inactive', return_value=(0, 0, None)), \
             contextlib.redirect_stdout(io.StringIO()):
            first = tool.do_import(api, copy.deepcopy(plan))
            writes = copy.deepcopy(api.writes)
            second = tool.do_import(api, copy.deepcopy(plan))
        self.assertEqual(first['synced_existing'], 1)
        self.assertEqual(second['sync_unchanged'], 1)
        self.assertEqual(first['success'], [])
        self.assertEqual(api.writes, writes)
        self.assertFalse(api.created)
        self.assertFalse(first['prune_failed'])

    def test_official_exact_export_binds_redacted_legacy_name_and_type_migration(self):
        old = self.legacy()
        old['type'] = 'oauth'
        old['credentials']['access_token'] = 'obsolete-synthetic'
        api = RedactedApi([old])
        current = api._call('GET', '/api/v1/admin/accounts/7')
        self.assertNotIn('api_key', current['credentials'])
        self.assertEqual(self.sync(api, current), (1, 0, []))
        self.assertEqual(api.rows[7]['type'], 'apikey')
        self.assertEqual(api.rows[7]['name'], self.record['name'])
        self.assertIsNone(api.rows[7]['credentials']['access_token'])
        self.assertTrue(api.exports)
        self.assertEqual(set(api.exports), {7})

    def test_weak_legacy_without_export_renames_but_cannot_change_type_or_be_pruned(self):
        old = self.legacy()
        api = RedactedApi([old], deny_export=True)
        current = api._call('GET', '/api/v1/admin/accounts/7')
        self.assertEqual(self.sync(api, current), (1, 0, []))
        metadata = api.rows[7]['extra']['cpa2sub2api_source']
        self.assertEqual(metadata['kind'], 'legacy_claim')
        self.assertNotIn('key_sha256', metadata)
        changed = copy.deepcopy(self.record)
        changed['name'] = 'different-synthetic-source'
        removed, failures = tool.prune_stale_accounts(api, [changed])
        self.assertFalse(removed)
        self.assertFalse(api.deleted)
        old['type'] = 'oauth'
        api = RedactedApi([old], deny_export=True)
        result = self.sync(api, api._call('GET', '/api/v1/admin/accounts/7'))
        self.assertEqual(result[:2], (0, 0))
        self.assertTrue(result[2])
        self.assertFalse(api.writes)

    def test_modern_trusted_key_drift_is_corrected_same_id(self):
        old = copy.deepcopy(self.account)
        old['credentials']['api_key'] = 'wrong-synthetic-key'
        api = RedactedApi([old])
        self.assertEqual(self.sync(api, api._call('GET', '/api/v1/admin/accounts/7')), (1, 0, []))
        self.assertEqual(api.rows[7]['credentials']['api_key'], self.record['api_key'])
        self.assertFalse(api.created)

    def test_explicit_private_export_fallback_binds_one_no_sentinel_id(self):
        old = self.legacy(sentinel=False)
        api = RedactedApi([old], deny_export=True)
        evidence = {7: copy.deepcopy(old)}
        preview, failures = tool.claim_source_accounts(api, [7], 'default', dry_run=True,
                                                       records=[self.record], evidence=evidence)
        self.assertFalse(failures)
        result, failures = tool.claim_source_accounts(api, [7], 'default', expected=preview,
                                                      records=[self.record], evidence=evidence)
        self.assertFalse(failures)
        self.assertEqual(len(result), 1)
        self.assertEqual(api.rows[7]['name'], self.record['name'])

    def test_candidate_wrong_key_and_unavailable_export_preserve_old_id(self):
        class CandidateApi(RedactedApi):
            def update_account(self, aid, change):
                super().update_account(aid, change)
                if (aid != 7 and self.corrupt and 'credentials' in change
                        and tool.BLOCK_ALL_SENTINEL not in change['credentials'].get('model_mapping', {})):
                    self.rows[aid]['credentials']['api_key'] = 'wrong-candidate-synthetic-key'
            def reveal_account_credentials(self, account):
                if account['id'] != 7 and not self.corrupt:
                    raise tool.ApiError(403, 'step-up required', '<redacted>')
                return super().reveal_account_credentials(account)
        for corrupt in (True, False):
            with self.subTest(corrupt=corrupt):
                old = copy.deepcopy(self.account)
                old['platform'] = 'openai'
                api = CandidateApi([old])
                api.corrupt = corrupt
                result = self.sync(api, api._call('GET', '/api/v1/admin/accounts/7'))
                self.assertEqual(result[:2], (0, 0))
                self.assertTrue(result[2])
                self.assertEqual(api.rows, {7: old})
                self.assertNotIn(7, api.deleted)
                self.assertEqual(api.deleted, api.created)
                self.assertFalse(api.groups)

    def test_destructive_dependency_snapshot_requires_total_and_valid_shadow_links(self):
        class BrokenSnapshotApi(Api):
            def list_accounts(self, page=1, size=100):
                value = super().list_accounts(page, size)
                if self.break_total:
                    value['data'].pop('total')
                return value
        for kind in ('missing-total', 'spark-without-parent', 'invalid-parent'):
            with self.subTest(kind=kind):
                old = copy.deepcopy(self.account)
                old['platform'] = 'openai'
                extra = []
                if kind == 'spark-without-parent':
                    extra = [{'id': 8, 'name': 'unknown-shadow', 'quota_dimension': 'spark'}]
                elif kind == 'invalid-parent':
                    extra = [{'id': 8, 'name': 'unknown-shadow', 'parent_account_id': 'not-an-id'}]
                api = BrokenSnapshotApi([old] + extra)
                api.break_total = kind == 'missing-total'
                result = self.sync(api, old)
                self.assertEqual(result[:2], (0, 0))
                self.assertTrue(result[2])
                self.assertEqual(api.rows[7], old)
                self.assertFalse(api.created)
                self.assertFalse(api.deleted)
                self.assertFalse(api.writes)

    def test_official_global_dimension_and_ordinary_countless_read_remain_legal(self):
        old = copy.deepcopy(self.account)
        old['quota_dimension'] = 'global'
        api = Api([old])
        tool._require_no_shadow_dependencies(api, old)
        class CountlessApi(Api):
            def list_accounts(self, page=1, size=100):
                value = super().list_accounts(page, size)
                value['data'].pop('total')
                return value
        rows, error = tool._fetch_all_accounts(CountlessApi([old]))
        self.assertIsNone(error)
        self.assertEqual([r['id'] for r in rows], [7])

    def test_destructive_pagination_rejects_changed_total(self):
        class PagedApi:
            def list_accounts(self, page=1, size=100):
                return {'data': {'items': [{'id': page, 'name': 'synthetic'}], 'total': 2 if page == 1 else 3}}
        with self.assertRaises(ValueError):
            tool._require_no_shadow_dependencies(PagedApi(), {'id': 1})


if __name__ == '__main__':
    unittest.main()
