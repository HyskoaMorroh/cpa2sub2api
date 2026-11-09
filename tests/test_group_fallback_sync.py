"""Fallback reconciliation writes only drift and verifies the persisted field."""
import copy
import unittest

import tool


class Api:
    def __init__(self, rows, ignore=False):
        self.rows = {row['id']: copy.deepcopy(row) for row in rows}
        self.writes = []
        self.ignore = ignore
    def get_group(self, gid):
        return {'code': 0, 'data': copy.deepcopy(self.rows[gid])}
    def update_group(self, gid, patch):
        self.writes.append((gid, copy.deepcopy(patch)))
        if not self.ignore:
            self.rows[gid].update(copy.deepcopy(patch))


class GroupFallbackSyncTests(unittest.TestCase):
    def test_same_fallback_is_a_zero_write_noop(self):
        for dry_run in (False, True):
            api = Api([{'id': 1, 'fallback_group_id': 2, 'rate_multiplier': 7}])
            self.assertEqual(tool.apply_fallback_chain(api, {'A': 'B'}, {'A': 1, 'B': 2}, dry_run=dry_run), (0, []))
            self.assertFalse(api.writes)
            self.assertEqual(api.rows[1]['rate_multiplier'], 7)

    def test_drift_updates_only_fallback_tail_before_head_and_is_idempotent(self):
        api = Api([{'id': 1, 'fallback_group_id': 9, 'rate_multiplier': 7},
                   {'id': 2, 'fallback_group_id': None}])
        chain, ids = {'A': 'B', 'B': 'C'}, {'A': 1, 'B': 2, 'C': 3}
        self.assertEqual(tool.apply_fallback_chain(api, chain, ids), (2, []))
        self.assertEqual(api.writes, [(2, {'fallback_group_id': 3}), (1, {'fallback_group_id': 2})])
        self.assertEqual(api.rows[1]['rate_multiplier'], 7)
        self.assertEqual(tool.apply_fallback_chain(api, chain, ids), (0, []))
        self.assertEqual(len(api.writes), 2)

    def test_write_not_persisted_is_not_counted_success(self):
        api = Api([{'id': 1, 'fallback_group_id': None}], ignore=True)
        ok, failures = tool.apply_fallback_chain(api, {'A': 'B'}, {'A': 1, 'B': 2})
        self.assertEqual(ok, 0)
        self.assertTrue(failures)


if __name__ == '__main__':
    unittest.main()
