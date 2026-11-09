"""A single shared host budget covers automatic batches and native schedules."""
import concurrent.futures
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


class AutomaticBudgetTests(CompatTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.host = "fixture.example"
        self.now = 2_000_000_000.0
        self.binding = {"source_id": "a" * 40, "model": "gpt-fixture", "generation": "b" * 64, "limit": 1}
        self.enterContext(patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "false"}))
        budget.initialize_budget(self.root, lambda: None)

    def claim(self, bindings=None, now=None):
        return budget.claim_batch(self.root, self.host, bindings or [self.binding], lambda: None,
            now=self.now if now is None else now)

    def consume(self, ticket, now=None, **changed):
        value = dict(self.binding, **changed)
        return budget.consume_ticket(self.root, self.host, ticket, value["source_id"], value["model"],
            value["generation"], lambda: None, now=self.now if now is None else now)

    def test_one_batch_per_host_week_and_each_binding_has_a_finite_budget(self):
        ticket = self.claim()
        self.consume(ticket)
        with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_budget_exhausted"):
            self.consume(ticket)
        with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_not_due"):
            self.claim(now=self.now + 6 * 3600)
        next_ticket = self.claim(now=self.now + 604800)
        self.assertNotEqual(next_ticket, ticket)
        self.consume(next_ticket, now=self.now + 604800)

    def test_an_import_claim_also_blocks_a_native_plan_with_another_key_model_or_generation(self):
        ticket = self.claim()
        for changed in ({"source_id": "c" * 40}, {"model": "gpt-other"}, {"generation": "d" * 64}):
            with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_binding_changed"):
                self.consume(ticket, **changed)
        native_binding = dict(self.binding, source_id="c" * 40, model="gpt-other")
        with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_not_due"):
            self.claim([native_binding], now=self.now + 3 * 86400)

    def test_ticket_cannot_be_replayed_for_another_host_and_is_not_an_authorization(self):
        ticket = self.claim()
        with self.assertRaises(budget.AutomaticProbeSkipped):
            budget.consume_ticket(self.root, "another.example", ticket, self.binding["source_id"],
                self.binding["model"], self.binding["generation"], lambda: None, now=self.now)
        with patch.dict(os.environ, {"MODEL_PROBE_HARD_DISABLED": "true"}):
            with self.assertRaises(budget.AutomaticProbeSkipped):
                self.consume(ticket)
        with self.assertRaises(budget.AutomaticProbeSkipped):
            budget.consume_ticket(self.root, self.host, ticket, self.binding["source_id"], self.binding["model"],
                self.binding["generation"], lambda: False, now=self.now)
        self.consume(ticket)

    def test_simultaneous_claims_accept_only_one_and_do_not_share_tickets(self):
        def claim():
            try:
                return self.claim()
            except budget.AutomaticProbeSkipped:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(lambda _: claim(), range(24)))
        self.assertEqual(sum(ticket is not None for ticket in tickets), 1)

    def test_missing_corrupt_or_symlink_ledger_cannot_reset_an_initialized_budget(self):
        ticket = self.claim()
        ledger = self.root / budget.LEDGER_FILE
        original = ledger.read_bytes()
        for value in (None, b"{broken}"):
            if value is None:
                ledger.unlink()
            else:
                ledger.write_bytes(value)
            with self.assertRaises(budget.AutomaticProbeSkipped):
                budget.initialize_budget(self.root, lambda: None)
            with self.assertRaises(budget.AutomaticProbeSkipped):
                self.claim(now=self.now + 1)
            ledger.write_bytes(original)
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_bytes(original)
        ledger.unlink()
        ledger.symlink_to(outside)
        with self.assertRaises(budget.AutomaticProbeSkipped):
            self.consume(ticket)

    def test_legacy_cached_attempt_seeds_the_same_week_without_old_lower_success_proof(self):
        other = Path(self.temporary.name) / "legacy-state"
        history = Path(self.temporary.name) / "out"
        history.mkdir()
        (history / "model-probe-cache.json").write_text(json.dumps({"version": 1, "entries": {
            "openai-compatibility|fixture.example|6|5|family=gpt": {"latest": False, "prev": True, "ts": self.now}}}), encoding="utf8")
        budget.initialize_budget(other, lambda: None, history_dir=history)
        with self.assertRaisesRegex(budget.AutomaticProbeSkipped, "automatic_probe_not_due"):
            budget.claim_batch(other, self.host, [self.binding], lambda: None, now=self.now + 3600)

    def test_ledger_hides_host_model_and_keys_and_keeps_private_permissions(self):
        self.claim()
        content = (self.root / budget.LEDGER_FILE).read_text()
        self.assertNotIn(self.host, content)
        self.assertNotIn(self.binding["model"], content)
        self.assertIn(hashlib.sha256(self.host.encode()).hexdigest(), content)
        if os.name != "nt":
            self.assertEqual((self.root / budget.LEDGER_FILE).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
