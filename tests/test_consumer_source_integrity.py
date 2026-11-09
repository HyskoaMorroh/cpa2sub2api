"""The isolated native build must compile only its reviewed source delta."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("consumer_patch", ROOT / "third_party/patches/apply_consumer_patch.py")
consumer_patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consumer_patch)


class ConsumerSourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "source"
        self.inputs = Path(self.temporary.name) / "inputs"
        self.source.mkdir()
        self.inputs.mkdir()
        self.spec = json.loads((ROOT / "third_party/patches/consumer_patch_manifest.json").read_text())
        files = {
            "internal/runtime/executor/helps/proxy_helpers.go": "package helps\nfunc NewProxyAwareHTTPClient(ctx context.Context, cfg *config.Config, auth *cliproxyauth.Auth, timeout time.Duration) (clientOut *http.Client) {\n\tdefer func() { clientOut = httptransport.Decorate(ctx, clientOut) }()\n}\n",
            "internal/runtime/executor/helps/utls_client.go": "package helps\nfunc fixture() {\n\t\tclientOut = httptransport.Decorate(ctx, clientOut)\n}\n",
            "cmd/server/main.go": "package main\nfunc main() {}\n",
            ".gitignore": "ignored*\n",
        }
        for relative, content in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, newline="\n")
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.spec["upstream_commit"] = self.git("rev-parse", "HEAD").strip()
        self.spec["original_source_hashes"] = {
            relative: consumer_patch.sha(self.source / relative) for relative in self.spec["source_delta"]["modified"]}
        for relative in self.spec["build_inputs"]:
            name = Path(relative).name
            origin = ROOT / relative
            (self.inputs / name).write_bytes(origin.read_bytes())
        original_output = subprocess.check_output

        def output(args, **kwargs):
            # Formatting is not under test; the Docker Go tests exercise real gofmt.
            if args == ["gofmt"]:
                return kwargs["input"]
            return original_output(args, **kwargs)

        self.enterContext(patch.object(consumer_patch, "SOURCE", self.source))
        self.enterContext(patch.object(consumer_patch, "INPUTS", self.inputs))
        self.enterContext(patch.object(consumer_patch, "STAMP", self.inputs / "applied-source.json"))
        self.enterContext(patch.object(consumer_patch, "require_workspace", return_value=None))
        self.enterContext(patch.object(consumer_patch.subprocess, "check_output", side_effect=output))
        self.enterContext(patch.object(consumer_patch.subprocess, "check_call", return_value=0))

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source, text=True)

    def change(self, relative):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf8") as stream:
            stream.write("// changed\n")

    def patched(self):
        consumer_patch.apply(self.spec)
        return json.loads(consumer_patch.STAMP.read_text())["patched_source_hashes"]

    def test_pristine_and_exact_reviewed_delta_are_accepted(self):
        consumer_patch.require_source(self.spec)
        evidence = consumer_patch.verify_source_tree(self.spec, self.patched())
        self.assertEqual(evidence["tracked_head_files"], 4)
        self.assertEqual(evidence["modified"], sorted(self.spec["source_delta"]["modified"]))
        self.assertEqual(evidence["added"], sorted(self.spec["source_delta"]["added"]))

    def test_staged_unrelated_change_is_rejected_before_apply(self):
        self.change("cmd/server/main.go")
        self.git("add", "cmd/server/main.go")
        with self.assertRaisesRegex(ValueError, "index changed"):
            consumer_patch.require_source(self.spec)

    def test_untracked_and_ignored_source_are_rejected_before_apply(self):
        for relative in ("internal/runtime/executor/helps/zz_untracked.go", "ignored-source.go"):
            with self.subTest(relative=relative):
                self.change(relative)
                with self.assertRaisesRegex(ValueError, "untracked source delta"):
                    consumer_patch.require_source(self.spec)
                (self.source / relative).unlink()

    def test_assume_unchanged_and_skip_worktree_do_not_hide_actual_source_bytes(self):
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                self.git("update-index", "--" + flag, "cmd/server/main.go")
                self.change("cmd/server/main.go")
                with self.assertRaisesRegex(ValueError, "source byte digest mismatch"):
                    consumer_patch.require_source(self.spec)
                self.git("update-index", "--no-" + flag, "cmd/server/main.go")
                self.git("checkout", "--", "cmd/server/main.go")

    def test_post_apply_unrelated_untracked_staged_and_modified_bytes_are_rejected(self):
        digests = self.patched()
        self.change("cmd/server/main.go")
        with self.assertRaisesRegex(ValueError, "tracked source delta"):
            consumer_patch.verify_source_tree(self.spec, digests)
        self.git("checkout", "--", "cmd/server/main.go")
        self.change("zz_extra.go")
        with self.assertRaisesRegex(ValueError, "untracked source delta"):
            consumer_patch.verify_source_tree(self.spec, digests)
        (self.source / "zz_extra.go").unlink()
        self.git("add", self.spec["source_delta"]["modified"][0])
        with self.assertRaisesRegex(ValueError, "index changed"):
            consumer_patch.verify_source_tree(self.spec, digests)

    def test_forged_stamp_cannot_authorize_modified_patch_bytes_or_extra_scope(self):
        digests = self.patched()
        relative = self.spec["source_delta"]["added"][0]
        self.change(relative)
        digests[relative] = consumer_patch.sha(self.source / relative)
        with self.assertRaisesRegex(ValueError, "reviewed inputs"):
            consumer_patch.verify_source_tree(self.spec, digests)
        altered = copy.deepcopy(self.spec)
        altered["source_delta"]["modified"].append("cmd/server/main.go")
        with self.assertRaisesRegex(ValueError, "reviewed scope"):
            consumer_patch.verify_source_tree(altered, digests)

    def test_export_ignore_cannot_hide_changed_committed_source(self):
        attributes = self.source / ".git/info/attributes"
        attributes.write_text("cmd/server/main.go export-ignore\n", newline="\n")
        self.git("update-index", "--assume-unchanged", "cmd/server/main.go")
        self.change("cmd/server/main.go")
        with self.assertRaisesRegex(ValueError, "source byte digest mismatch"):
            consumer_patch.require_source(self.spec)

    def test_replaced_commit_cannot_change_the_fixed_head_source(self):
        original = self.spec["upstream_commit"]
        self.change("cmd/server/main.go")
        self.git("add", "cmd/server/main.go")
        self.git("commit", "-qm", "replacement")
        replacement = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "HEAD", original)
        self.git("replace", original, replacement)
        # require_workspace is mocked for this portable fixture. Object reads
        # must still independently ignore replacement history.
        with self.assertRaisesRegex(ValueError, "index changed"):
            consumer_patch.require_source(self.spec)


if __name__ == "__main__":
    unittest.main()
