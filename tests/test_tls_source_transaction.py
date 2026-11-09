"""Source TLS learning preserves full state and rejects stale proof publication."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from ._test_compat import write_text_lf
else:
    from _test_compat import write_text_lf

import tool
import tls_diagnostic_transaction as tls
from relay.config import RelayError, export_bundle, load_bundle, route_id


PROVENANCE = {"feature_id": "cpa2sub2api-diagnostic-no-redirect-v1",
    "source_commit": "91e0dee827abf476f9f86246f68c500a112f7155", "binary_sha256": "a" * 64}


class Driver:
    def __init__(self, outcomes, action=None):
        self.outcomes, self.action, self.calls = list(outcomes), action, []

    def run(self, record, source, model, profile, settings, validate):
        validate()
        self.calls.append(copy.deepcopy(profile))
        if self.action:
            self.action(len(self.calls))
        result = self.outcomes.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class TLSSourceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.settings_path = Path(self.tmp.name) / "settings.json"
        self.document = {"openai-compatibility": [{"name": "fixture", "base-url": "https://fixture.invalid",
            "api-key-entries": [{"api-key": "fixture-a"}, {"api-key": "fixture-b"}],
            "models": [{"name": "gpt-fixture", "alias": "*"}]}],
            "payload": {"default": [{"models": [{"name": "gpt-fixture", "protocol": "openai"}],
                "params": {"max_tokens": 16}}]}}
        self.records, _ = tool.collect(self.document)
        for record in self.records:
            record["_source_scope"] = "default"
            record["_relay_models"] = ["gpt-fixture"]
        tool.assign_names(self.records)
        self.bundle = export_bundle(self.records, self.document, "default", self.root)
        self.settings = {"request_relay_state_dir": str(self.root), "request_relay_enabled": True,
            "request_relay_force_all_accounts": True, "model_probe_enabled": True,
            "online_probe_hosts": {"fixture.invalid": True}, "unrelated": {"secret": "fixture-only"}}
        write_text_lf(self.settings_path, json.dumps(self.settings), encoding="utf8")
        self.original = tls.state_snapshot(self.root, self.settings_path)
        self.record = self.records[0]
        self.binding = tls.source_binding(self.record, self.document, "gpt-fixture", ["gpt-fixture"], PROVENANCE)
        self.allowed = True

    def validate(self):
        if not self.allowed:
            raise RuntimeError("fixture authorization revoked")

    def compare(self, driver):
        return tls.compare_and_publish(self.records, self.document, self.record, "gpt-fixture",
            self.settings, self.bundle, self.settings_path, PROVENANCE, self.validate, driver)

    def test_failure_and_baseline_success_do_not_touch_main_state(self):
        for outcomes in ([False, False], [True]):
            with self.subTest(outcomes=outcomes):
                driver = Driver(outcomes)
                result = self.compare(driver)
                self.assertFalse(result["saved"])
                self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)
                self.assertEqual(len(driver.calls), len(outcomes))
                if len(outcomes) == 2:
                    self.assertEqual(driver.calls, [None, {"impersonate": "chrome136"}])

    def test_success_publishes_bound_setting_and_complete_generation_only(self):
        result = self.compare(Driver([False, True]))
        self.assertTrue(result["saved"])
        current = load_bundle(self.root)
        self.assertEqual(set(current["routes"]), set(self.bundle["routes"]))
        self.assertEqual(current["state"]["route_tokens"], self.bundle["state"]["route_tokens"])
        self.assertEqual(current["state"]["consumer_token"], self.bundle["state"]["consumer_token"])
        self.assertEqual(current["consumer_config"]["payload"], self.bundle["consumer_config"]["payload"])
        rid = route_id(self.record, "default")
        self.assertEqual(current["transports"][rid]["impersonate"], "chrome136")
        saved = json.loads(self.settings_path.read_text())
        self.assertEqual(saved["unrelated"], self.settings["unrelated"])
        item = saved[tls.LEARNED_KEY][rid]
        self.assertEqual(item["binding"], self.binding)
        self.assertEqual(tls.learned_profile(self.record, self.document, saved, ["gpt-fixture"], PROVENANCE),
            {"impersonate": "chrome136"})
        self.assertFalse((self.root / tls.JOURNAL).exists())

    def test_source_key_model_and_binary_changes_invalidate_learned_parameters(self):
        self.compare(Driver([False, True]))
        saved = json.loads(self.settings_path.read_text())
        changed_key = dict(self.record, api_key="different-fixture-key")
        changed_source = dict(self.document, **{"disable-cooling": True})
        for record, source, models, provenance in (
            (changed_key, self.document, ["gpt-fixture"], PROVENANCE),
            (self.record, changed_source, ["gpt-fixture"], PROVENANCE),
            (self.record, self.document, ["other-model"], PROVENANCE),
            (self.record, self.document, ["gpt-fixture"], dict(PROVENANCE, binary_sha256="b" * 64))):
            self.assertIsNone(tls.learned_profile(record, source, saved, models, provenance))

    def test_cancel_and_revoke_after_success_do_not_publish(self):
        with self.assertRaises(KeyboardInterrupt):
            self.compare(Driver([KeyboardInterrupt()]))
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)
        def revoke(call):
            if call == 2:
                self.allowed = False
        with self.assertRaises(RuntimeError):
            self.compare(Driver([False, True], revoke))
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)

    def test_concurrent_settings_change_is_preserved_and_blocks_publish(self):
        def concurrent(call):
            if call == 2:
                self.settings_path.write_text('{"other_writer":true}', encoding="utf8")
        with self.assertRaisesRegex(RelayError, "state_changed"):
            self.compare(Driver([False, True], concurrent))
        self.assertEqual(json.loads(self.settings_path.read_text()), {"other_writer": True})
        now = tls.state_snapshot(self.root, self.settings_path)
        self.assertEqual(now["current"], self.original["current"])
        self.assertEqual(now["secrets"], self.original["secrets"])
        self.assertEqual(now["generations"], self.original["generations"])

    def test_failure_during_publication_restores_exact_bytes_and_generation_set(self):
        original_write = tls._atomic_write
        failed = False
        def fail_once(path, value):
            nonlocal failed
            if str(path) == str(self.settings_path) and not failed:
                failed = True
                raise OSError("fixture interrupted settings write")
            return original_write(path, value)
        with patch.object(tls, "_atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.compare(Driver([False, True]))
        self.assertTrue(failed)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)
        self.assertFalse((self.root / tls.JOURNAL).exists())

    def test_revoked_final_check_rolls_back_a_complete_commit(self):
        def revoke_after_pointer():
            if (self.root / "CURRENT").read_bytes() != self.original["current"]:
                raise RuntimeError("fixture consent revoked at final check")
        with self.assertRaises(RuntimeError):
            tls.publish_success(self.root, self.settings_path, self.original, self.records,
                self.document, self.bundle, self.binding, {"impersonate": "chrome136"}, revoke_after_pointer)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)

    def test_partial_crash_journal_recovers_before_the_next_transaction(self):
        original_write = tls._atomic_write
        def crash_before_settings(path, value):
            if str(path) == str(self.settings_path):
                raise OSError("fixture crash")
            return original_write(path, value)
        original_recover = tls.recover_transaction
        def prevent_crash_recovery(state_dir, settings_path):
            if (Path(state_dir) / tls.JOURNAL).exists():
                raise RuntimeError("fixture process disappeared")
            return original_recover(state_dir, settings_path)
        with patch.object(tls, "_atomic_write", side_effect=crash_before_settings), \
                patch.object(tls, "recover_transaction", side_effect=prevent_crash_recovery):
            with self.assertRaises(RuntimeError):
                self.compare(Driver([False, True]))
        self.assertTrue((self.root / tls.JOURNAL).exists())
        tls.recover_transaction(self.root, self.settings_path)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)

    def test_crash_after_current_before_authority_commit_recovers_all_state(self):
        original_write = tls._atomic_write
        def process_disappears(path, value):
            if Path(path) == self.root / "CURRENT":
                if value == self.original["current"]:
                    raise RuntimeError("fixture recovery process unavailable")
                original_write(path, value)
                raise SystemExit("fixture vanished before final authority check")
            return original_write(path, value)
        with patch.object(tls, "_atomic_write", side_effect=process_disappears):
            with self.assertRaises(RuntimeError):
                self.compare(Driver([False, True]))
        self.assertTrue((self.root / tls.JOURNAL).exists())
        self.assertNotEqual((self.root / "CURRENT").read_bytes(), self.original["current"])
        tls.recover_transaction(self.root, self.settings_path)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)
        self.assertFalse((self.root / tls.JOURNAL).exists())

    def test_accepted_commit_with_delayed_journal_cleanup_remains_complete(self):
        original_unlink = Path.unlink
        def unavailable_cleanup(path, *args, **kwargs):
            if path == self.root / tls.JOURNAL:
                raise OSError("fixture cleanup temporarily unavailable")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unavailable_cleanup):
            result = self.compare(Driver([False, True]))
        self.assertTrue(result["saved"])
        accepted = tls.state_snapshot(self.root, self.settings_path)
        journal = json.loads((self.root / tls.JOURNAL).read_text())
        self.assertEqual(journal["phase"], "committed")
        tls.recover_transaction(self.root, self.settings_path)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), accepted)
        self.assertEqual(load_bundle(self.root)["generation"], result["generation"])

    def test_accepted_journal_conflict_never_rolls_back_accepted_runtime(self):
        original_unlink = Path.unlink
        def unavailable_cleanup(path, *args, **kwargs):
            if path == self.root / tls.JOURNAL:
                raise OSError("fixture cleanup temporarily unavailable")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unavailable_cleanup):
            self.compare(Driver([False, True]))
        accepted = tls.state_snapshot(self.root, self.settings_path)
        saved = json.loads(self.settings_path.read_text())
        saved["external_writer"] = True
        self.settings_path.write_text(json.dumps(saved), encoding="utf8")
        external = self.settings_path.read_bytes()
        with self.assertRaisesRegex(RelayError, "concurrent_change_needs_review"):
            tls.recover_transaction(self.root, self.settings_path)
        current = tls.state_snapshot(self.root, self.settings_path)
        self.assertEqual(current["settings"], external)
        self.assertEqual(current["current"], accepted["current"])
        self.assertEqual(current["secrets"], accepted["secrets"])
        self.assertEqual(current["generations"], accepted["generations"])
        self.assertTrue((self.root / tls.JOURNAL).exists())
        self.assertEqual(load_bundle(self.root)["generation"], accepted["current"].decode().strip())

    def test_settings_edit_after_its_write_is_detected_before_authority_commit(self):
        original_write = tls._atomic_write
        external = b'{"external_writer":true}'
        def concurrent_after_settings(path, value):
            original_write(path, value)
            if Path(path) == self.settings_path:
                self.settings_path.write_bytes(external)
        with patch.object(tls, "_atomic_write", side_effect=concurrent_after_settings):
            with self.assertRaisesRegex(RelayError, "concurrent_change_needs_review"):
                self.compare(Driver([False, True]))
        current = tls.state_snapshot(self.root, self.settings_path)
        self.assertEqual(current["settings"], external)
        self.assertEqual(current["current"], self.original["current"])
        self.assertEqual(current["secrets"], self.original["secrets"])
        self.assertEqual(current["generations"], self.original["generations"])
        self.assertEqual(json.loads((self.root / tls.JOURNAL).read_text())["phase"], "prepared")

    def test_old_generation_change_during_publication_cannot_escape_final_cas(self):
        original_write = tls._atomic_write
        original_routes = self.root / "generations" / self.bundle["generation"] / "routes.json"
        def concurrent_generation(path, value):
            original_write(path, value)
            if Path(path) == self.settings_path:
                original_routes.write_bytes(self.original["generations"][self.bundle["generation"]]["routes.json"] + b" ")
        with patch.object(tls, "_atomic_write", side_effect=concurrent_generation):
            with self.assertRaisesRegex(RelayError, "state_changed"):
                self.compare(Driver([False, True]))
        self.assertEqual(self.settings_path.read_bytes(), self.original["settings"])
        self.assertEqual((self.root / "CURRENT").read_bytes(), self.original["current"])
        self.assertEqual((self.root / "secrets.json").read_bytes(), self.original["secrets"])
        self.assertEqual(original_routes.read_bytes(), self.original["generations"][self.bundle["generation"]]["routes.json"] + b" ")
        self.assertEqual(set(tls.state_snapshot(self.root, self.settings_path)["generations"]), set(self.original["generations"]))

    def test_concurrent_pointer_or_secrets_change_blocks_publish_and_preserves_writer(self):
        for name, value in (("CURRENT", b"b" * 64 + b"\n"), ("secrets.json", b'{"external_writer":true}')):
            with self.subTest(name=name):
                target = self.root / name
                def change(call):
                    if call == 2:
                        target.write_bytes(value)
                with self.assertRaisesRegex(RelayError, "state_changed"):
                    self.compare(Driver([False, True], change))
                self.assertEqual(target.read_bytes(), value)
                self.assertEqual(self.settings_path.read_bytes(), self.original["settings"])
                target.write_bytes(self.original["current" if name == "CURRENT" else "secrets"])
                self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)

    def test_missing_other_route_is_rejected_without_partial_generation(self):
        with self.assertRaisesRegex(RelayError, "full_route_contract_changed"):
            tls.publish_success(self.root, self.settings_path, self.original, [self.record],
                self.document, self.bundle, self.binding, {"impersonate": "chrome136"}, self.validate)
        self.assertEqual(tls.state_snapshot(self.root, self.settings_path), self.original)
        self.assertFalse((self.root / tls.JOURNAL).exists())

    def test_generation_tamper_and_route_set_shrink_cannot_publish(self):
        def tamper(call):
            if call == 2:
                directory = self.root / "generations" / self.bundle["generation"]
                with (directory / "routes.json").open("ab") as stream:
                    stream.write(b" ")
        with self.assertRaisesRegex(RelayError, "state_changed"):
            self.compare(Driver([False, True], tamper))
        self.assertEqual((self.root / "CURRENT").read_bytes(), self.original["current"])


if __name__ == "__main__":
    unittest.main()
