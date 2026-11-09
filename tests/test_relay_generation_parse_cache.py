"""Reuse verified parsing without weakening immutable bytes or secret checks."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import relay.config as config


class GenerationParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = {"openai-compatibility": [{"name": "fixture", "base-url": "https://fixture.invalid",
            "api-key-entries": [{"api-key": "fixture-only"}], "models": [{"name": "gpt-fixture"}]}]}
        self.records = [{"source_section": "openai-compatibility", "section": "openai-compatibility",
            "group": "fixture", "base_url": "https://fixture.invalid", "api_key": "fixture-only"}]
        self.bundle = config.export_bundle(self.records, self.document, "fixture", self.root)

    def test_same_verified_generation_is_decoded_once_and_returned_as_independent_objects(self):
        original = config.yaml.safe_load
        with patch.object(config.yaml, "safe_load", wraps=original) as decode:
            first = config.load_bundle(self.root)
            second = config.load_bundle(self.root)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(first, second)
        first["consumer_config"]["access"]["api-keys"] = ["caller-corrupted"]
        first["routes"].clear()
        third = config.load_bundle(self.root)
        self.assertEqual(third["consumer_config"], second["consumer_config"])
        self.assertEqual(third["routes"], second["routes"])

    def test_same_size_same_mtime_tamper_is_rejected_after_warming_cache(self):
        config.load_bundle(self.root)
        path = self.root / "generations" / self.bundle["generation"] / "consumer.yaml"
        before, stat = path.read_bytes(), path.stat()
        after = before.replace(b"fixture.invalid", b"changed.invalid")
        self.assertNotEqual(after, before)
        self.assertEqual(len(after), len(before))
        path.write_bytes(after)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, stat.st_mtime_ns)
        with self.assertRaisesRegex(config.RelayError, "generation_digest_mismatch"):
            config.load_bundle(self.root)

    def test_fresh_secret_bytes_are_checked_after_warming_cache(self):
        config.load_bundle(self.root)
        path = self.root / "secrets.json"
        before = path.read_bytes()
        token = self.bundle["state"]["consumer_token"]
        changed = token[:-1] + ("a" if token[-1] != "a" else "b")
        after = before.replace(token.encode(), changed.encode())
        self.assertEqual(len(before), len(after))
        path.write_bytes(after)
        with self.assertRaisesRegex(config.RelayError, "generation_secret_state_mismatch"):
            config.load_bundle(self.root)

    def test_current_pointer_and_manifest_are_always_rechecked(self):
        config.load_bundle(self.root)
        pointer = self.root / "CURRENT"
        pointer.write_text("a" * 64 + "\n", encoding="ascii")
        with self.assertRaises(config.RelayError):
            config.load_bundle(self.root)
        pointer.write_text(self.bundle["generation"] + "\n", encoding="ascii")
        manifest = self.root / "generations" / self.bundle["generation"] / "manifest.json"
        value = json.loads(manifest.read_text())
        value["consumer_sha256"] = "a" * 64
        manifest.write_bytes(config.canonical_json(value))
        with self.assertRaisesRegex(config.RelayError, "generation_digest_mismatch"):
            config.load_bundle(self.root)

    def test_valid_changed_generation_never_returns_the_old_cached_bundle(self):
        before = config.load_bundle(self.root)
        changed = copy.deepcopy(self.document)
        changed["payload"] = {"override": [{"models": [{"name": "gpt-fixture"}], "params": {"max_tokens": 3}}]}
        exported = config.export_bundle(self.records, changed, "fixture", self.root)
        after = config.load_bundle(self.root)
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertEqual(after["generation"], exported["generation"])
        self.assertEqual(after["consumer_config"]["payload"], changed["payload"])


if __name__ == "__main__":
    unittest.main()
