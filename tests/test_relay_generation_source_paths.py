"""Cold and cached generations cannot follow links or share path identity."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import relay.config as config


class GenerationSourcePathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "state"
        self.document = {"openai-compatibility": [{"name": "fixture", "base-url": "https://fixture.invalid",
            "api-key-entries": [{"api-key": "fixture-only"}], "models": [{"name": "gpt-fixture"}]}]}
        self.records = [{"source_section": "openai-compatibility", "section": "openai-compatibility",
            "group": "fixture", "base_url": "https://fixture.invalid", "api_key": "fixture-only"}]
        self.bundle = config.export_bundle(self.records, self.document, "fixture", self.root)
        self.generation = self.root / "generations" / self.bundle["generation"]

    def replace_with_link(self, path, outside):
        data = path.read_bytes()
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(data)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as error:
            path.write_bytes(data)
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows file symlink privilege unavailable; mandatory Linux image coverage remains")
            raise
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return data

    def test_cold_and_warm_same_bytes_symlinks_are_rejected_for_every_source_file(self):
        for warmed in (False, True):
            for relative in ("CURRENT", "secrets.json", "generations/" + self.bundle["generation"] + "/manifest.json",
                    "generations/" + self.bundle["generation"] + "/consumer.yaml",
                    "generations/" + self.bundle["generation"] + "/routes.json",
                    "generations/" + self.bundle["generation"] + "/transports.json"):
                with self.subTest(warmed=warmed, relative=relative):
                    if warmed:
                        config.load_bundle(self.root)
                    path = self.root / relative
                    data = self.replace_with_link(path, self.base / "outside" / (path.name + str(warmed)))
                    try:
                        with self.assertRaises(config.RelayError):
                            config.load_bundle(self.root)
                    finally:
                        path.unlink()
                        path.write_bytes(data)

    def test_same_bytes_generation_directory_symlink_is_rejected_after_cache_warm(self):
        config.load_bundle(self.root)
        outside = self.base / "outside-generation"
        self.generation.rename(outside)
        try:
            self.generation.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            outside.rename(self.generation)
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows directory symlink privilege unavailable; mandatory Linux image coverage remains")
            raise
        with self.assertRaises(config.RelayError):
            config.load_bundle(self.root)

    def test_root_directory_symlink_is_not_accepted_as_another_source_path(self):
        config.load_bundle(self.root)
        linked = self.base / "linked-state"
        try:
            linked.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows directory symlink privilege unavailable; mandatory Linux image coverage remains")
            raise
        with self.assertRaises(config.RelayError):
            config.load_bundle(linked)

    def test_identical_bytes_at_another_regular_root_have_independent_parse_identity(self):
        other = self.base / "another-state"
        shutil.copytree(self.root, other)
        original = config.yaml.safe_load
        with patch.object(config.yaml, "safe_load", wraps=original) as decode:
            first = config.load_bundle(self.root)
            second = config.load_bundle(other)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(first["consumer_config"], second["consumer_config"])
        self.assertEqual(first["routes"], second["routes"])
        self.assertNotEqual(first["consumer_path"], second["consumer_path"])
        self.assertEqual(Path(second["consumer_path"]).parent.parent.parent, other)


if __name__ == "__main__":
    unittest.main()
