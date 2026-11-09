"""Automatic model probes share a persisted host week with native schedules."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from ._test_compat import CompatTestCase
else:
    from _test_compat import CompatTestCase

import automatic_probe_budget as budget
import model_probe


class ModelWeeklyDueTests(CompatTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 2_000_000_000.0
        self.settings = {"model_probe_enabled": True, "online_probe_hosts": {"fixture.example": True},
            "request_relay_state_dir": str(self.root / "state")}
        self.unit = {"section": "openai-compatibility", "host": "fixture.example", "platform": "openai",
            "base_url": "https://fixture.example/v1", "api_key": "fixture-only", "headers": {}, "family": "gpt",
            "latest_gen": 6, "prev_gen": 5, "latest_model": "gpt-6", "prev_model": "gpt-5", "provenance": "fixture-provenance"}
        self.calls = []
        self.responses = [(200, '{"choices":[{"message":{"content":"5"}}]}')]
        self.enterContext(patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "false"}))
        self.enterContext(patch.object(model_probe.time, "time", side_effect=lambda: self.now))

    def http(self, method, url, headers, body, **kwargs):
        self.calls.append(body["model"])
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    def run_probe(self, units=None, force=False):
        return model_probe.probe_all(units or [self.unit], self.http, out_dir=str(self.root / "out"),
            settings=self.settings, ttl=1, automatic=True, force=force)

    def test_success_failure_and_exception_never_repeat_automatically_before_seven_days(self):
        for responses in ([(200, '{"choices":[{}]}')], [(503, '{}'), (503, '{}')]):
            with self.subTest(responses=responses):
                other = self.root / str(len(self.calls))
                self.settings["request_relay_state_dir"] = str(other / "state")
                self.responses = responses
                before = len(self.calls)
                model_probe.probe_all([self.unit], self.http, out_dir=str(other / "out"), settings=self.settings,
                    ttl=1, automatic=True)
                first = len(self.calls)
                self.assertGreater(first, before)
                self.now += 3600
                model_probe.probe_all([self.unit], self.http, out_dir=str(other / "out"), settings=self.settings,
                    ttl=1, automatic=True)
                self.assertEqual(len(self.calls), first)

    def test_prev_success_forced_fresh_branch_and_corrupt_cache_cannot_bypass_week(self):
        self.responses = [(503, '{}'), (200, '{"choices":[{"message":{"content":"5"}}]}')]
        first = self.run_probe()
        self.assertTrue(first[(self.unit["section"], self.unit["host"], "gpt")]["prev"])
        self.assertEqual(self.calls, ["gpt-6", "gpt-5"])
        cache = self.root / "out" / "model-probe-cache.json"
        cache.write_text('{broken}', encoding="utf8")
        self.now += 3600
        self.assertEqual(self.run_probe(), {})
        self.assertEqual(self.calls, ["gpt-6", "gpt-5"])
        changed = dict(self.unit, latest_model="gpt-7", latest_gen=7, provenance="new-key-model-scope")
        self.assertEqual(self.run_probe([changed]), {})
        self.assertEqual(len(self.calls), 2)

    def test_direct_import_attempt_blocks_another_native_source_at_same_host(self):
        self.run_probe()
        with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_not_due"):
            budget.claim_batch(self.settings["request_relay_state_dir"], "https://FIXTURE.example.:443/another",
                [{"source_id": "a" * 40, "model": "gpt-other", "generation": "b" * 64, "limit": 1}],
                lambda: None, now=self.now + 3 * 86400)

    def test_explicit_force_is_manual_and_does_not_bypass_hard_or_permanent_rules(self):
        self.run_probe()
        count = len(self.calls)
        self.run_probe(force=True)
        self.assertGreater(len(self.calls), count)
        count = len(self.calls)
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            self.assertEqual(self.run_probe(force=True), {})
        with patch.object(model_probe.probe_policy, "permanently_denied", return_value=True):
            self.assertEqual(self.run_probe(force=True), {})
        self.assertEqual(len(self.calls), count)

    def test_saved_force_key_does_not_authorize_another_automatic_job(self):
        self.run_probe()
        count = len(self.calls)
        self.settings.update(force=True, model_probe_force=True, _automatic_probe={"ticket": "invented"})
        self.now += 3600
        self.assertEqual(self.run_probe(), {})
        self.assertEqual(len(self.calls), count)

    def test_relay_batch_sends_private_ticket_and_native_side_consumes_it(self):
        unit = dict(self.unit, request_base_url="http://relay.invalid/r/" + "a" * 40,
            request_api_key="fixture-route-token")
        self.settings["request_relay"] = {"generation": "b" * 64}
        calls = []
        def relay_http(method, url, headers, body, **kwargs):
            ticket = headers[budget.HEADER]
            self.assertNotEqual(ticket, "invented")
            budget.consume_ticket(self.settings["request_relay_state_dir"], unit["base_url"], ticket,
                "a" * 40, body["model"], "b" * 64, lambda: None, now=self.now)
            calls.append(body["model"])
            return 200, '{"choices":[{"message":{"content":"5"}}]}'
        model_probe.probe_all([unit], relay_http, out_dir=str(self.root / "out"), settings=self.settings,
            ttl=1, automatic=True)
        self.assertEqual(calls, ["gpt-6"])

    def test_explicit_local_auto_skip_never_creates_failure_or_lower_release_proof(self):
        def skip(*args, **kwargs):
            return 400, '{"error":"automatic_probe_unsafe_profile"}'
        result = model_probe.probe_all([self.unit], skip, out_dir=str(self.root / "out"), settings=self.settings,
            automatic=True)
        self.assertEqual(result, {})
        cache = self.root / "out" / "model-probe-cache.json"
        if cache.exists():
            self.assertEqual(json.loads(cache.read_text())["entries"], {})


if __name__ == "__main__":
    unittest.main()
