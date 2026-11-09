"""TLS source comparison is explicit and never writes account TLS flags."""
import contextlib
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool
import request_relay
import model_probe
import tls_diagnostic_transaction as tls

CONSENT = {"model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}
GOOD = '{"choices":[{"message":{"content":"5"}}]}'
BAD = 'data: {"type":"error","success":false}\n\n'
PROVENANCE = {"feature_id": "cpa2sub2api-diagnostic-no-redirect-v1",
              "source_commit": "91e0dee827abf476f9f86246f68c500a112f7155",
              "binary_sha256": "a" * 64, "automatic_output_cap": 16,
              "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}


class Api:
    def __init__(self, account):
        self.row = copy.deepcopy(account)
        self.base, self.key, self.user_agent, self.timeout = "http://admin.invalid", "test", "test", 1
        self.writes = []
        self.fail_next_read = False

    def _call(self, method, path):
        assert (method, path) == ("GET", "/api/v1/admin/accounts/7")
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("synthetic transient read error")
        return copy.deepcopy(self.row)

    def update_account(self, aid, body):
        self.writes.append(copy.deepcopy(body))
        self.row.update(copy.deepcopy(body))


class SourceFixture:
    """Full source/state transaction with only the model response simulated."""
    def __init__(self):
        self.stack = contextlib.ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = pathlib.Path(self.directory) / "relay"
        self.settings_path = pathlib.Path(self.directory) / "settings.json"
        self.document = {"openai-compatibility": [{"name": "fixture",
            "base-url": "https://upstream.example",
            "api-key-entries": [{"api-key": "synthetic-source-key"}],
            "models": [{"name": "gpt-9"}]}]}
        self.settings = dict(tool.DEFAULTS, **CONSENT, source_scope="fixture",
            request_relay_source_scope="fixture", request_relay_enabled=True,
            request_relay_force_all_accounts=True, request_relay_weighted_bucket=False,
            request_relay_state_dir=str(self.root), request_relay_base_url="http://relay.invalid:8380")
        self.records, _ = tool.collect(self.document)
        for record in self.records:
            record["_source_scope"] = "fixture"
        tool.assign_names(self.records)
        tool.remap_priority(self.records)
        self.reference = tool._export_request_relay(self.records, self.document, self.settings)
        self.bundle = request_relay.load_bundle(self.root)
        self.settings["request_relay"] = self.reference
        account = tool.to_account(self.records[0],
            dict(self.settings, _request_relay_bundle=self.bundle),
            {self.records[0]["group"]: 1}, {}, {})
        account.update(id=7, status="active", schedulable=True)
        account["extra"]["runtime_health"] = "preserve"
        self.api = Api(account)
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf8")
        self.before = tls.state_snapshot(self.root, self.settings_path)
        self.original_account = copy.deepcopy(self.api.row)
        self.stack.enter_context(patch.object(tool, "SETTINGS_PATH", str(self.settings_path)))
        self.stack.enter_context(patch.object(tool, "fetch_cpa_text",
            return_value=(tool._yaml().safe_dump(self.document), "synthetic source")))
        self.stack.enter_context(patch.object(tool, "_tls_consumer_provenance", return_value=PROVENANCE))
        self.stack.enter_context(patch.object(request_relay, "verify_runtime",
            return_value={"ready": True, "diagnostic_guard": PROVENANCE["feature_id"],
                "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}))

    def close(self):
        self.stack.close()

    def diagnose(self, outcomes, action=None):
        calls = []
        pending = list(outcomes)
        def run(driver, record, source, model, profile, settings, validate):
            validate()
            calls.append(copy.deepcopy(profile))
            if action:
                action(len(calls))
            status, body = pending.pop(0)
            return status == 200 and model_probe._valid_model_output(body, "openai")
        with patch.object(tls.NativeDiagnosticDriver, "run", new=run), \
             patch.object(tool, "_http", side_effect=AssertionError("external HTTP forbidden")):
            result = tool.diagnose_tls_fingerprint(self.api, [7], self.settings)
        return result, calls


class TlsDiagnosticsTests(unittest.TestCase):
    def fixture(self):
        fixture = SourceFixture()
        self.addCleanup(fixture.close)
        return fixture

    def test_no_consent_or_manual_cooldown_never_sends_or_changes(self):
        for cfg, delta in (({}, {}), (CONSENT, {"status": "inactive"}),
                           (CONSENT, {"rate_limit_reset_at": "future"}),
                           (CONSENT, {"schedulable": False})):
            fixture = self.fixture()
            fixture.api.row.update(delta)
            settings = dict(fixture.settings, **cfg)
            if not cfg:
                settings.update(model_probe_enabled=False, online_probe_hosts={})
            with patch.object(tls.NativeDiagnosticDriver, "run", side_effect=AssertionError("model request")):
                self.assertEqual(tool.diagnose_tls_fingerprint(fixture.api, [7], settings), (set(), []))
            self.assertEqual(fixture.api.writes, [])
            self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)

    def test_successful_protocol_comparison_retains_only_verified_tls(self):
        fixture = self.fixture()
        (learned, failures), calls = fixture.diagnose([(200, BAD), (200, GOOD)])
        self.assertEqual(learned, {"upstream.example"})
        self.assertFalse(failures)
        self.assertEqual(calls, [None, {"impersonate": "chrome136"}])
        self.assertEqual(fixture.api.row, fixture.original_account)
        self.assertEqual(fixture.api.row["extra"]["runtime_health"], "preserve")
        self.assertEqual(fixture.api.row["status"], "active")
        self.assertEqual(fixture.api.writes, [])
        current = request_relay.load_bundle(fixture.root)
        self.assertEqual(current["state"]["route_tokens"], fixture.bundle["state"]["route_tokens"])
        self.assertEqual(set(current["routes"]), set(fixture.bundle["routes"]))
        saved = json.loads(fixture.settings_path.read_text())
        route = next(iter(current["routes"].values()))
        self.assertEqual(saved[tls.LEARNED_KEY][route["id"]]["profile"], {"impersonate": "chrome136"})

    def test_failed_protocol_or_html_restores_extra(self):
        fixture = self.fixture()
        (learned, failures), calls = fixture.diagnose([(200, BAD), (200, "<html>login</html>")])
        self.assertFalse(learned)
        self.assertTrue(failures)
        self.assertEqual(len(calls), 2)
        self.assertEqual(fixture.api.row["extra"], fixture.original_account["extra"])
        self.assertEqual(fixture.api.writes, [])
        self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)

    def test_transient_readback_failure_after_tls_write_still_rolls_back(self):
        fixture = self.fixture()
        def fail_read(call):
            if call == 2:
                fixture.api.fail_next_read = True
        (learned, failures), calls = fixture.diagnose([(200, BAD), (200, GOOD)], fail_read)
        self.assertFalse(learned)
        self.assertTrue(failures)
        self.assertEqual(len(calls), 2)
        self.assertEqual(fixture.api.row["extra"], fixture.original_account["extra"])
        self.assertEqual(fixture.api.writes, [])
        self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)


if __name__ == "__main__":
    unittest.main()
