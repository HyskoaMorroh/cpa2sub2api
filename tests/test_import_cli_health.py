"""Executing imports honors configured health ranking; previews remain offline."""
import contextlib
import copy
import io
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool


class ImportCliHealthTests(unittest.TestCase):
    def test_execute_uses_live_ranking_preference_without_implicit_probes(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                settings = dict(tool.DEFAULTS, health_rerank_enabled=enabled, model_probe_enabled=True)
                plan = {'config': {'source_scope': 'default', 'health_rerank_enabled': False},
                        'accounts_raw': [{'base_url': 'https://source.example'}]}
                def build(config):
                    self.assertFalse(config['model_probe_enabled'])
                    self.assertFalse(config['health_rerank_enabled'])
                    return copy.deepcopy(plan), '', 'synthetic', []
                received = []
                def execute(api, actual, **kwargs):
                    received.append(actual['config']['health_rerank_enabled'])
                    return {}
                with patch.object(tool, 'load_settings', return_value=settings), \
                     patch.object(tool, 'build_plan', side_effect=build), \
                     patch.object(tool, 'Sub2Api'), patch.object(tool, 'do_import', side_effect=execute), \
                     patch.object(tool, 'print_import_summary', return_value=0), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.import_cli(['--yes']), 0)
                self.assertEqual(received, [enabled])

    def test_preview_never_constructs_the_admin_client_or_runs_import(self):
        plan = {'config': {'source_scope': 'default', 'health_rerank_enabled': False},
                'accounts_raw': [{'base_url': 'https://source.example'}]}
        with patch.object(tool, 'load_settings', return_value=dict(tool.DEFAULTS, health_rerank_enabled=True)), \
             patch.object(tool, 'build_plan', return_value=(plan, '', 'synthetic', [])), \
             patch.object(tool, 'Sub2Api', side_effect=AssertionError('preview management request')), \
             patch.object(tool, 'do_import', side_effect=AssertionError('preview import')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.import_cli(['--preview']), 0)


if __name__ == '__main__':
    unittest.main()
