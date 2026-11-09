"""Consumer restarts use fresh OAuth state while immutable generations survive."""
import hashlib
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import request_relay as relay
from relay.server import RelayRuntime
from relay.server import GUARD_FEATURE, DIAGNOSTIC_PREFIX_ENV, GUARD_QUERY_ENV


class ConsumerRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_generation_restarts_with_private_empty_auth_and_preserves_legacy_state(self):
        raw = {"codex-api-key": [{"api-key": "synthetic-restart-key",
                "base-url": "http://127.0.0.1:9",
                "models": [{"name": "gpt-restart", "alias": "gpt-restart"}]}]}
        record = {"section": "codex-api-key", "source_section": "codex-api-key",
                  "group": "codex", "base_url": "http://127.0.0.1:9",
                  "api_key": "synthetic-restart-key"}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            relay.export_bundle([record], raw, "restart-fixture", root, consumer_port=port)
            bundle = relay.load_bundle(root)
            generation = Path(root) / "generations" / bundle["generation"]
            legacy = generation / "consumer-home" / "empty-auth"
            legacy.mkdir(parents=True)
            (legacy / "kv.db").write_bytes(b"synthetic-bbolt-runtime-state")
            (legacy / "oauth.json").write_text('{"token":"legacy-synthetic-never-inherit"}', encoding="utf-8")
            protected = [Path(root) / "secrets.json", Path(root) / "CURRENT",
                         generation / "consumer.yaml", generation / "routes.json",
                         generation / "transports.json", generation / "manifest.json",
                         legacy / "kv.db", legacy / "oauth.json"]
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
            homes = []

            class Child:
                returncode = None

                def terminate(self):
                    self.returncode = 0

                async def wait(self):
                    return self.returncode

            async def start_consumer(*arguments, **options):
                home = Path(options["cwd"])
                self.assertEqual(options["env"]["HOME"], str(home))
                self.assertEqual(list((home / "empty-auth").iterdir()), [])
                self.assertEqual(arguments[2], bundle["consumer_path"])
                self.assertEqual(json.loads(options["env"][DIAGNOSTIC_PREFIX_ENV]),
                                 [r["diagnostic_prefix"] for r in bundle["routes"].values()])
                self.assertNotIn(GUARD_QUERY_ENV, options["env"])
                if os.name != "nt":
                    self.assertEqual(home.stat().st_mode & 0o777, 0o700)
                    self.assertEqual((home / "empty-auth").stat().st_mode & 0o777, 0o700)
                homes.append(home)
                (home / "empty-auth" / "kv.db").write_bytes(b"consumer-created-bbolt-fixture")
                return Child()

            runtime = RelayRuntime(root, "synthetic-consumer")
            try:
                with mock.patch("relay.server.asyncio.create_subprocess_exec", side_effect=start_consumer), \
                        mock.patch.object(runtime, "_registry_ready", new=mock.AsyncMock(return_value=True)), \
                        mock.patch("relay.server.verify_consumer_guard", return_value={"feature_id": GUARD_FEATURE}), \
                        mock.patch.dict(os.environ, {DIAGNOSTIC_PREFIX_ENV: '["untrusted"]', GUARD_QUERY_ENV: '1'}):
                    await runtime._activate(bundle)
                    self.assertTrue(runtime.accepting)
                    await runtime._activate(bundle)
                    self.assertTrue(runtime.accepting)
                self.assertEqual(len(homes), 2)
                self.assertNotEqual(homes[0], homes[1])
                self.assertTrue(all(home != legacy.parent for home in homes))
                self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected})
                self.assertEqual(relay.load_bundle(root)["generation"], bundle["generation"])
            finally:
                await runtime.close()


if __name__ == "__main__":
    unittest.main()
