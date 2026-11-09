"""The explicit manual node mode must not schedule provider/group speed tests."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml


class MihomoManualModeTests(unittest.TestCase):
    def apply_policy(self, setting, text):
        source = (Path(__file__).resolve().parents[1] / 'mihomo-manager/bootstrap-mihomo.sh').read_text(encoding='utf8')
        match = re.search(r"python3 - \"\$CONFIG\" <<'PY_POLICY'\n(.*?)\nPY_POLICY", source, re.S)
        self.assertIsNotNone(match, 'bootstrap must apply the explicit automatic node test policy')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text(text, encoding='utf8')
            env = dict(os.environ)
            if setting is None:
                env.pop('MIHOMO_AUTOMATIC_TESTS', None)
            else:
                env['MIHOMO_AUTOMATIC_TESTS'] = setting
            result = subprocess.run([sys.executable, '-', str(path)], input=match.group(1),
                                    encoding='utf8', env=env, capture_output=True)
            return result.returncode, path.read_text(encoding='utf8')

    def test_explicit_off_disables_provider_and_automatic_group_tests(self):
        raw = ('secret: synthetic-secret\nproxy-providers:\n  saved:\n    type: file\n    path: ./saved.body\n'
               '    health-check: {enable: true, url: https://fixture.invalid/204, interval: 300}\n'
               'proxy-groups:\n- {name: AUTO, type: url-test, use: [saved], url: https://fixture.invalid/204, interval: 300, tolerance: 50}\n'
               'rules: [MATCH,AUTO]\n')
        code, text = self.apply_policy('false', raw)
        self.assertEqual(code, 0)
        cfg = yaml.safe_load(text)
        self.assertFalse(cfg['proxy-providers']['saved']['health-check']['enable'])
        self.assertEqual(cfg['proxy-groups'][0]['type'], 'select')
        self.assertNotIn('url', cfg['proxy-groups'][0])
        self.assertNotIn('interval', cfg['proxy-groups'][0])
        self.assertEqual(cfg['proxy-groups'][0]['use'], ['saved'])
        self.assertEqual(cfg['secret'], 'synthetic-secret')

    def test_default_and_explicit_on_preserve_existing_configuration_bytes(self):
        raw = '# retain original comments\nproxy-groups: []\n'
        for setting in (None, 'true'):
            with self.subTest(setting=setting):
                code, text = self.apply_policy(setting, raw)
                self.assertEqual((code, text), (0, raw))

    def test_invalid_setting_fails_without_overwriting_config(self):
        raw = '# no mutation on invalid setting\nproxy-groups: []\n'
        code, text = self.apply_policy('maybe', raw)
        self.assertNotEqual(code, 0)
        self.assertEqual(text, raw)

    def test_off_converts_fallback_and_load_balance_without_losing_routes(self):
        raw = ('dns: {enable: false}\nsecret: synthetic-secret\n'
               'proxies: [{name: saved-node, type: direct}]\n'
               'proxy-groups:\n'
               '- {name: FALLBACK, type: fallback, proxies: [saved-node], url: https://fixture.invalid/204, interval: 300, lazy: true}\n'
               '- {name: BALANCED, type: load-balance, proxies: [saved-node], strategy: consistent-hashing, url: https://fixture.invalid/204, interval: 300}\n'
               'rules: [MATCH,FALLBACK]\n')
        code, text = self.apply_policy('off', raw)
        self.assertEqual(code, 0)
        cfg, original = yaml.safe_load(text), yaml.safe_load(raw)
        self.assertEqual([g['type'] for g in cfg['proxy-groups']], ['select', 'select'])
        for group in cfg['proxy-groups']:
            self.assertEqual(group['proxies'], ['saved-node'])
            for removed in ('url', 'interval', 'strategy', 'lazy'):
                self.assertNotIn(removed, group)
        for name in ('dns', 'secret', 'proxies', 'rules'):
            self.assertEqual(cfg[name], original[name])

    def test_invalid_yaml_or_structure_leaves_original_bytes(self):
        for raw in ('proxy-groups: [\n', 'proxy-groups: wrong-type\n',
                    'proxy-providers: {saved: {health-check: true}}\n',
                    'proxy-providers: []\n', 'proxy-providers: false\n', 'proxy-providers: 0\n',
                    'proxy-groups: {}\n', 'proxy-groups: false\n', 'proxy-groups: 0\n'):
            with self.subTest(raw=raw):
                code, text = self.apply_policy('false', raw)
                self.assertNotEqual(code, 0)
                self.assertEqual(text, raw)

    def test_off_is_idempotent_and_preserves_already_manual_bytes(self):
        raw = '# manual config stays byte-identical\nproxy-groups: [{name: ONLY, type: select, proxies: [DIRECT]}]\n'
        self.assertEqual(self.apply_policy('false', raw), (0, raw))
        active = 'proxy-groups: [{name: AUTO, type: url-test, proxies: [DIRECT], interval: 30}]\n'
        code, changed = self.apply_policy('false', active)
        self.assertEqual(code, 0)
        self.assertEqual(self.apply_policy('false', changed), (0, changed))


if __name__ == '__main__':
    unittest.main()
