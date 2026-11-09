"""Isolated settings transactions and live consent; no upstream requests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tool


class SettingsTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "settings.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tool, "SETTINGS_PATH", str(self.path)))
        self.stack.enter_context(patch.object(tool, "CONFIG_YAML", str(Path(self.tmp.name) / "missing.yaml")))
        self.stack.enter_context(patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "", "ONLINE_PROBE_HOSTS": "",
                                                       "SUB2API_ADMIN_KEY": "environment-only-secret"}))
        self.stack.enter_context(patch.object(tool, "_http", side_effect=AssertionError("network forbidden")))

    def write(self, saved):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(saved), encoding="utf-8")

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_new_transaction_creates_missing_parent_without_environment_secrets(self):
        result = tool.update_saved_settings(lambda saved: dict(saved, model_probe_enabled=False))
        self.assertEqual(result, {"model_probe_enabled": False})
        self.assertEqual(self.saved(), result)
        self.assertNotIn("environment-only-secret", self.path.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_transaction_preserves_unknown_nested_fields_and_returns_independent_copy(self):
        original = {"unknown": {"members": ["kept"]}, "sub2api_admin_key": "stored-synthetic"}
        self.write(original)
        supplied = []
        def mutate(saved):
            supplied.append(saved)
            saved["online_probe_hosts"] = {"alpha.example": False}
            return saved
        result = tool.update_saved_settings(mutate)
        supplied[0]["unknown"]["members"].append("later")
        result["unknown"]["members"].append("returned")
        self.assertEqual(self.saved()["unknown"], original["unknown"])
        self.assertEqual(self.saved()["sub2api_admin_key"], "stored-synthetic")
        self.assertNotIn("sub2api_base_url", self.saved())

    def test_malformed_or_nonobject_settings_are_never_reset(self):
        for contents in ("{private-broken", "[]", "null", '"string"', '{"number": NaN}', '{"number": Infinity}'):
            with self.subTest(contents=contents):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises(tool.SettingsFormatError) as error:
                    tool.update_saved_settings(lambda saved: {"reset": True})
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)
                self.assertNotIn("private-broken", str(error.exception))

    def test_mutator_failure_and_unserializable_result_leave_original_untouched(self):
        original = {"keep": True}
        self.write(original)
        def failed(saved):
            saved["keep"] = False
            raise ValueError("caller failed")
        with self.assertRaisesRegex(ValueError, "caller failed"):
            tool.update_saved_settings(failed)
        with self.assertRaises(TypeError):
            tool.update_saved_settings(lambda saved: None)
        with self.assertRaises(TypeError):
            tool.update_saved_settings(lambda saved: {"invalid": object()})
        self.assertEqual(self.saved(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_atomic_replace_failure_keeps_existing_file(self):
        self.write({"keep": True})
        with patch.object(tool.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(tool.SettingsError):
                tool.update_saved_settings(lambda saved: dict(saved, keep=False))
        self.assertEqual(self.saved(), {"keep": True})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_lock_path_is_explicit_failure_without_changing_settings(self):
        self.write({"keep": True})
        Path(str(self.path) + ".lock").mkdir()
        with self.assertRaises(tool.SettingsError):
            tool.update_saved_settings(lambda saved: {"reset": True})
        self.assertEqual(self.saved(), {"keep": True})

    def test_corrupted_lock_file_is_not_silently_replaced(self):
        self.write({"keep": True})
        lock = Path(str(self.path) + ".lock")
        lock.write_bytes(b"corrupted-lock")
        with self.assertRaises(tool.SettingsError):
            tool.update_saved_settings(lambda saved: {"reset": True})
        self.assertEqual(lock.read_bytes(), b"corrupted-lock")
        self.assertEqual(self.saved(), {"keep": True})

    def test_lock_timeout_preserves_settings(self):
        self.write({"keep": True})
        script = '''import os, sys
p=sys.argv[1]
with open(p, "w+b") as lock:
    lock.write(b"\\0"); lock.flush(); lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    print("ready", flush=True)
    sys.stdin.readline()
'''
        worker = subprocess.Popen([sys.executable, "-c", script, str(self.path) + ".lock"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(), "ready")
            with self.assertRaises(tool.SettingsLockTimeout):
                tool.update_saved_settings(lambda saved: {"reset": True}, timeout=0.05)
            self.assertEqual(self.saved(), {"keep": True})
        finally:
            worker.communicate("release\n", timeout=5)

    def test_two_processes_preserve_all_mutations_and_cli_site_updates(self):
        self.write({"unknown": {"preserve": True}})
        script = '''import contextlib, io, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["SETTINGS_PATH"] = sys.argv[2]
import tool
tool.CONFIG_YAML = sys.argv[2] + ".missing"
tool._http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
label=sys.argv[3]
for i in range(10):
    def mutate(saved):
        time.sleep(0.003)
        saved[label] = saved.get(label, 0) + 1
        return saved
    tool.update_saved_settings(mutate)
    with contextlib.redirect_stdout(io.StringIO()):
        tool.probe_cli(["enable", label + "-" + str(i) + ".example"])
'''
        workers = [subprocess.Popen([sys.executable, "-c", script, str(Path(tool.__file__).parent),
                                     str(self.path), label], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True) for label in ("first", "second")]
        try:
            for worker in workers:
                out, err = worker.communicate(timeout=20)
                self.assertEqual(worker.returncode, 0, (out + err)[-1500:])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
        actual = self.saved()
        self.assertEqual((actual["first"], actual["second"]), (10, 10))
        self.assertEqual(len(actual["online_probe_hosts"]), 20)
        self.assertEqual(actual["unknown"], {"preserve": True})

    def test_save_settings_keeps_replacement_contract_and_uses_atomic_replace(self):
        self.write({"old": True})
        replacement = {"new": {"keep": True}}
        self.assertIsNone(tool.save_settings(replacement))
        self.assertEqual(self.saved(), replacement)

    def test_cli_mutations_read_latest_raw_fields_inside_transaction(self):
        self.write({"online_probe_hosts": {"alpha.example": True}})
        update = tool.update_saved_settings
        def interleave(mutator):
            update(lambda saved: dict(saved, online_probe_hosts=dict(
                saved["online_probe_hosts"], **{"beta.example": True}), unknown="latest"))
            return update(mutator)
        with patch.object(tool, "update_saved_settings", side_effect=interleave), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.probe_cli(["disable", "alpha.example"]), 0)
        self.assertEqual(self.saved()["online_probe_hosts"], {"alpha.example": False, "beta.example": True})
        self.assertEqual(self.saved()["unknown"], "latest")

    def test_menu_connection_update_cannot_restore_stale_consent(self):
        self.write({"model_probe_enabled": False, "online_probe_hosts": {"alpha.example": False},
                    "unknown": "latest"})
        stale = dict(tool.DEFAULTS, model_probe_enabled=True, online_probe_hosts={"alpha.example": True})
        with patch("builtins.input", side_effect=["https://admin.example", ""]), \
             contextlib.redirect_stdout(io.StringIO()):
            tool.act_settings(stale)
        self.assertEqual(self.saved(), {"model_probe_enabled": False,
                                        "online_probe_hosts": {"alpha.example": False},
                                        "unknown": "latest", "sub2api_base_url": "https://admin.example"})

    def test_one_click_connection_update_preserves_live_consent_and_environment_secrets(self):
        spec = importlib.util.spec_from_file_location("settings_test_one_click", Path(tool.__file__).with_name("一键导入.py"))
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        self.write({"model_probe_enabled": False, "online_probe_hosts": {"alpha.example": False},
                    "unknown": "latest"})
        stale = dict(tool.DEFAULTS, model_probe_enabled=True, online_probe_hosts={"alpha.example": True},
                     sub2api_admin_key="environment-only-secret")
        with patch.object(entry, "ask_line", side_effect=["https://admin.example/", ""]), \
             contextlib.redirect_stdout(io.StringIO()):
            entry.act_setkey(stale)
        self.assertEqual(self.saved(), {"model_probe_enabled": False,
                                        "online_probe_hosts": {"alpha.example": False},
                                        "unknown": "latest", "sub2api_base_url": "https://admin.example"})

    def test_environment_true_cannot_restore_explicit_saved_master_off(self):
        self.write({"model_probe_enabled": False, "online_probe_hosts": {"alpha.example": True}})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example"}):
            self.assertFalse(tool.load_settings()["model_probe_enabled"])

    def test_environment_false_default_does_not_override_saved_master_on(self):
        self.write({"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false"}):
            self.assertTrue(tool.load_settings()["model_probe_enabled"])

    def test_hard_disabled_blocks_saved_master_on_without_erasing_consent(self):
        self.write({"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false", "MODEL_PROBE_HARD_DISABLED": "true"}):
            effective = tool.load_settings()
            self.assertFalse(effective["model_probe_enabled"])
            self.assertTrue(effective["model_probe_hard_disabled"])
            self.assertFalse(tool.probe_policy.probe_allowed("alpha.example", effective))
            self.assertTrue(self.saved()["model_probe_enabled"])

    def test_environment_false_is_default_when_master_has_not_been_saved(self):
        self.write({"unknown": "preserve"})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false"}):
            self.assertFalse(tool.load_settings()["model_probe_enabled"])

    def test_environment_optin_still_works_without_saved_master(self):
        self.write({"unknown": "preserve"})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example"}):
            self.assertTrue(tool.probe_policy.probe_allowed("alpha.example", tool.load_settings()))

    def test_environment_whitelist_limits_saved_on_and_respects_saved_off(self):
        self.write({"model_probe_enabled": True, "online_probe_hosts": {
            "alpha.example": False, "beta.example": True}})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example,gamma.example"}):
            actual = tool.load_settings()
            self.assertFalse(tool.probe_policy.probe_allowed("alpha.example", actual))
            self.assertFalse(tool.probe_policy.probe_allowed("beta.example", actual))
            self.assertTrue(tool.probe_policy.probe_allowed("gamma.example", actual))

    def test_stale_live_config_is_rechecked_after_saved_revocation(self):
        stale = {"_live_probe_policy": True, "model_probe_enabled": True,
                 "online_probe_hosts": {"alpha.example": True}}
        self.write({"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example"}):
            self.assertTrue(tool.probe_policy.probe_allowed("alpha.example", tool.resolve_probe_policy(stale)))
            tool.update_saved_settings(lambda saved: dict(saved, online_probe_hosts={"alpha.example": False}))
            self.assertFalse(tool.probe_policy.probe_allowed("alpha.example", tool.resolve_probe_policy(stale)))

    def test_malformed_saved_consent_fails_closed_even_with_environment_optin(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example"}), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tool.probe_policy.probe_allowed("alpha.example", tool.load_settings()))


if __name__ == "__main__":
    unittest.main()
