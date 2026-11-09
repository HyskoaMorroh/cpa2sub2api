"""Explicit source models and priorities survive conversion without probing."""
import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tool
import request_relay
from relay import config as relay_config
from relay.config import RelayError


class SourceModelFidelityTests(unittest.TestCase):
    def records(self, models=None, excluded=None, priorities=(7,)):
        entries = []
        for index, priority in enumerate(priorities):
            entry = {"api-key": "synthetic-key-%d" % index,
                     "base-url": "https://source-%d.example/v1" % index,
                     "priority": priority,
                     "models": models if models is not None else [
                         {"name": "gpt-6.1-sol", "alias": "gpt-6.1-sol-agt", "force-mapping": True},
                         {"name": "gpt-6.0"}, {"name": "gpt-5.4-mini"}],
                     "excluded-models": excluded or []}
            entries.append(entry)
        document = {"codex-api-key": entries}
        records, _ = tool.collect(document)
        tool.assign_names(records)
        tool.remap_priority(records)
        return records, document

    def account(self, record):
        return tool.to_account(record, dict(tool.DEFAULTS), {"Codex": 1}, {}, {})

    def test_explicit_verified_models_are_not_highest_filtered(self):
        records, _ = self.records()
        mapping = self.account(records[0])["credentials"]["model_mapping"]
        self.assertEqual(mapping, {"gpt-6.1-sol-agt": "gpt-6.1-sol",
                                   "gpt-6.0": "gpt-6.0", "gpt-5.4-mini": "gpt-5.4-mini"})

    def test_original_name_is_not_an_extra_public_alias(self):
        records, _ = self.records(models=[{"name": "gpt-6.1-sol", "alias": "gpt-6.1-sol-agt"}])
        self.assertEqual(self.account(records[0])["credentials"]["model_mapping"],
                         {"gpt-6.1-sol-agt": "gpt-6.1-sol"})

    def test_denyall_desired_payload_contains_no_visible_sentinel(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        self.assertEqual(desired["credentials"].get("model_mapping"), {})
        self.assertEqual(desired["status"], "inactive")
        self.assertIs(desired["schedulable"], False)
        self.assertTrue(tool.needs_inactive(records[0], dict(tool.DEFAULTS, zero_model_policy="active")))

    def test_source_priority_ties_remain_ties_across_domains(self):
        records, _ = self.records(priorities=(90, 20, 90, 0))
        actual = [r["new_priority"] for r in records]
        self.assertEqual(actual[0], actual[2])
        self.assertLess(actual[0], actual[1])
        self.assertLess(actual[1], actual[3])

    def test_health_does_not_override_explicit_source_order(self):
        records, _ = self.records(priorities=(90, 20))
        before = [r["new_priority"] for r in records]
        current = {r["name"]: {"status": "error" if i == 0 else "active", "schedulable": i != 0}
                   for i, r in enumerate(records)}
        tool.health_rerank_priority(records, current, dict(tool.DEFAULTS))
        self.assertEqual([r["new_priority"] for r in records], before)
        self.assertLess(records[0]["health_score"], records[1]["health_score"])

    def test_relay_registers_only_declared_aliases_and_keeps_consumer_model_fields(self):
        records, document = self.records(models=[
            {"name": "gpt-6.1-sol", "alias": "gpt-6.1-sol-agt", "force-mapping": True}])
        record = records[0]
        record["_relay_models"] = ["gpt-6.1-sol-agt"]
        models, aliases = relay_config._model_list(document["codex-api-key"][0], record)
        self.assertEqual(aliases, ["gpt-6.1-sol-agt"])
        self.assertEqual(models, document["codex-api-key"][0]["models"])
        self.assertEqual(request_relay.relay_model_mapping(record,
                         {"gpt-6.1-sol-agt": "gpt-6.1-sol"}),
                         {"gpt-6.1-sol-agt": "gpt-6.1-sol-agt"})

    def test_direct_source_never_gets_unrestricted_native_passthrough(self):
        records, _ = self.records()
        extra = self.account(records[0])["extra"]
        self.assertFalse(extra.get("openai_passthrough", False))
        self.assertFalse(extra.get("anthropic_passthrough", False))

    def test_source_exclusions_apply_to_public_alias_not_hidden_upstream_name(self):
        records, _ = self.records(models=[{"name": "gpt-6.1-sol", "alias": "isolated"}],
                                  excluded=["isolated"])
        self.assertEqual(self.account(records[0])["credentials"]["model_mapping"], {})
        records, _ = self.records(models=[{"name": "gpt-6.1-sol", "alias": "isolated"}],
                                  excluded=["gpt-6.1-sol"])
        self.assertEqual(self.account(records[0])["credentials"]["model_mapping"],
                         {"isolated": "gpt-6.1-sol"})

    def test_stopped_creation_is_shielded_until_one_atomic_stop_and_clear(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        payload = tool._source_create_payload(desired)
        self.assertEqual(payload["credentials"]["model_mapping"],
                         {tool.BLOCK_ALL_SENTINEL: tool.BLOCK_ALL_SENTINEL})
        self.assertFalse(payload["extra"]["openai_passthrough"])
        row = dict(copy.deepcopy(payload), id=31, status="active", schedulable=True)
        writes = []

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                writes.append(copy.deepcopy(change))
                row.update(copy.deepcopy(change))

            def bulk_update_accounts(self, ids, change):
                self.update_account(ids[0], change)

        tool._finalize_source_stopped_account(Api(), 31, desired)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["status"], "inactive")
        self.assertIs(writes[0]["schedulable"], False)
        self.assertEqual(writes[0]["credentials"]["model_mapping"], {})
        self.assertEqual(desired["credentials"]["model_mapping"], {})

    def test_stopped_sync_closes_route_in_same_write_as_removing_sentinel(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        current = tool._source_create_payload(desired)
        current.update(id=31, status="active", schedulable=True)
        change = tool.diff_account_fields(current, desired)
        self.assertEqual(change["credentials"]["model_mapping"], {})
        self.assertEqual(change["status"], "inactive")
        self.assertIs(change["schedulable"], False)

    def test_relay_rejects_raw_or_other_alias_before_consumer(self):
        record = {"prefix": "isolated-route", "models": ["gpt-6.1-sol-agt"],
                  "source_section": "codex-api-key"}
        import json
        for model in ("gpt-6.1-sol", "gpt-6.1-sol-other"):
            with self.assertRaises(RelayError):
                request_relay.prepare_request(record, "/v1/responses",
                    json.dumps({"model": model, "input": "synthetic"}).encode())
        _, body = request_relay.prepare_request(record, "/v1/responses",
            b'{"model":"gpt-6.1-sol-agt","input":"synthetic"}')
        self.assertEqual(json.loads(body)["model"], "isolated-route/gpt-6.1-sol-agt")

    def test_explicit_alias_pool_members_are_not_reclassified_as_fallback(self):
        records, document = self.records(models=[
            {"name": "gpt-6.1-sol", "alias": "pooled"},
            {"name": "gpt-5.4-mini", "alias": "pooled"}])
        record = records[0]
        record["_relay_models"] = ["pooled"]
        native_models, aliases = relay_config._model_list(document["codex-api-key"][0], record)
        self.assertEqual(native_models, document["codex-api-key"][0]["models"])
        self.assertEqual(aliases, ["pooled"])
        self.assertEqual(request_relay.relay_model_mapping(record, tool.record_model_mapping(record)),
                         {"pooled": "pooled"})

    def test_failed_stopped_readback_never_reports_success_or_writes_foreign_id(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        row = dict(tool._source_create_payload(desired), id=31, status="active", schedulable=True)
        for failure in ("foreign", "ignored"):
            writes = []

            class Api:
                def _call(self, method, path):
                    result = copy.deepcopy(row)
                    if failure == "foreign":
                        result["name"] = "foreign-manual-account"
                    return result

                def update_account(self, aid, change):
                    writes.append(copy.deepcopy(change))

                def bulk_update_accounts(self, ids, change):
                    self.update_account(ids[0], change)

            with self.assertRaises(ValueError):
                tool._finalize_source_stopped_account(Api(), 31, desired)
            self.assertEqual(len(writes), 0 if failure == "foreign" else 1)

    def test_source_stop_marker_restores_only_exact_clean_tool_stop(self):
        records, _ = self.records()
        record = records[0]
        desired = self.account(record)
        current = dict(copy.deepcopy(desired), status="inactive", schedulable=False)
        current["extra"]["cpa2sub2api_source_disabled"] = True
        restored = tool._source_stop_transition(current, desired, record)
        self.assertEqual((restored["status"], restored["schedulable"]), ("active", True))
        self.assertIs(restored["extra"]["cpa2sub2api_source_disabled"], False)
        for hold in ({"error_message": "operator or upstream hold"}, {"status": "error"},
                     {"schedulable": True}, {"overload_until": "2099-01-01T00:00:00Z"}):
            held = dict(copy.deepcopy(current), **hold)
            result = tool._source_stop_transition(held, desired, record)
            self.assertNotIn("status", result)
            self.assertNotIn("schedulable", result)
        manual = copy.deepcopy(current)
        manual["extra"].pop("cpa2sub2api_source_disabled")
        self.assertNotIn("status", tool._source_stop_transition(manual, desired, record))

    def test_preexisting_manual_stop_does_not_gain_source_restore_permission(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        manual = dict(copy.deepcopy(desired), status="inactive", schedulable=False)
        manual["extra"].pop("cpa2sub2api_source_disabled")
        result = tool._source_stop_transition(manual, desired, records[0])
        self.assertIs(result["extra"]["cpa2sub2api_source_disabled"], False)

    def test_sync_source_stop_then_reopen_is_atomic_and_idempotent(self):
        records, _ = self.records()
        record = records[0]
        row = dict(self.account(record), id=31, status="active", schedulable=True)
        writes = []

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                writes.append(copy.deepcopy(change))
                row.update(copy.deepcopy(change))

            def bulk_update_accounts(self, ids, change):
                self.update_account(ids[0], change)

        api = Api()
        cfg = dict(tool.DEFAULTS)
        record["excluded"] = ["*"]
        result = tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
                                             {record["name"]: copy.deepcopy(row)})
        self.assertEqual(result, (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("inactive", False))
        self.assertEqual(row["credentials"]["model_mapping"], {})
        self.assertIs(row["extra"]["cpa2sub2api_source_disabled"], True)
        self.assertEqual((writes[0]["status"], writes[0]["schedulable"]), ("inactive", False))
        self.assertTrue(all(writes[i].get("status", "inactive") == "inactive"
                            for i in range(len(writes))))
        stopped_writes = len(writes)
        record["excluded"] = []
        result = tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
                                             {record["name"]: copy.deepcopy(row)})
        self.assertEqual(result, (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("active", True))
        self.assertIs(row["extra"]["cpa2sub2api_source_disabled"], False)
        self.assertGreater(len(writes), stopped_writes)
        restored_writes = len(writes)
        self.assertEqual(tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
            {record["name"]: copy.deepcopy(row)}), (0, 1, []))
        self.assertEqual(len(writes), restored_writes)

    def test_fresh_manual_hold_revokes_restore_after_initial_snapshot(self):
        records, _ = self.records()
        record = records[0]
        initial = dict(self.account(record), id=31, status="inactive", schedulable=False)
        initial["extra"]["cpa2sub2api_source_disabled"] = True
        row = dict(copy.deepcopy(initial), status="error", error_message="concurrent operator hold")
        writes = []

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                writes.append(copy.deepcopy(change))
                row.update(copy.deepcopy(change))

            def bulk_update_accounts(self, ids, change):
                self.update_account(ids[0], change)

        result = tool.sync_existing_accounts(Api(), records, dict(tool.DEFAULTS), {"Codex": 1}, {},
                                             {record["name"]: initial})
        self.assertEqual(result, (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("error", False))
        self.assertTrue(writes)
        self.assertNotIn("status", writes[0])
        self.assertNotIn("schedulable", writes[0])

    def test_actual_http_retry_rechecks_ownership_and_does_not_report_success(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        row = dict(tool._source_create_payload(desired), id=31, status="active", schedulable=True)
        counts = {"get": 0, "put": 0}

        class Handler(BaseHTTPRequestHandler):
            def send(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                counts["get"] += 1
                self.send(200, {"data": row})

            def do_POST(self):
                counts["put"] += 1
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                row["name"] = "foreign-reassigned-account"
                self.send(500, {"message": "synthetic transient server failure"})

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        settings = dict(tool.DEFAULTS, sub2api_base_url="http://127.0.0.1:%d" % server.server_port,
                        sub2api_admin_key="synthetic-admin", http_timeout=2, http_retries=1)
        with self.assertRaises(tool.ProbeAuthorityRevoked):
            tool._finalize_source_stopped_account(tool.Sub2Api(settings), 31, desired)
        self.assertEqual(counts["put"], 1)
        self.assertGreaterEqual(counts["get"], 3)

    def test_final_stopped_readback_checks_extra_flags_not_only_empty_models(self):
        records, _ = self.records(excluded=["*"])
        desired = self.account(records[0])
        row = dict(tool._source_create_payload(desired), id=31, status="active", schedulable=True)

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                row.update(copy.deepcopy(change))
                row["extra"]["openai_passthrough"] = True

            def bulk_update_accounts(self, ids, change):
                self.update_account(ids[0], change)

        with self.assertRaises(ValueError):
            tool._finalize_source_stopped_account(Api(), 31, desired)

    def test_real_dto_put_ignores_schedulable_but_single_id_bulk_stops_and_reopens(self):
        records, _ = self.records()
        record = records[0]
        row = dict(self.account(record), id=31, status="active", schedulable=True)
        row["credentials"]["custom_headers"] = {"x-obsolete": "remove through replacement"}
        row["extra"]["cloak"] = {"mode": "obsolete"}
        calls = []

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                # Pinned UpdateAccountRequest does not deserialize schedulable.
                calls.append(("PUT", copy.deepcopy(change)))
                row.update({key: copy.deepcopy(value) for key, value in change.items()
                            if key != "schedulable"})

            def bulk_update_accounts(self, ids, change):
                if ids != [31] or set(change) - tool.SOURCE_BULK_FIELDS:
                    raise ValueError("not the pinned exact-ID bulk DTO")
                calls.append(("BULK", copy.deepcopy(change)))
                for key, value in change.items():
                    if key in ("credentials", "extra"):
                        row[key].update(copy.deepcopy(value))
                    else:
                        row[key] = copy.deepcopy(value)

        api = Api()
        cfg = dict(tool.DEFAULTS)
        record["excluded"] = ["*"]
        self.assertEqual(tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
            {record["name"]: copy.deepcopy(row)}), (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("inactive", False))
        self.assertEqual(row["credentials"]["model_mapping"], {})
        self.assertNotIn("custom_headers", row["credentials"])
        self.assertNotIn("cloak", row["extra"])
        self.assertEqual(calls[0], ("BULK", {"status": "inactive", "schedulable": False}))
        self.assertTrue(any(method == "PUT" for method, _ in calls))
        record["excluded"] = []
        self.assertEqual(tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
            {record["name"]: copy.deepcopy(row)}), (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("active", True))

    def test_runtime_hold_after_parameter_put_never_restarts_source(self):
        records, _ = self.records()
        record = records[0]
        desired = self.account(record)
        for hold in ({"error_message": "new upstream error"},
                     {"temp_unschedulable_until": "2099-01-01T00:00:00Z"},
                     {"rate_limit_reset_at": "2099-01-01T00:00:00Z"}):
            with self.subTest(hold=hold):
                row = dict(copy.deepcopy(desired), id=31, status="inactive", schedulable=False)
                row["extra"]["cpa2sub2api_source_disabled"] = True
                row["notes"] += " previous source stop"
                row["credentials"]["model_mapping"] = {"old-model": "old-model"}
                writes = []

                class Api:
                    def _call(self, method, path):
                        return copy.deepcopy(row)

                    def update_account(self, aid, change):
                        writes.append(("PUT", copy.deepcopy(change)))
                        row.update(copy.deepcopy(change))
                        row.update(hold)

                    def bulk_update_accounts(self, ids, change):
                        writes.append(("BULK", copy.deepcopy(change)))
                        row.update(copy.deepcopy(change))

                original = copy.deepcopy(row)
                result = tool.sync_existing_accounts(Api(), records, dict(tool.DEFAULTS), {"Codex": 1}, {},
                                                     {record["name"]: original})
                self.assertTrue(any(method == "PUT" for method, _ in writes), writes)
                self.assertEqual(result[:2], (0, 0))
                self.assertTrue(result[2])
                self.assertEqual((row["status"], row["schedulable"]), ("inactive", False))
                self.assertFalse(any(patch.get("status") == "active" or patch.get("schedulable") is True
                                     for _, patch in writes))

    def test_source_restore_notes_only_with_masked_credentials_uses_complete_snapshot(self):
        records, _ = self.records()
        record = records[0]
        desired = self.account(record)
        row = dict(copy.deepcopy(desired), id=31, status="inactive", schedulable=False)
        row["extra"]["cpa2sub2api_source_disabled"] = True
        row["notes"] += " previous source stop"
        writes = []

        class Api:
            def _call(self, method, path):
                public = copy.deepcopy(row)
                public["credentials"].pop("api_key", None)
                return public

            def reveal_account_credentials(self, account):
                return copy.deepcopy(row["credentials"])

            def update_account(self, aid, change):
                writes.append(("PUT", copy.deepcopy(change)))
                row.update(copy.deepcopy(change))

            def bulk_update_accounts(self, ids, change):
                writes.append(("BULK", copy.deepcopy(change)))
                row.update(copy.deepcopy(change))

        api = Api()
        result = tool.sync_existing_accounts(api, records, dict(tool.DEFAULTS), {"Codex": 1}, {},
                                             {record["name"]: api._call("GET", "unused")})
        self.assertEqual(result, (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("active", True))
        self.assertEqual(row["credentials"]["api_key"], "synthetic-key-0")
        self.assertEqual(writes[0], ("BULK", {"status": "inactive", "schedulable": False}))

    def test_native_stripped_billing_false_is_same_disabled_state_and_sync_noop(self):
        records, _ = self.records()
        record = records[0]
        desired = self.account(record)
        native = dict(copy.deepcopy(desired), id=31, status="active", schedulable=True)
        for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
            native["extra"].pop(key)
        native["extra"]["openai_long_context_billing_enabled"] = False
        native["credentials"].update(pool_mode=True, pool_mode_retry_count=2)
        self.assertEqual(tool.diff_account_fields(native, desired), {})

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(native)

            def update_account(self, *args):
                raise AssertionError("native-equivalent billing defaults must be a no-op")

            def bulk_update_accounts(self, *args):
                raise AssertionError("native-equivalent billing defaults must be a no-op")

        self.assertEqual(tool.sync_existing_accounts(Api(), records, dict(tool.DEFAULTS), {"Codex": 1}, {},
                         {record["name"]: native}), (0, 1, []))

    def test_native_finalizer_readback_strips_only_system_billing_false(self):
        records, _ = self.records(excluded=["*"])
        record = records[0]
        desired = self.account(record)
        row = dict(tool._source_create_payload(desired), id=31, status="active", schedulable=True)
        for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
            row["extra"].pop(key)
        row["extra"]["openai_long_context_billing_enabled"] = False
        row["credentials"].update(pool_mode=True, pool_mode_retry_count=2)

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def bulk_update_accounts(self, ids, change):
                if ids != [31]:
                    raise AssertionError("must update one exact owned ID")
                for key, value in change.items():
                    if key in ("extra", "credentials"):
                        row[key].update(copy.deepcopy(value))
                    else:
                        row[key] = copy.deepcopy(value)
                for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
                    row["extra"].pop(key, None)

        tool._finalize_source_stopped_account(Api(), 31, desired)
        self.assertEqual((row["status"], row["schedulable"]), ("inactive", False))
        self.assertEqual(row["credentials"]["model_mapping"], {})
        self.assertIs(row["extra"]["openai_long_context_billing_enabled"], False)

    def test_billing_true_and_other_missing_managed_fields_are_not_normalized_away(self):
        records, _ = self.records()
        desired = self.account(records[0])
        for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
            current = copy.deepcopy(desired)
            current["extra"][key] = True
            patch = tool.diff_account_fields(current, desired)
            self.assertIn(key, patch)
            self.assertIs(patch[key], False)
            self.assertIs(patch["extra"][key], False)
        current = copy.deepcopy(desired)
        current["extra"].pop("openai_passthrough")
        self.assertIn("extra", tool.diff_account_fields(current, desired))

    def test_native_stripped_billing_during_notes_only_restore_stays_idempotent(self):
        records, _ = self.records()
        record = records[0]
        desired = self.account(record)
        row = dict(copy.deepcopy(desired), id=31, status="inactive", schedulable=False)
        row["extra"]["cpa2sub2api_source_disabled"] = True
        row["notes"] += " previous source stop"
        for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
            row["extra"].pop(key)
        writes = []

        class Api:
            def _call(self, method, path):
                return copy.deepcopy(row)

            def update_account(self, aid, change):
                writes.append(("PUT", copy.deepcopy(change)))
                row.update(copy.deepcopy(change))
                for key in ("upstream_billing_probe_enabled", "upstream_billing_rate_sync_enabled"):
                    row["extra"].pop(key, None)

            def bulk_update_accounts(self, ids, change):
                self.update_account(ids[0], change)

        api = Api()
        cfg = dict(tool.DEFAULTS)
        self.assertEqual(tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
            {record["name"]: copy.deepcopy(row)}), (1, 0, []))
        self.assertEqual((row["status"], row["schedulable"]), ("active", True))
        count = len(writes)
        self.assertEqual(tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {},
            {record["name"]: copy.deepcopy(row)}), (0, 1, []))
        self.assertEqual(len(writes), count)


if __name__ == "__main__":
    unittest.main()
