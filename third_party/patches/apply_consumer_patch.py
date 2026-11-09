#!/usr/bin/env python3
"""Apply a reviewed consumer patch only inside its isolated Docker build tree."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

SOURCE = Path("/build/consumer-src")
INPUTS = Path("/build/patches")
MANIFEST = INPUTS / "consumer_patch_manifest.json"
STAMP = INPUTS / "applied-source.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_lf_sha(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def command(*args):
    return subprocess.check_output(args, cwd=SOURCE, env=source_env(), text=True).strip()


def source_env():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def manifest():
    value = json.loads(MANIFEST.read_text(encoding="utf8"))
    if (value.get("schema_version") != 1 or type(value.get("automatic_output_cap")) is not int
            or value.get("automatic_output_cap") != 16
            or value.get("automatic_output_header") != "X-CPA2SUB2API-Auto-Output-Cap"):
        raise ValueError("unsupported consumer patch manifest")
    return value


def build_inputs(spec, canonical=False):
    hashes = {}
    for relative in spec["build_inputs"]:
        if relative in ("Dockerfile", ".dockerignore", "requirements.txt"):
            path = INPUTS / relative
        elif relative.startswith("third_party/patches/"):
            name = relative[len("third_party/patches/"):]
            if "/" in name or "\\" in name or name in ("", ".", ".."):
                raise ValueError("unsafe declared build input")
            path = INPUTS / name
        else:
            raise ValueError("undeclared consumer build input")
        hashes[relative] = canonical_lf_sha(path) if canonical else sha(path)
    return hashes


def require_workspace(spec):
    if SOURCE.resolve() != Path("/build/consumer-src") or not (SOURCE / ".git").is_dir() or (SOURCE / ".git").is_symlink():
        raise ValueError("isolated Docker consumer build workspace required")
    if command("git", "rev-parse", "HEAD") != spec["upstream_commit"]:
        raise ValueError("consumer upstream commit mismatch")
    if command("git", "for-each-ref", "--format=%(refname)", "refs/replace") or (SOURCE / ".git/info/grafts").exists():
        raise ValueError("consumer replacement history is not permitted")


def git_paths(*args):
    raw = subprocess.check_output(["git", *args, "-z"], cwd=SOURCE, env=source_env())
    return {item.decode("utf8", errors="strict") for item in raw.split(b"\0") if item}


def head_snapshot():
    """Bind every compiled source byte to HEAD, including skip-worktree files."""
    # archive honours export-ignore/export-subst attributes. The compiler does
    # not; enumerate the actual committed blobs, unaffected by those attributes.
    raw = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", "HEAD"], cwd=SOURCE, env=source_env())
    result = {}
    entries = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, object_type, oid = metadata.split()
        relative = name.decode("utf8", errors="strict")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ValueError("unsafe consumer source tree path")
        if object_type != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise ValueError("unsupported consumer source tree entry")
        kind = "symlink" if mode == b"120000" else "file"
        entries.append((relative, kind, oid))
    objects = subprocess.check_output(["git", "cat-file", "--batch"], cwd=SOURCE,
        env=source_env(), input=b"".join(oid + b"\n" for _, _, oid in entries))
    offset = 0
    for relative, kind, oid in entries:
        end = objects.index(b"\n", offset)
        actual_oid, object_type, size = objects[offset:end].split()
        if actual_oid != oid or object_type != b"blob":
            raise ValueError("consumer committed blob mismatch")
        size = int(size)
        payload = objects[end + 1:end + 1 + size]
        offset = end + 1 + size + 1
        if len(payload) != size or objects[offset - 1:offset] != b"\n":
            raise ValueError("consumer committed blob truncated")
        result[relative] = {"kind": kind, "sha256": hashlib.sha256(payload).hexdigest()}
    if offset != len(objects):
        raise ValueError("consumer committed blob stream differs")
    return result


def current_source_sha(relative, kind="file"):
    path = SOURCE / relative
    if kind == "symlink":
        if not path.is_symlink():
            raise ValueError("consumer source entry type changed: " + relative)
        return hashlib.sha256(os.readlink(path).encode("utf8")).hexdigest()
    if not path.is_file() or path.is_symlink():
        raise ValueError("consumer source entry type changed: " + relative)
    return sha(path)


def declared_delta(spec):
    expected = {
        "modified": ["internal/runtime/executor/helps/proxy_helpers.go", "internal/runtime/executor/helps/utls_client.go"],
        "added": ["internal/runtime/executor/helps/diagnostic_redirect.go", "internal/runtime/executor/helps/diagnostic_redirect_test.go"],
    }
    if spec.get("source_delta") != expected:
        raise ValueError("consumer patch source delta differs from reviewed scope")
    return expected


def patch_helper(relative, text):
    if relative.endswith("proxy_helpers.go"):
        signature = "func NewProxyAwareHTTPClient(ctx context.Context, cfg *config.Config, auth *cliproxyauth.Auth, timeout time.Duration) (clientOut *http.Client) {\n\t"
        old = signature + "defer func() { clientOut = httptransport.Decorate(ctx, clientOut) }()"
        new = (signature + "defer func() {\n\t\tclientOut = httptransport.Decorate(ctx, clientOut)\n"
               "\t\tclientOut = applyCPA2Sub2APIDiagnosticRedirectPolicy(clientOut, auth)\n"
               "\t\tclientOut = applyCPA2Sub2APIDiagnosticWirePolicy(ctx, clientOut, auth)\n\t}()")
    else:
        old = "\t\tclientOut = httptransport.Decorate(ctx, clientOut)"
        new = (old + "\n\t\tclientOut = applyCPA2Sub2APIDiagnosticRedirectPolicy(clientOut, auth)"
               + "\n\t\tclientOut = applyCPA2Sub2APIDiagnosticWirePolicy(ctx, clientOut, auth)")
    if text.count(old) != 1:
        raise ValueError("HTTP client patch location mismatch: " + relative)
    return text.replace(old, new).encode("utf8")


def expected_patch_hashes(spec, tests_only=False):
    delta = declared_delta(spec)
    result = {}
    for relative in ([] if tests_only else delta["modified"]) + delta["added"]:
        if relative in delta["modified"]:
            original = subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=SOURCE, env=source_env())
            payload = patch_helper(relative, original.decode("utf8"))
        else:
            payload = (INPUTS / Path(relative).name).read_bytes()
        formatted = subprocess.check_output(["gofmt"], input=payload, cwd=SOURCE)
        result[relative] = hashlib.sha256(formatted).hexdigest()
    return result


def verify_source_tree(spec, patched=None, tests_only=False):
    require_workspace(spec)
    delta = declared_delta(spec)
    modified = set() if patched is None or tests_only else set(delta["modified"])
    added = set() if patched is None else set(delta["added"])
    if git_paths("diff", "--cached", "HEAD", "--name-only"):
        raise ValueError("consumer source index changed")
    if git_paths("diff", "HEAD", "--name-only") != modified:
        raise ValueError("consumer tracked source delta differs from reviewed scope")
    # Include ignored files too: Go compiles ignored .go files just like other files.
    if git_paths("ls-files", "--others") != added:
        raise ValueError("consumer untracked source delta differs from reviewed scope")
    snapshot = head_snapshot()
    if patched is not None and set(patched) != modified | added:
        raise ValueError("consumer patched digest set differs from reviewed scope")
    if patched is not None and patched != expected_patch_hashes(spec, tests_only):
        raise ValueError("consumer patched byte digests differ from reviewed inputs")
    for relative, original in snapshot.items():
        expected = patched[relative] if relative in modified else original["sha256"]
        if current_source_sha(relative, original["kind"]) != expected:
            raise ValueError("consumer source byte digest mismatch: " + relative)
    for relative in added:
        if relative in snapshot or current_source_sha(relative) != patched[relative]:
            raise ValueError("consumer added source byte digest mismatch: " + relative)
    return {"tracked_head_files": len(snapshot), "modified": sorted(modified), "added": sorted(added),
            "head_tree_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def require_source(spec):
    verify_source_tree(spec)
    for relative, expected in spec["original_source_hashes"].items():
        if sha(SOURCE / relative) != expected:
            raise ValueError("consumer upstream source digest mismatch: " + relative)


def apply(spec, tests_only=False):
    require_source(spec)
    hashes = build_inputs(spec)
    helpers = SOURCE / "internal/runtime/executor/helps"
    for name in ("diagnostic_redirect.go", "diagnostic_redirect_test.go"):
        shutil.copyfile(INPUTS / name, helpers / name)
    if not tests_only:
        for relative in declared_delta(spec)["modified"]:
            path = SOURCE / relative
            path.write_bytes(patch_helper(relative, path.read_text(encoding="utf8")))
    changed = [helpers / name for name in ("diagnostic_redirect.go", "diagnostic_redirect_test.go",
                                          "proxy_helpers.go", "utls_client.go")]
    subprocess.check_call(["gofmt", "-w"] + [str(path) for path in changed])
    patched = {path.relative_to(SOURCE).as_posix(): sha(path) for path in changed
               if not tests_only or path.name.startswith("diagnostic_redirect")}
    source_evidence = verify_source_tree(spec, patched, tests_only=tests_only)
    if tests_only:
        return
    stamp = {
        "upstream_commit": spec["upstream_commit"],
        "original_source_hashes": spec["original_source_hashes"],
        "patched_source_hashes": patched,
        "source_tree_evidence": source_evidence,
        "build_input_hashes": hashes,
        "build_input_canonical_lf_hashes": build_inputs(spec, canonical=True),
    }
    STAMP.write_text(json.dumps(stamp, sort_keys=True, indent=2), encoding="utf8")


def build(spec, binary):
    stamp = json.loads(STAMP.read_text(encoding="utf8"))
    if stamp["build_input_hashes"] != build_inputs(spec):
        raise ValueError("consumer build inputs changed after applying patch")
    if stamp["source_tree_evidence"] != verify_source_tree(spec, stamp["patched_source_hashes"]):
        raise ValueError("consumer source tree changed before build")
    epoch = int(command("git", "show", "-s", "--format=%ct", spec["upstream_commit"]))
    date = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = dict(os.environ, CGO_ENABLED="1", GOOS="linux", SOURCE_DATE_EPOCH=str(epoch))
    binary.parent.mkdir(parents=True, exist_ok=True)
    flags = "-s -w -X main.Version=%s-cpa2sub2api-diagnostic-no-redirect-v1 -X main.Commit=%s -X main.BuildDate=%s" % (
        spec["upstream_version"], spec["upstream_commit"], date)
    subprocess.check_call(["go", "build", "-trimpath", "-buildvcs=false", "-ldflags=" + flags,
                           "-o", str(binary), "./cmd/server/"], cwd=SOURCE, env=env)
    if stamp["source_tree_evidence"] != verify_source_tree(spec, stamp["patched_source_hashes"]):
        raise ValueError("consumer source tree changed during build")
    stamp["build_recipe"] = {"source_date_epoch": epoch, "build_date_utc": date, "cgo_enabled": "1",
                             "goos": "linux", "trimpath": True, "buildvcs": False}
    STAMP.write_text(json.dumps(stamp, sort_keys=True, indent=2), encoding="utf8")


def provenance(spec, binary, output):
    stamp = json.loads(STAMP.read_text(encoding="utf8"))
    if stamp["upstream_commit"] != spec["upstream_commit"] or command("git", "rev-parse", "HEAD") != spec["upstream_commit"]:
        raise ValueError("compiled consumer commit mismatch")
    if stamp["build_input_hashes"] != build_inputs(spec):
        raise ValueError("consumer build inputs changed after applying patch")
    if stamp["source_tree_evidence"] != verify_source_tree(spec, stamp["patched_source_hashes"]):
        raise ValueError("consumer source tree changed before provenance")
    builder_image = os.environ.get("CPA_CONSUMER_BUILD_IMAGE")
    if builder_image != spec["builder_image"]:
        raise ValueError("consumer builder image differs from reviewed digest")
    go_version = command("go", "version")
    if "go1.26.10 " not in go_version:
        raise ValueError("consumer compiler differs from reviewed Go 1.26.10")
    feature_env = dict(os.environ, CPA2SUB2API_GUARD_FEATURE_QUERY="1")
    feature_raw = subprocess.check_output([str(binary), "-h"], env=feature_env, text=True)
    feature = json.loads(feature_raw)
    if (feature.get("feature_id") != spec["feature_id"] or feature.get("source_commit") != spec["upstream_commit"]
            or type(feature.get("automatic_output_cap")) is not int
            or feature.get("automatic_output_cap") != spec["automatic_output_cap"]
            or feature.get("automatic_output_header") != spec["automatic_output_header"]):
        raise ValueError("compiled consumer guard feature mismatch")
    result = {
        "schema_version": 1,
        "feature_id": spec["feature_id"],
        "automatic_output_cap": spec["automatic_output_cap"],
        "automatic_output_header": spec["automatic_output_header"],
        "upstream_repo": spec["upstream_repo"],
        "upstream_version": spec["upstream_version"],
        "upstream_commit": spec["upstream_commit"],
        "builder_image": builder_image,
        "go_version": go_version,
        "goarch": command("go", "env", "GOARCH"),
        "cgo_enabled": "1",
        "binary_sha256": sha(binary),
        "build_input_hashes": stamp["build_input_hashes"],
        "build_input_canonical_lf_hashes": stamp["build_input_canonical_lf_hashes"],
        "build_recipe": stamp["build_recipe"],
        "original_source_hashes": stamp["original_source_hashes"],
        "patched_source_hashes": stamp["patched_source_hashes"],
        "source_tree_evidence": stamp["source_tree_evidence"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "build", "provenance"))
    parser.add_argument("--tests-only", action="store_true")
    parser.add_argument("--binary", type=Path, default=Path("/CLIProxyAPI/CLIProxyAPI"))
    parser.add_argument("--output", type=Path, default=Path("/build/consumer-provenance.json"))
    args = parser.parse_args()
    spec = manifest()
    if args.action == "apply":
        apply(spec, tests_only=args.tests_only)
    elif args.action == "build":
        build(spec, args.binary)
    else:
        provenance(spec, args.binary, args.output)


if __name__ == "__main__":
    main()
