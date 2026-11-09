"""Saved probe authority is explicit; an operator hard disable always wins."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from ._test_compat import CompatTestCase
else:
    from _test_compat import CompatTestCase

import model_probe
import probe_policy
import probe_web
import tool


class EnvironmentAuthorityTests(CompatTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_path = self.root / "settings.json"
        self.config_path = self.root / "config.yaml"
        self.settings_path.write_text(json.dumps({"unrelated": {"fixture": "preserve"}}), encoding="utf8")
        self.config_path.write_text("openai-compatibility:\n- name: fixture\n  base-url: https://fixture.example\n  api-key-entries:\n  - api-key: fixture-only\n  models:\n  - name: gpt-fixture\n", encoding="utf8")
        self.enterContext(patch.object(tool, "SETTINGS_PATH", str(self.settings_path)))
        self.enterContext(patch.object(tool, "CONFIG_YAML", str(self.config_path)))
        self.enterContext(patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false",
            "MODEL_PROBE_HARD_DISABLED": "false", "ONLINE_PROBE_HOSTS": ""}))
        self.enterContext(patch.object(tool, "_http", side_effect=AssertionError("provider HTTP forbidden")))
        self.service = probe_web.ProbeService()

    def save(self, **values):
        saved = json.loads(self.settings_path.read_text())
        saved.update(values)
        self.settings_path.write_text(json.dumps(saved), encoding="utf8")

    def enable_in_ui(self):
        self.service.update({"hosts": ["fixture.example"], "enabled": True})
        return self.service.update({"master": True})

    def test_env_false_is_default_until_ui_explicitly_saves_master_true(self):
        self.assertFalse(tool.load_settings()["model_probe_enabled"])
        with patch.object(self.service, "_reconcile") as reconcile:
            response = self.enable_in_ui()
        reconcile.assert_not_called()
        self.assertTrue(response["state"]["master"]["desired"])
        self.assertTrue(response["state"]["master"]["effective"])
        self.assertFalse(response["state"]["master"]["environmentOverride"])
        self.assertTrue(response["state"]["sites"][0]["effective"])
        self.assertTrue(probe_policy.probe_allowed("https://fixture.example", tool.load_settings()))
        self.assertEqual(json.loads(self.settings_path.read_text())["unrelated"], {"fixture": "preserve"})

    def test_saved_false_remains_revoked_under_env_true(self):
        self.save(model_probe_enabled=False, online_probe_hosts={"fixture.example": True})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true"}):
            self.assertFalse(tool.load_settings()["model_probe_enabled"])
            self.assertFalse(self.service.snapshot()["master"]["effective"])
            self.assertFalse(probe_policy.probe_allowed("fixture.example", tool.load_settings()))

    def test_hard_true_blocks_ui_and_runtime_even_after_saved_opt_in(self):
        self.save(model_probe_enabled=True, online_probe_hosts={"fixture.example": True})
        before = self.settings_path.read_bytes()
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}), \
                patch.object(self.service, "_reconcile") as reconcile:
            for payload in ({"master": True}, {"hosts": ["fixture.example"], "enabled": True}):
                with self.assertRaises(probe_web.UiError) as rejected:
                    self.service.update(payload)
                self.assertEqual(rejected.exception.status, 409)
                self.assertEqual(rejected.exception.code, "PROBE_HARD_DISABLED")
            state = self.service.snapshot()
            effective = tool.load_settings()
        reconcile.assert_not_called()
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertTrue(state["master"]["desired"])
        self.assertFalse(state["master"]["effective"])
        self.assertTrue(state["master"]["hardDisabled"])
        self.assertIn("MODEL_PROBE_HARD_DISABLED", state["master"]["hardDisableReason"])
        self.assertFalse(state["sites"][0]["effective"])
        self.assertIn("MODEL_PROBE_HARD_DISABLED", state["sites"][0]["reason"])
        self.assertFalse(effective["model_probe_enabled"])
        self.assertTrue(effective["model_probe_hard_disabled"])

    def test_hard_env_cannot_be_bypassed_by_explicit_caller_settings(self):
        explicit = {"model_probe_enabled": True, "model_probe_hard_disabled": False,
            "online_probe_hosts": {"fixture.example": True}}
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            self.assertFalse(probe_policy.probe_allowed("fixture.example", explicit))
            http = unittest.mock.Mock(side_effect=AssertionError("source POST forbidden"))
            result = model_probe._probe_once(http, "openai", "https://fixture.example", "fixture-only",
                "gpt-fixture", {}, None, explicit)
        self.assertFalse(result[0])
        http.assert_not_called()

    def test_current_policy_replaces_stale_saved_hard_disable_false(self):
        self.save(model_probe_enabled=True, online_probe_hosts={"fixture.example": True})
        stale = {"model_probe_enabled": True, "model_probe_hard_disabled": False,
            "online_probe_hosts": {"fixture.example": True}}
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            current = tool.current_probe_policy(stale)
        self.assertTrue(current["model_probe_hard_disabled"])
        self.assertFalse(current["model_probe_enabled"])
        self.assertFalse(probe_policy.probe_allowed("fixture.example", current))

    def test_saved_hard_disable_is_not_removed_by_false_environment_default(self):
        self.save(model_probe_enabled=True, model_probe_hard_disabled=True,
            online_probe_hosts={"fixture.example": True})
        self.assertTrue(tool.load_settings()["model_probe_hard_disabled"])
        self.assertFalse(tool.load_settings()["model_probe_enabled"])

    def test_permanent_deny_remains_independent_of_saved_opt_in(self):
        self.enable_in_ui()
        original = probe_policy.permanently_denied
        with patch.object(probe_policy, "permanently_denied", side_effect=lambda host:
                probe_policy.normalize_host(host) == "fixture.example" or original(host)):
            state = self.service.snapshot()
            self.assertTrue(state["sites"][0]["locked"])
            self.assertFalse(state["sites"][0]["effective"])
            self.assertIn("永久禁探", state["sites"][0]["reason"])

    def test_hard_true_disables_existing_weekly_plan_without_enabling_it(self):
        try:
            from tests.test_probe_policy import ProbePolicyTests
        except ImportError:
            from test_probe_policy import ProbePolicyTests
        account, settings, _ = ProbePolicyTests.guarded_plan_fixture(self, "gpt-9")
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": True, "auto_recover": True,
                    "cron_expression": "*/30 * * * *", "model_id": "gpt-9"}
                self.writes = []
            def _call(self, method, path, body=None):
                if method == "GET":
                    return dict(account)
                self.writes.append(dict(body))
                self.plan.update(body)
            def list_test_plans(self, aid):
                return [dict(self.plan)]
        api = Api()
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            count, failures = tool.ensure_test_plans(api, {account["name"]: 7}, settings)
        self.assertEqual((count, failures), (1, []))
        self.assertFalse(api.plan["enabled"])
        self.assertFalse(api.plan["auto_recover"])
        self.assertFalse(any(write.get("enabled") or write.get("auto_recover") for write in api.writes))
        self.assertEqual(api.plan["cron_expression"].split()[1:], ["3", "*", "*", "0"])

    def test_hard_true_prevents_recovery_and_tls_trials_with_full_state_unchanged(self):
        try:
            from tests.test_tls_diagnostics import SourceFixture
        except ImportError:
            from test_tls_diagnostics import SourceFixture
        import tls_diagnostic_transaction as tls
        fixture = SourceFixture()
        self.addCleanup(fixture.close)
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            result, calls = fixture.diagnose([(200, '{"choices":[{}]}')])
            self.assertEqual((result, calls), ((set(), []), []))
            fixture.api.row.update(status="inactive", schedulable=False)
            settings = dict(fixture.settings, explicit_recovery_account_ids=[7])
            with patch.object(tool, "_test_relay_account_exact", side_effect=AssertionError("recovery source POST forbidden")):
                evidence, failures = tool.probe_import_recovery(fixture.api, {fixture.api.row["name"]: 7}, settings)
            self.assertEqual((evidence, failures), ({}, []))
        self.assertEqual(fixture.api.writes, [])
        self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)

    def test_hard_activation_at_final_write_boundary_prevents_sensitive_mutation(self):
        account = {"id": 7, "notes": tool.NOTE_SENTINEL, "status": "inactive",
            "credentials": {"base_url": "https://fixture.example"}}
        settings = {"model_probe_enabled": True, "online_probe_hosts": {"fixture.example": True}}
        class Api:
            def __init__(self):
                self.writes = []
            def _call(self, method, path, body=None):
                if method == "GET":
                    os.environ["MODEL_PROBE_HARD_DISABLED"] = "true"
                    return dict(account)
                self.writes.append(body)
        api = Api()
        with self.assertRaises(tool.ProbeAuthorityRevoked):
            with tool._guarded_admin_write(tool._account_probe_write_guard(api, account, settings)):
                api._call("PUT", "/api/v1/admin/accounts/7", {"status": "active"})
        self.assertEqual(api.writes, [])

    def test_hard_activation_inside_ui_atomic_save_preserves_original_bytes(self):
        before = self.settings_path.read_bytes()
        original_state = self.service._state
        def state_then_disable():
            state = original_state()
            os.environ["MODEL_PROBE_HARD_DISABLED"] = "true"
            return state
        with patch.object(self.service, "_state", side_effect=state_then_disable):
            with self.assertRaises(probe_web.UiError) as rejected:
                self.service.update({"master": True})
        self.assertEqual(rejected.exception.code, "PROBE_HARD_DISABLED")
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_environment_host_allowlist_still_limits_saved_grants(self):
        self.enable_in_ui()
        with patch.dict(os.environ, {"ONLINE_PROBE_HOSTS": "other.example"}):
            current = tool.load_settings()
            self.assertTrue(current["model_probe_enabled"])
            self.assertFalse(probe_policy.probe_allowed("fixture.example", current))
            self.assertFalse(self.service.snapshot()["sites"][0]["effective"])

    def test_shared_saved_settings_have_same_effect_across_separate_processes(self):
        self.enable_in_ui()
        code = "import json,tool; s=tool.load_settings(); print(json.dumps({'master':s['model_probe_enabled'],'hard':s.get('model_probe_hard_disabled',False),'allowed':tool.probe_policy.probe_allowed('fixture.example',s)}))"
        environment = dict(os.environ, SETTINGS_PATH=str(self.settings_path),
            MODEL_PROBE_ENABLED="false", MODEL_PROBE_HARD_DISABLED="false")
        states = []
        for _ in range(3):
            result = subprocess.run([sys.executable, "-c", code], cwd=Path(tool.__file__).parent,
                env=environment, check=True, capture_output=True, text=True)
            states.append(json.loads(result.stdout))
        self.assertEqual(states, [{"master": True, "hard": False, "allowed": True}] * 3)
        self.save(model_probe_enabled=False)
        environment["MODEL_PROBE_ENABLED"] = "true"
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(tool.__file__).parent,
            env=environment, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), {"master": False, "hard": False, "allowed": False})
        self.save(model_probe_enabled=True)
        environment["MODEL_PROBE_HARD_DISABLED"] = "true"
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(tool.__file__).parent,
            env=environment, check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), {"master": False, "hard": True, "allowed": False})


if __name__ == "__main__":
    unittest.main()
