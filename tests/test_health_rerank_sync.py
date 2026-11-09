"""Offline safety checks for the second-stage ranking boundary."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool


class FakeApi:
    def __init__(self, rows=None, write_error=False, read_error=False):
        self.rows = rows if rows is not None else [{"id": 7, "priority": 3, "load_factor": 8}]
        self.writes = []
        self.reads = 0
        self.write_error = write_error
        self.read_error = read_error

    def update_account(self, aid, payload):
        self.writes.append((aid, payload))
        if self.write_error:
            raise RuntimeError("secret-token-do-not-print")

    def list_accounts(self, page, size):
        self.reads += 1
        if self.read_error:
            raise RuntimeError("secret-token-do-not-print")
        return {"data": {"items": self.rows, "total": len(self.rows)}}


class RankingSyncTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"name": "synthetic", "new_priority": 3, "health_score": .95,
                         "credentials": {"api_key": "never-send"}, "proxy_id": 22,
                         "concurrency": 99, "extra": {"unexpected": True}}]
        self.have = {"synthetic": {"id": 7, "priority": 9, "load_factor": 2}}

    def run_sync(self, api, **kwargs):
        return tool.sync_health_ranking(api, self.records, self.have, **kwargs)

    def test_only_ranking_fields_and_verified_success(self):
        api = FakeApi()
        with patch.object(tool, "to_account", side_effect=AssertionError("generic conversion")), \
             patch.object(tool, "diff_account_fields", side_effect=AssertionError("generic diff")):
            self.assertEqual(self.run_sync(api), (1, 0, []))
        self.assertEqual(api.writes, [(7, {"priority": 3, "load_factor": 8})])
        self.assertEqual(api.reads, 1)

    def test_noop_and_dry_run_do_not_contact_api(self):
        api = FakeApi()
        self.assertEqual(self.run_sync(api, dry_run=True), (1, 0, []))
        self.have["synthetic"].update(priority=3, load_factor=8)
        self.assertEqual(self.run_sync(api), (0, 1, []))
        self.assertEqual((api.writes, api.reads), ([], 0))

    def test_missing_mismatched_and_wrong_id_readback_fail(self):
        for rows in ([], [{"id": 7, "priority": 3}],
                     [{"id": 7, "priority": 4, "load_factor": 8}],
                     [{"id": 8, "name": "synthetic", "priority": 3, "load_factor": 8}]):
            with self.subTest(rows=rows):
                ok, same, failures = self.run_sync(FakeApi(rows))
                self.assertEqual((ok, same, len(failures)), (0, 0, 1))

    def test_transport_errors_do_not_expose_body_or_count_success(self):
        for api in (FakeApi(write_error=True), FakeApi(read_error=True)):
            result = self.run_sync(api)
            self.assertEqual(result[0], 0)
            self.assertEqual(len(result[2]), 1)
            self.assertNotIn("secret-token", str(result))

    def test_duplicate_target_id_aborts_before_any_write(self):
        self.records.append(dict(self.records[0], name="other"))
        self.have["other"] = dict(self.have["synthetic"])
        api = FakeApi()
        self.assertEqual(self.run_sync(api)[0], 0)
        self.assertEqual(api.writes, [])

    def test_invalid_health_score_prevents_partial_priority_write(self):
        for score in (float("nan"), float("inf"), -1, 2, "bad"):
            self.records[0]["health_score"] = score
            api = FakeApi()
            self.assertEqual(self.run_sync(api)[0], 0)
            self.assertEqual(api.writes, [])

    def test_duplicate_or_incomplete_readback_fails(self):
        api = FakeApi([{"id": 7, "priority": 3, "load_factor": 8}] * 2)
        self.assertEqual(self.run_sync(api)[0], 0)
        with patch.object(tool, "_fetch_all_accounts", return_value=([], "private error body")):
            result = self.run_sync(FakeApi())
            self.assertEqual(result[0], 0)
            self.assertNotIn("private error body", str(result))

    def test_health_bands(self):
        for score, weight in ((1, 8), (.9, 8), (.7, 6), (.5, 4), (0, 2)):
            self.records[0]["health_score"] = score
            api = FakeApi([{"id": 7, "priority": 3, "load_factor": weight}])
            self.assertEqual(self.run_sync(api)[0], 1)
            self.assertTrue(set(api.writes[0][1]) <= {"priority", "load_factor"})


if __name__ == "__main__":
    unittest.main()
