"""Exact reviewed source representations do not waive unknown config/code drift."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tool
import upstream_contract as contract

FILE = 'internal/config/config_types.go'
PINNED = 'a234c8850ae30b8124f17613abdbe56f8bd0e3dd5e6cbc9ebc0e809c4f3ceb53'
REVIEWED = '0ba74d8a82bd80bef18cd693360283932e45a0ae767d1b71d581c736bd620ab6'


class ReviewedSourceEquivalenceTests(unittest.TestCase):
    def test_exact_reviewed_file_keeps_consumer_baseline_and_actual_source_version_distinct(self):
        info = contract.reviewed_source_equivalence('cpa', FILE, REVIEWED)
        self.assertEqual(info['baseline_sha256'], PINNED)
        self.assertEqual(info['source_tag'], 'v8.0.61')
        self.assertEqual(info['source_commit'], '681ce41b6e44c45b15b79378b003909d6ba29e68')
        self.assertEqual(contract.PINNED['cpa']['tag'], 'v8.0.60')
        self.assertTrue(info['comment_and_layout_only'])
        for kind, relative, digest in [('sub2api', FILE, REVIEWED), ('cpa', 'other.go', REVIEWED),
                ('cpa', FILE, 'f'*64)]:
            self.assertIsNone(contract.reviewed_source_equivalence(kind, relative, digest))

    def test_known_digest_allows_config_contract_but_records_equivalent_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/FILE
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture for the already verified exact digest')
            # Cryptographic hashing is not the subject here: feed the known
            # official-file digest to exercise both downstream contract gates.
            with patch.object(contract.hashlib, 'sha256') as hashed:
                hashed.return_value.hexdigest.return_value = REVIEWED
                evidence = contract.source_evidence(directory, 'cpa')
                unsupported, drift = tool.cpa_capability_issues({}, {'cpa_src':directory})
            self.assertEqual((unsupported, drift), ([], []))
            self.assertEqual(evidence['status'], 'verified_reviewed_equivalent')
            self.assertEqual(evidence['files'][FILE], REVIEWED)
            self.assertEqual(evidence['tag'], 'v8.0.60')
            self.assertEqual(evidence['source_file_equivalences'][FILE]['source_tag'], 'v8.0.61')

    def test_equivalent_source_does_not_accept_unknown_managed_configuration_field(self):
        document = {'codex-api-key':[{'api-key':'synthetic','base-url':'https://fixture.example',
                     'models':[{'name':'gpt-9'}], 'future-unadapted-setting':True}]}
        unsupported, _ = tool.cpa_capability_issues(document, {})
        self.assertEqual(unsupported, ['codex-api-key[0].future-unadapted-setting'])
        with self.assertRaisesRegex(ValueError, 'unadapted managed configuration fields'):
            tool.do_import(object(), {'unsupported_config_fields':unsupported})

    def test_unreviewed_code_directive_and_even_new_comment_bytes_remain_blocked(self):
        baseline = b'package config\nconst SafeBudget = 16\n'
        expected = hashlib.sha256(baseline).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/FILE
            path.parent.mkdir(parents=True)
            path.write_bytes(baseline)
            with patch.dict(contract.PINNED['cpa']['expected_hashes'], {FILE:expected}):
                self.assertEqual(contract.source_evidence(directory, 'cpa')['status'], 'verified_pinned')
                for changed in [b'package config\nconst SafeBudget = 4096\n',
                                b'//go:build future\n'+baseline, baseline+b'// unreviewed comment\n']:
                    path.write_bytes(changed)
                    evidence = contract.source_evidence(directory, 'cpa')
                    self.assertEqual(evidence['status'], 'review_required')
                    self.assertEqual(evidence['unverified_files'], [FILE])

    def test_registry_cannot_be_reused_after_consumer_contract_baseline_changes(self):
        with patch.dict(contract.PINNED['cpa']['expected_hashes'], {FILE:'a'*64}):
            self.assertIsNone(contract.reviewed_source_equivalence('cpa', FILE, REVIEWED))

    def test_saved_plan_cannot_consume_a_different_even_reviewed_source_representation(self):
        before = {'status':'verified_pinned','files':{FILE:PINNED}}
        after = {'status':'verified_reviewed_equivalent','files':{FILE:REVIEWED}}
        settings = {'cpa_src':'/fixture/cpa','sub2api_src':'/fixture/sub2api'}
        with patch.object(contract, 'source_evidence', return_value=before):
            plan = {'source_contract':tool._upstream_source_contract(settings)}
        with patch.object(contract, 'source_evidence', return_value=after):
            with self.assertRaisesRegex(ValueError, 'current cpa upstream contract drift'):
                tool._validate_upstream_source_contract(plan, settings)


if __name__ == '__main__':
    unittest.main()
