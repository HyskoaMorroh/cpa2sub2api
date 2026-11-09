"""Version evidence, atomic source caches and conservative contract upgrades."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import tool

try:
    import upstream_contract as uc
except ImportError:
    uc = None


class UpstreamContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.text = "type Config struct {\n Pool *Pool `yaml:\"pool\"`\n}\ntype Pool struct {\n Enabled bool `yaml:\"enabled\"`\n}\n"
        self.spec = {"repo": "owner/project", "prefix": "", "tag": "v1.0.0",
                     "commit": "a" * 40, "files": ["config.go"]}

    def require_module(self):
        self.assertIsNotNone(uc, "versioned contract cache has not been implemented")
        return uc

    def fetch(self, method, url, **kwargs):
        self.assertIn("/" + self.spec["commit"] + "/", url)
        return 200, self.text

    def save(self, now=1000):
        return self.require_module().refresh_snapshot(self.root, "cpa", self.spec, self.fetch, now=now)

    def test_refresh_records_immutable_revision_time_and_each_file_digest(self):
        snapshot = self.save()
        self.assertEqual(snapshot["tag"], "v1.0.0")
        self.assertEqual(snapshot["commit"], "a" * 40)
        self.assertEqual(snapshot["extracted_at"], 1000)
        self.assertEqual(snapshot["files"]["config.go"], hashlib.sha256(self.text.encode()).hexdigest())
        self.assertTrue(os.path.isfile(os.path.join(snapshot["root"], "manifest.json")))

    def test_fresh_cache_is_read_without_fetch_and_expired_cache_refreshes(self):
        self.save()
        fn = unittest.mock.Mock(side_effect=self.fetch)
        cached, warnings = self.require_module().cached_or_refresh(
            self.root, "cpa", self.spec, fn, now=1010, ttl=60)
        self.assertTrue(cached)
        fn.assert_not_called()
        self.assertFalse(warnings)
        fresh, warnings = uc.cached_or_refresh(self.root, "cpa", self.spec, fn, now=1061, ttl=60)
        self.assertEqual(fn.call_count, 1)
        self.assertEqual(fresh["extracted_at"], 1061)

    def test_offline_expired_cache_is_labeled_stale_without_fetch_or_mutation(self):
        snapshot = self.save()
        manifest = os.path.join(snapshot["root"], "manifest.json")
        before = Path(manifest).read_bytes()
        fn = unittest.mock.Mock(side_effect=AssertionError("offline network"))
        cached, warnings = self.require_module().cached_or_refresh(
            self.root, "cpa", self.spec, fn, now=2000, ttl=60, offline=True)
        self.assertTrue(cached)
        self.assertTrue(any("stale" in warning for warning in warnings))
        self.assertEqual(Path(manifest).read_bytes(), before)
        fn.assert_not_called()

    def test_modified_source_and_future_timestamp_are_not_valid_cache(self):
        snapshot = self.save()
        with open(os.path.join(snapshot["root"], "config.go"), "w") as stream:
            stream.write(self.text + "// altered\n")
        self.assertIsNone(self.require_module().load_snapshot(self.root, "cpa", self.spec, now=1010))
        snapshot = self.save(now=2000)
        self.assertIsNone(uc.load_snapshot(self.root, "cpa", self.spec, now=1000))

    def test_partial_refresh_cannot_replace_previous_complete_generation(self):
        snapshot = self.save()
        before = Path(snapshot["root"], "manifest.json").read_bytes()
        failing = copy.deepcopy(self.spec)
        failing["files"] = ["config.go", "missing.go"]
        def fetch(method, url, **kwargs):
            return (404, "") if url.endswith("missing.go") else (200, self.text)
        with self.assertRaises(self.require_module().ContractError):
            uc.refresh_snapshot(self.root, "cpa", failing, fetch, now=1100)
        self.assertEqual(Path(snapshot["root"], "manifest.json").read_bytes(), before)

    def test_recursive_fields_report_nested_additions_and_type_changes(self):
        old = self.require_module().recursive_fields(self.text)
        new = uc.recursive_fields(self.text.replace("Enabled bool", "Enabled string").replace(
            " Enabled string", " Future int `yaml:\"future\"`\n Enabled string"))
        changes = uc.field_changes(old, new)
        self.assertIn({"path": "Config.pool.enabled", "kind": "type", "before": "bool", "after": "string"}, changes)
        self.assertTrue(any(change["path"] == "Config.pool.future" for change in changes))

    def test_array_and_pointer_structs_are_traversed_but_free_maps_remain_typed_leaves(self):
        text = self.text.replace("Pool *Pool", "Pool []Pool").replace(
            " Enabled bool", " Headers map[string]string `yaml:\"headers\"`\n Enabled bool")
        fields = self.require_module().recursive_fields(text)
        self.assertEqual(fields["Config.pool[].enabled"], "bool")
        self.assertEqual(fields["Config.pool[].headers"], "map[string]string")

    def test_candidate_does_not_replace_pinned_source_or_claim_unknown_semantics(self):
        baseline = self.save()
        old_bytes = Path(baseline["root"], "config.go").read_bytes()
        candidate = copy.deepcopy(self.spec)
        candidate.update(tag="v2.0.0", commit="b" * 40)
        updated = self.text.replace("Enabled bool", "Enabled string")
        snapshot = self.require_module().refresh_snapshot(self.root, "cpa", candidate,
            lambda *args, **kwargs: (200, updated), now=1100)
        result = uc.compare_snapshots(baseline, snapshot)
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["automatic_upgrade"])
        self.assertTrue(result["field_changes"])
        self.assertEqual(Path(baseline["root"], "config.go").read_bytes(), old_bytes)

    def test_known_pinned_snapshot_remains_identical_when_refresh_fails(self):
        baseline = self.save()
        fn = unittest.mock.Mock(return_value=(503, "failed"))
        cached, warnings = self.require_module().cached_or_refresh(
            self.root, "cpa", self.spec, fn, now=2000, ttl=60)
        self.assertEqual(cached["files"], baseline["files"])
        self.assertTrue(any("stale" in warning for warning in warnings))

    def test_capability_source_drift_blocks_import_before_management_writes(self):
        source = os.path.join(self.root, "internal", "config")
        os.makedirs(source)
        with open(os.path.join(source, "config_types.go"), "w") as stream:
            stream.write(self.text)
        document = {"claude-api-key": [{"api-key": "synthetic", "base-url": "https://site.example",
                    "models": [{"name": "claude-opus-9"}]}]}
        settings = dict(tool.DEFAULTS, cpa_src=self.root, health_rerank_enabled=False,
                        model_probe_enabled=False, request_relay_enabled=False)
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(document), "fixture")):
            plan, _, _, _ = tool.build_plan(settings)
        self.assertTrue(plan["capability_drift"])
        with patch.object(tool, "ensure_groups", side_effect=AssertionError("management write")), \
             patch.object(tool, "load_settings", return_value=settings), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "contract|capability"):
                tool.do_import(object(), plan)

    def test_preview_never_discovers_latest_or_refreshes_source_cache(self):
        self.require_module()
        settings = dict(tool.DEFAULTS, model_probe_enabled=False, health_rerank_enabled=False,
                        request_relay_enabled=False)
        with patch.object(tool, "load_settings", return_value=settings), \
             patch.object(tool, "fetch_cpa_text", return_value=("claude-api-key:\n  - api-key: synthetic\n    base-url: https://site.example\n    models: [{name: claude-opus-9}]\n", "fixture")), \
             patch.object(uc, "discover_latest", side_effect=AssertionError("preview network")), \
             patch.object(uc, "refresh_snapshot", side_effect=AssertionError("preview write")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.import_cli(["--preview"]), 0)

    def test_offline_source_fetch_never_networks_even_for_missing_legacy_cache(self):
        with patch.object(tool, "OUT_DIR", self.root), \
             patch.object(tool, "_http", side_effect=AssertionError("offline network")):
            sub, cpa, warnings = tool.fetch_upstream_src(dict(tool.DEFAULTS, fetch_upstream_src=False))
        self.assertIsNone(sub)
        self.assertIsNone(cpa)
        self.assertTrue(warnings)

    def test_source_changed_since_plan_creation_blocks_writes(self):
        settings = dict(tool.DEFAULTS, cpa_src=self.root, health_rerank_enabled=False,
                        model_probe_enabled=False, request_relay_enabled=False)
        document = "claude-api-key:\n- api-key: synthetic\n  base-url: https://site.example\n  models: [{name: claude-opus-9}]\n"
        # Missing local sources use the reviewed built-in contract during preview.
        settings["cpa_src"] = os.path.join(self.root, "source")
        with patch.object(tool, "fetch_cpa_text", return_value=(document, "fixture")):
            plan, _, _, _ = tool.build_plan(settings)
        self.assertFalse(plan["capability_drift"])
        os.makedirs(os.path.join(settings["cpa_src"], "internal", "config"))
        Path(settings["cpa_src"], "internal", "config", "config_types.go").write_text(self.text)
        with patch.object(tool, "load_settings", return_value=settings), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("write")):
            with self.assertRaisesRegex(ValueError, "current upstream contract drift"):
                tool.do_import(object(), plan)

    def test_stale_plan_contract_identity_is_rejected(self):
        snapshot = dict(tool.CPA_CAPABILITY_SNAPSHOT, consumer_commit="b" * 40)
        with self.assertRaisesRegex(ValueError, "stale capability"):
            tool.do_import(object(), {"capability_snapshot": snapshot})

    def test_invalid_manifest_shape_is_rejected_without_exception(self):
        snapshot = self.save()
        Path(snapshot["root"], "manifest.json").write_text("[]")
        self.assertIsNone(self.require_module().load_snapshot(self.root, "cpa", self.spec, now=1010))

    def test_latest_api_metadata_resolves_exact_release_commit(self):
        def metadata(method, url, **kwargs):
            return (200, json.dumps({"tag_name": "v9.0.0", "draft": False, "prerelease": False})) if url.endswith(
                "/releases/latest") else (200, json.dumps({"sha": "b" * 40}))
        release = self.require_module().discover_latest("cpa", metadata)
        self.assertEqual((release["tag"], release["commit"]), ("v9.0.0", "b" * 40))
        self.assertFalse(release["automatic_upgrade"])

    def test_rate_limited_api_falls_back_to_official_release_and_peeled_tag(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def geturl(self):
                return "https://github.com/router-for-me/CLIProxyAPI/releases/tag/v9.0.0"
        subprocess_result = unittest.mock.Mock(stdout=("a" * 40 + "\trefs/tags/v9.0.0\n" +
            "b" * 40 + "\trefs/tags/v9.0.0^{}\n"))
        with patch.object(self.require_module().urllib.request, "urlopen", return_value=Response()), \
             patch.object(uc.subprocess, "run", return_value=subprocess_result) as git:
            release = uc.discover_latest("cpa", lambda *a, **k: (403, "rate limited"))
        self.assertEqual(release["commit"], "b" * 40)
        self.assertIn("git_ref", release["metadata_source"])
        self.assertEqual(git.call_args.args[0][-2:], ["refs/tags/v9.0.0", "refs/tags/v9.0.0^{}"])

    def test_inspect_is_read_only_and_latest_requires_explicit_online_refresh(self):
        with patch.object(tool, "OUT_DIR", self.root), \
             patch.object(tool, "load_settings", return_value=dict(tool.DEFAULTS)), \
             patch.object(tool, "_http", side_effect=AssertionError("network")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.upstream_cli(["inspect"]), 0)
            self.assertEqual(os.listdir(self.root), [])
            with self.assertRaises(SystemExit):
                tool.upstream_cli(["refresh", "--latest", "--offline"])

    def test_explicit_latest_refresh_generates_review_candidate_without_activating_it(self):
        release = {"repo": self.spec["repo"], "tag": "v2.0.0", "commit": "b" * 40,
                   "checked_at": 1100, "automatic_upgrade": False}
        cache_root = os.path.join(self.root, "upstream-cache")
        baseline = self.require_module().refresh_snapshot(cache_root, "cpa", self.spec, self.fetch, now=1000)
        before = Path(baseline["root"], "config.go").read_bytes()
        with patch.dict(uc.PINNED, {"cpa": self.spec}), \
             patch.object(tool, "OUT_DIR", self.root), \
             patch.object(tool, "load_settings", return_value=dict(tool.DEFAULTS)), \
             patch.object(uc, "discover_latest", return_value=release), \
             patch.object(tool, "_http", return_value=(200, self.text.replace("Enabled bool", "Enabled string"))), \
             patch.object(uc.time, "time", return_value=1010), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.upstream_cli(["refresh", "--latest", "--kind", "cpa"]), 2)
        self.assertEqual(Path(baseline["root"], "config.go").read_bytes(), before)
        report = json.loads(Path(cache_root, "release-evidence.json").read_text())
        self.assertFalse(report["automatic_upgrade"])
        self.assertEqual(report["upstreams"]["cpa"]["comparison"]["status"], "review_required")

    def test_normal_online_check_uses_release_ttl_then_automatically_builds_new_candidate(self):
        self.require_module()
        baseline = uc.refresh_snapshot(self.root, "cpa", self.spec, self.fetch, now=1000)
        release = dict(repo=self.spec["repo"], tag=self.spec["tag"], commit=self.spec["commit"], checked_at=1010)
        discovery = unittest.mock.Mock(return_value=release)
        fn = unittest.mock.Mock(side_effect=self.fetch)
        with patch.dict(uc.PINNED, {"cpa": self.spec}), patch.object(uc, "discover_latest", discovery):
            report, code = uc.check_releases(self.root, fn, kinds=("cpa",), ttl=60, now=1010)
            self.assertEqual(code, 0)
            self.assertEqual(discovery.call_count, 1)
            fn.assert_not_called()
            before = Path(self.root, "release-evidence.json").read_bytes()
            report, code = uc.check_releases(self.root, fn, kinds=("cpa",), ttl=60, now=1020)
            self.assertEqual(discovery.call_count, 1)
            fn.assert_not_called()
            self.assertEqual(Path(self.root, "release-evidence.json").read_bytes(), before)
            discovery.return_value = dict(release, tag="v2.0.0", commit="b" * 40)
            fn.side_effect = lambda method, url, **kwargs: (200, self.text if "/" + "a" * 40 + "/" in url
                else self.text.replace("Enabled bool", "Enabled string"))
            report, code = uc.check_releases(self.root, fn, kinds=("cpa",), ttl=60, now=1071)
            self.assertEqual(discovery.call_count, 2)
            self.assertEqual(code, 2)
            self.assertTrue(report["upstreams"]["cpa"]["comparison"]["field_changes"])
            self.assertEqual(uc.PINNED["cpa"]["commit"], "a" * 40)
        self.assertEqual(Path(baseline["root"], "config.go").read_text(), self.text)

    def test_offline_release_check_never_fetches_or_writes_even_without_cache(self):
        with patch.object(self.require_module(), "discover_latest", side_effect=AssertionError("network")):
            report, code = uc.check_releases(self.root, unittest.mock.Mock(), offline=True)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(report["automatic_upgrade"])

    def test_preview_source_check_never_fetches_missing_sources(self):
        settings = dict(tool.DEFAULTS, sub2api_src=os.path.join(self.root, "missing"),
                        _request_relay_preview=True, fetch_upstream_src=True)
        with patch.object(tool, "OUT_DIR", self.root), \
             patch.object(tool, "_http", side_effect=AssertionError("preview network")), \
             patch.object(self.require_module(), "discover_latest", side_effect=AssertionError("network")):
            ok, messages = tool.sync_constants(settings, verbose=False)
        self.assertFalse(ok)
        self.assertTrue(messages)
        self.assertEqual(os.listdir(self.root), [])

    def test_metadata_failure_retains_previous_evidence_and_labels_it_unconfirmed(self):
        self.require_module()
        uc.refresh_snapshot(self.root, "cpa", self.spec, self.fetch, now=1000)
        release = dict(repo=self.spec["repo"], tag=self.spec["tag"], commit=self.spec["commit"], checked_at=1010)
        with patch.dict(uc.PINNED, {"cpa": self.spec}), patch.object(uc, "discover_latest", return_value=release):
            uc.check_releases(self.root, self.fetch, kinds=("cpa",), ttl=60, now=1010)
        with patch.dict(uc.PINNED, {"cpa": self.spec}), patch.object(uc, "discover_latest",
                side_effect=uc.ContractError("metadata unavailable")):
            report, code = uc.check_releases(self.root, self.fetch, kinds=("cpa",), ttl=60, now=1071)
        self.assertEqual(code, 1)
        item = report["upstreams"]["cpa"]
        self.assertEqual(item["last_available_release"]["commit"], "a" * 40)
        self.assertTrue(item["latest_evidence_stale"])
        self.assertNotIn("available_release", item)

    def signed_source_fixture(self):
        specs = copy.deepcopy(self.require_module().PINNED)
        roots = {kind: os.path.join(self.root, "signed-" + kind) for kind in specs}
        digest = hashlib.sha256(self.text.encode()).hexdigest()
        for kind, spec in specs.items():
            spec["expected_hashes"] = {rel: digest for rel in spec["files"]}
            for rel in spec["files"]:
                path = Path(roots[kind], *rel.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.text, encoding="utf8", newline="\n")
        settings = dict(tool.DEFAULTS, sub2api_src=roots["sub2api"], cpa_src=roots["cpa"])
        return specs, roots, settings

    def test_import_rehashes_all_sub2api_files_after_plan_generation(self):
        specs, roots, settings = self.signed_source_fixture()
        with patch.dict(uc.PINNED, specs), patch.object(tool, "load_settings", return_value=settings), \
             patch.object(tool, "cpa_capability_issues", return_value=([], [])), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("write")):
            plan = {"source_contract": tool._upstream_source_contract(settings),
                    "capability_snapshot": dict(tool.CPA_CAPABILITY_SNAPSHOT)}
            path = Path(roots["sub2api"], *specs["sub2api"]["files"][0].split("/"))
            path.write_text(self.text + "// future implementation\n", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "sub2api upstream contract drift"):
                tool.do_import(object(), plan)

    def test_missing_sub2api_contract_file_blocks_before_management_writes(self):
        specs, roots, settings = self.signed_source_fixture()
        with patch.dict(uc.PINNED, specs), patch.object(tool, "load_settings", return_value=settings), \
             patch.object(tool, "cpa_capability_issues", return_value=([], [])):
            plan = {"source_contract": tool._upstream_source_contract(settings),
                    "capability_snapshot": dict(tool.CPA_CAPABILITY_SNAPSHOT)}
            Path(roots["sub2api"], *specs["sub2api"]["files"][-1].split("/")).unlink()
            with self.assertRaisesRegex(ValueError, "sub2api upstream contract drift"):
                tool.do_import(object(), plan)

    def test_configured_or_plan_runtime_source_root_changes_are_rejected(self):
        specs, roots, settings = self.signed_source_fixture()
        copied = os.path.join(self.root, "other-signed-source")
        shutil.copytree(roots["sub2api"], copied)
        with patch.dict(uc.PINNED, specs):
            plan = {"source_contract": tool._upstream_source_contract(settings)}
            tool._validate_upstream_source_contract(plan, settings)
            with self.assertRaisesRegex(ValueError, "configured source roots changed"):
                tool._validate_upstream_source_contract(plan, dict(settings, sub2api_src=copied))
            with self.assertRaisesRegex(ValueError, "current runtime sub2api source root changed"):
                tool._validate_upstream_source_contract(plan, dict(settings, _runtime_sub2api_src=copied))
            plan["source_contract"]["runtime_roots"]["sub2api"] = copied
            with self.assertRaisesRegex(ValueError, "runtime source roots changed"):
                tool._validate_upstream_source_contract(plan, settings)

    def test_corrupted_cached_comparison_is_recomputed_without_network(self):
        self.require_module()
        uc.refresh_snapshot(self.root, "cpa", self.spec, self.fetch, now=1000)
        candidate = dict(self.spec, tag="v2.0.0", commit="b" * 40)
        uc.refresh_snapshot(self.root, "cpa", candidate, lambda *a, **k: (
            200, self.text.replace("Enabled bool", "Enabled string")), now=1000)
        release = dict(repo=self.spec["repo"], tag=candidate["tag"], commit=candidate["commit"], checked_at=1010)
        fn = unittest.mock.Mock(side_effect=AssertionError("network"))
        with patch.dict(uc.PINNED, {"cpa": self.spec}), patch.object(uc, "discover_latest", return_value=release):
            report, code = uc.check_releases(self.root, fn, kinds=("cpa",), now=1010)
        self.assertEqual(code, 2)
        report["upstreams"]["cpa"]["comparison"]["status"] = "verified_pinned"
        cache_report = Path(self.root, "release-evidence.json")
        cache_report.write_text(json.dumps(report), encoding="utf8")
        before = cache_report.read_bytes()
        with patch.dict(uc.PINNED, {"cpa": self.spec}), patch.object(uc, "discover_latest",
                side_effect=AssertionError("metadata network")):
            repaired, code = uc.check_releases(self.root, fn, kinds=("cpa",), now=1020, offline=True)
        self.assertEqual(code, 2)
        self.assertEqual(repaired["upstreams"]["cpa"]["comparison"]["status"], "review_required")
        self.assertTrue(repaired["upstreams"]["cpa"]["comparison"]["field_changes"])
        self.assertEqual(cache_report.read_bytes(), before)
        fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
