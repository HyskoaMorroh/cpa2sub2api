"""Immutable upstream evidence; release discovery never upgrades a consumer."""
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

CACHE_TTL = 7 * 24 * 60 * 60
VERIFIED_AT = "2026-11-08"
PINNED = {
    "cpa": {
        "repo": "router-for-me/CLIProxyAPI", "prefix": "", "tag": "v8.0.60",
        "commit": "91e0dee827abf476f9f86246f68c500a112f7155",
        "files": ["internal/config/config_types.go"],
        "expected_hashes": {"internal/config/config_types.go":
            "a234c8850ae30b8124f17613abdbe56f8bd0e3dd5e6cbc9ebc0e809c4f3ceb53"},
    },
    "sub2api": {
        "repo": "Wei-Shaw/sub2api", "prefix": "backend", "tag": "v0.2.20",
        "commit": "dab829a4e9b7d1cc307189aea8b0319eb29e29d0",
        "files": ["internal/service/account_header_override.go", "internal/domain/constants.go",
            "internal/handler/admin/account_handler.go", "internal/handler/admin/account_data.go",
            "internal/handler/admin/scheduled_test_handler.go", "internal/service/channel.go",
            "ent/schema/account.go", "migrations/081_create_channels.sql"],
        "expected_hashes": {
            "internal/service/account_header_override.go": "fa69098cb41e9d481bd9e7e22bc136a4a0d4a5e4a07275f2217aa5af0e0635c3",
            "internal/domain/constants.go": "bfd4658e8ff76e2940b2eba26e83286b2e6b973242564c5c73d4ddf5ef80ffe0",
            "internal/handler/admin/account_handler.go": "25a6aefe8bdbee39dbbd779d1c8692b986b5a76c922caf2dd0e2ce2cae9006ab",
            "internal/handler/admin/account_data.go": "74813d5bf027950a76bfaa8bd2ccc1cad682cdc4c6f03a44247f2a3545576ecf",
            "internal/handler/admin/scheduled_test_handler.go": "ac8a6612b3f0d09a77f430cf9692401f7b1fbed689a1df8bc7eacd3ed50ad4b2",
            "internal/service/channel.go": "7ee3fffa31c6cfac4576339a3d8b18c5546079e891702fb25f2c12f8f6487d8b",
            "ent/schema/account.go": "dfc1cd50c5d0736234fe5c5553f49a12e8153801a7a06a9ccd6ffa4dc7917ff2",
            "migrations/081_create_channels.sql": "4eb880b780b2a1df07ec8dff5a9cd1ecf5cd0d35119051018f3ed3b4ae594285",
        },
    },
}

# Exact reviewed representations of an individual source contract. This does
# not approve or select the newer runtime. Go AST, complete token stream and
# directives were compared against the pinned file before recording this SHA.
REVIEWED_SOURCE_EQUIVALENCES = {
    ("cpa", "internal/config/config_types.go",
     "0ba74d8a82bd80bef18cd693360283932e45a0ae767d1b71d581c736bd620ab6"): {
        "baseline_sha256": "a234c8850ae30b8124f17613abdbe56f8bd0e3dd5e6cbc9ebc0e809c4f3ceb53",
        "source_tag": "v8.0.61",
        "source_commit": "681ce41b6e44c45b15b79378b003909d6ba29e68",
        "reviewed_at": "2026-11-09",
        "comment_and_layout_only": True,
        "go_ast_sha256": "0f45127f4b2a4955ec4a88c48db1ec5241034686526063d9d02243e74806bb00",
        "go_token_sha256": "2992ab2faa9d4cc5af44fd480252c80660fdee618dd8e8dd4cbb2e05c99f2bd6",
        "go_directives": [],
        "cgo": False,
        "scope": "this exact configuration contract file; no runtime upgrade",
    },
}


def reviewed_source_equivalence(kind, relative, actual_sha256):
    item = REVIEWED_SOURCE_EQUIVALENCES.get((kind, relative, actual_sha256))
    baseline = PINNED.get(kind, {}).get("expected_hashes", {}).get(relative)
    if item is None or item["baseline_sha256"] != baseline:
        return None
    return copy.deepcopy(item)


class ContractError(ValueError):
    pass


def recursive_fields(text):
    """Named Go structures, including named pointer/array children; maps are leaves.

    This is evidence extraction, not proof that arbitrary Go code is understood.
    Any source change still requires review, including changes outside these tags.
    """
    structs = {}
    for match in re.finditer(r"type\s+(\w+)\s+struct\s*\{(.*?)^\}", text, re.S | re.M):
        fields = {}
        for line in match.group(2).splitlines():
            item = re.match(r"\s*\w+\s+(.+?)\s+`([^`]+)`", line)
            if not item:
                continue
            tag = re.search(r'yaml:"([^",]+)', item.group(2)) or re.search(r'json:"([^",]+)', item.group(2))
            if tag and tag.group(1) != "-":
                fields[tag.group(1)] = re.sub(r"\s+", " ", item.group(1).strip())
        if fields:
            structs[match.group(1)] = fields
    result = {}
    def walk(name, path, ancestors):
        for field, typ in sorted(structs[name].items()):
            key = path + "." + field
            result[key] = typ
            child = re.sub(r"^(?:\[\]|\*)+", "", typ)
            if child in structs and child not in ancestors and len(ancestors) < 12:
                walk(child, key + ("[]" if typ.startswith("[]") else ""), ancestors + (child,))
    for name in sorted(structs):
        walk(name, name, (name,))
    return result


def field_changes(before, after):
    changes = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old != new:
            kind = "added" if path not in before else "removed" if path not in after else "type"
            changes.append({"path": path, "kind": kind, "before": old, "after": new})
    return changes


def _spec(spec, kind):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+", kind)
            or not re.fullmatch(r"[0-9a-f]{40}", str(spec.get("commit", "")))
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(spec.get("repo", "")))
            or not isinstance(spec.get("tag"), str) or not spec["tag"]
            or not isinstance(spec.get("files"), list) or not spec["files"]):
        raise ContractError("invalid immutable upstream revision")
    for rel in spec["files"] + ([spec["prefix"]] if spec.get("prefix") else []):
        if (not isinstance(rel, str) or rel.startswith("/") or "\\" in rel
                or any(p in ("", ".", "..") for p in rel.split("/"))):
            raise ContractError("unsafe upstream source path")


def _read(path):
    with open(path, "rb") as stream:
        return stream.read()


def _schema(root, files):
    return {rel: recursive_fields(_read(os.path.join(root, *rel.split("/"))).decode("utf-8"))
            for rel in files if rel.endswith(".go")}


def load_snapshot(cache_root, kind, spec, now=None):
    _spec(spec, kind)
    now = time.time() if now is None else now
    base = os.path.join(cache_root, kind, spec["commit"])
    try:
        with open(os.path.join(base, "CURRENT.json"), encoding="utf-8") as stream:
            generation = json.load(stream)
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            return None
        root = os.path.join(base, generation)
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as stream:
            manifest = json.load(stream)
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict)
                or manifest.get("format") != 1 or manifest.get("repo") != spec["repo"]
                or manifest.get("tag") != spec["tag"] or manifest.get("commit") != spec["commit"]
                or manifest.get("prefix", "") != spec.get("prefix", "")
                or set(manifest.get("files", {})) != set(spec["files"])):
            return None
        stamp = manifest.get("extracted_at")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 or stamp > now + 300:
            return None
        for rel, digest in manifest["files"].items():
            actual = hashlib.sha256(_read(os.path.join(root, *rel.split("/")))).hexdigest()
            if digest != actual or spec.get("expected_hashes", {}).get(rel, actual) != actual:
                return None
        if manifest.get("schema") != _schema(root, spec["files"]):
            return None
        return dict(manifest, root=root)
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def refresh_snapshot(cache_root, kind, spec, http_fn, now=None):
    """Publish only a complete immutable generation. Previous generations remain."""
    _spec(spec, kind)
    now = time.time() if now is None else now
    base = os.path.join(cache_root, kind, spec["commit"])
    os.makedirs(base, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".pending-", dir=base)
    try:
        digests = {}
        for rel in spec["files"]:
            parts = [spec["repo"], spec["commit"], spec.get("prefix", ""), rel]
            url = "https://raw.githubusercontent.com/" + "/".join(p for p in parts if p)
            try:
                status, text = http_fn("GET", url, timeout=30)
            except Exception:
                raise ContractError("upstream source request failed: " + rel) from None
            if status != 200 or not isinstance(text, str) or not text.strip():
                raise ContractError("upstream source unavailable: " + rel)
            raw = text.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            if spec.get("expected_hashes", {}).get(rel, digest) != digest:
                raise ContractError("pinned upstream source digest changed: " + rel)
            path = os.path.join(stage, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(raw)
            digests[rel] = digest
        manifest = {"format": 1, "repo": spec["repo"], "tag": spec["tag"],
            "commit": spec["commit"], "prefix": spec.get("prefix", ""), "extracted_at": now,
            "files": digests, "schema": _schema(stage, spec["files"])}
        with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
        generation = uuid.uuid4().hex
        root = os.path.join(base, generation)
        os.replace(stage, root)
        stage = None
        fd, pointer = tempfile.mkstemp(prefix=".pointer-", dir=base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(generation, stream)
            os.replace(pointer, os.path.join(base, "CURRENT.json"))
        finally:
            if os.path.exists(pointer):
                os.unlink(pointer)
        return dict(manifest, root=root)
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def cached_or_refresh(cache_root, kind, spec, http_fn, now=None, ttl=CACHE_TTL, offline=False, force=False):
    now = time.time() if now is None else now
    if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
        raise ContractError("upstream cache TTL must be a positive number")
    cached = load_snapshot(cache_root, kind, spec, now=now)
    stale = bool(cached and now - cached["extracted_at"] > ttl)
    if cached and not stale and not force:
        return cached, []
    if offline:
        return cached, ["upstream source cache stale; offline pinned evidence retained" if stale else
                        "offline: upstream source cache unavailable"]
    try:
        return refresh_snapshot(cache_root, kind, spec, http_fn, now=now), []
    except ContractError as error:
        return cached, [str(error), "upstream source cache stale; pinned evidence retained" if cached else
                        "upstream source cache unavailable; built-in contract only"]


def source_evidence(root, kind):
    """Read-only check against reviewed source bytes; missing sources use baseline."""
    spec = PINNED[kind]
    result = {"repo": spec["repo"], "tag": spec["tag"], "commit": spec["commit"],
              "verified_at": VERIFIED_AT, "extracted_at": None, "status": "built_in",
              "files": {}, "unverified_files": []}
    if not root or not os.path.isdir(root):
        return result
    equivalents = {}
    for rel, expected in spec["expected_hashes"].items():
        try:
            actual = hashlib.sha256(_read(os.path.join(root, *rel.split("/")))).hexdigest()
        except OSError:
            actual = None
        result["files"][rel] = actual
        if actual != expected:
            equivalent = reviewed_source_equivalence(kind, rel, actual)
            if equivalent is None:
                result["unverified_files"].append(rel)
            else:
                equivalents[rel] = equivalent
    result["status"] = ("review_required" if result["unverified_files"] else
                        "verified_reviewed_equivalent" if equivalents else "verified_pinned")
    if equivalents:
        # Existing tag/commit identify the reviewed consumer contract. The exact
        # newer source-file representation is described separately, never as a
        # verified checkout or an automatically activated consumer revision.
        result["revision_role"] = "reviewed consumer contract baseline"
        result["source_file_equivalences"] = equivalents
    if result["status"] == "verified_pinned":
        try:
            with open(os.path.join(root, "manifest.json"), encoding="utf-8") as stream:
                manifest = json.load(stream)
            if (manifest.get("repo") == spec["repo"] and manifest.get("commit") == spec["commit"]
                    and manifest.get("tag") == spec["tag"] and manifest.get("files") == result["files"]):
                result["extracted_at"] = manifest.get("extracted_at")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return result


def compare_snapshots(before, after):
    changes = []
    for rel in sorted(set(before["files"]) | set(after["files"])):
        fields = field_changes(before.get("schema", {}).get(rel, {}), after.get("schema", {}).get(rel, {}))
        changes.extend(dict(change, file=rel) for change in fields)
        if before["files"].get(rel) != after["files"].get(rel) and not fields:
            changes.append({"file": rel, "path": rel, "kind": "source_content", "before":
                before["files"].get(rel), "after": after["files"].get(rel)})
    changed = bool(changes or before["commit"] != after["commit"])
    return {"status": "review_required" if changed else "verified_pinned",
            "automatic_upgrade": False, "field_changes": changes,
            "pinned_tag": before["tag"], "pinned_commit": before["commit"],
            "available_tag": after["tag"], "available_commit": after["commit"]}


def _get_json(url, http_fn):
    try:
        status, raw = http_fn("GET", url, headers={"Accept": "application/vnd.github+json",
                             "User-Agent": "cpa2sub2api-contract"}, timeout=20)
    except Exception:
        raise ContractError("GitHub metadata request failed") from None
    if status != 200:
        raise ContractError("GitHub metadata unavailable")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ContractError("invalid GitHub metadata") from None
    if not isinstance(value, dict):
        raise ContractError("invalid GitHub metadata")
    return value


def discover_latest(kind, http_fn):
    """Official latest stable release, resolving annotated tags to commit SHA."""
    spec = PINNED[kind]
    repo = spec["repo"]
    metadata_source = "github_api"
    try:
        release = _get_json("https://api.github.com/repos/" + repo + "/releases/latest", http_fn)
        if release.get("draft") or release.get("prerelease"):
            raise ContractError("latest upstream release is not stable")
        tag = release.get("tag_name")
    except ContractError:
        # Public API rate limits must not be confused with no available release.
        request = urllib.request.Request("https://github.com/" + repo + "/releases/latest",
                                         headers={"User-Agent": "cpa2sub2api-contract"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                final = response.geturl()
            prefix = "https://github.com/" + repo + "/releases/tag/"
            if not final.startswith(prefix):
                raise ContractError("official release redirect did not identify a release")
            tag = urllib.parse.unquote(final[len(prefix):])
            metadata_source = "github_release_redirect_and_git_ref"
        except Exception:
            raise ContractError("latest upstream release unavailable; pinned contract retained") from None
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", tag):
        raise ContractError("invalid upstream release tag")
    try:
        commit = _get_json("https://api.github.com/repos/" + repo + "/commits/" +
                           urllib.parse.quote(tag, safe=""), http_fn).get("sha", "")
    except ContractError:
        metadata_source = "github_api_and_git_ref" if metadata_source == "github_api" else metadata_source
        try:
            ref = "refs/tags/" + tag
            response = subprocess.run(["git", "ls-remote", "--tags", "https://github.com/" + repo + ".git",
                                       ref, ref + "^{}"], check=True, capture_output=True, text=True, timeout=25)
            refs = {row.split()[1]: row.split()[0] for row in response.stdout.splitlines() if len(row.split()) == 2}
            commit = refs.get(ref + "^{}", refs.get(ref, ""))
        except Exception:
            raise ContractError("upstream release commit unavailable; pinned contract retained") from None
    if not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise ContractError("upstream release did not resolve to a commit")
    return {"repo": repo, "tag": tag, "commit": commit, "checked_at": time.time(),
            "metadata_source": metadata_source, "automatic_upgrade": False}


def candidate_spec(kind, release):
    spec = copy.deepcopy(PINNED[kind])
    if release.get("repo") != spec["repo"]:
        raise ContractError("upstream release repository mismatch")
    spec.update(tag=release["tag"], commit=release["commit"])
    if spec["commit"] != PINNED[kind]["commit"]:
        spec.pop("expected_hashes", None)
    _spec(spec, kind)
    return spec


def check_releases(cache_root, http_fn, kinds=None, ttl=CACHE_TTL, force=False, offline=False, now=None):
    """TTL-driven release discovery. Only candidate evidence changes, never runtime.

    Normal source checks and explicit CLI checks share this path. Expired or
    missing metadata causes discovery only in the explicit online mode.
    """
    now = time.time() if now is None else now
    if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
        raise ContractError("upstream cache TTL must be a positive number")
    path = os.path.join(cache_root, "release-evidence.json")
    try:
        with open(path, encoding="utf-8") as stream:
            previous = json.load(stream)
        if not isinstance(previous, dict) or not isinstance(previous.get("upstreams"), dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    report = {"automatic_upgrade": False, "checked_at": previous.get("checked_at"),
              "upstreams": copy.deepcopy(previous.get("upstreams", {}))}
    changed, exit_code = False, 0
    for kind in kinds or tuple(PINNED):
        spec = PINNED[kind]
        prior = report["upstreams"].get(kind, {})
        release = prior.get("available_release", {}) if isinstance(prior, dict) else {}
        stamp = release.get("checked_at") if isinstance(release, dict) else None
        fresh = False
        verified_comparison = None
        if (type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp <= ttl
                and isinstance(prior.get("comparison"), dict)
                and prior["comparison"].get("status") in ("verified_pinned", "review_required")
                and prior["comparison"].get("pinned_commit") == spec["commit"]):
            try:
                candidate = candidate_spec(kind, release)
                candidate_snapshot = load_snapshot(cache_root, kind, candidate, now=now)
                pinned_snapshot = load_snapshot(cache_root, kind, spec, now=now)
                fresh = candidate_snapshot is not None and pinned_snapshot is not None
                if fresh:
                    verified_comparison = compare_snapshots(pinned_snapshot, candidate_snapshot)
            except (ContractError, KeyError):
                fresh = False
        if fresh and not force:
            if prior["comparison"] != verified_comparison:
                prior["comparison"] = verified_comparison
                prior.setdefault("warnings", []).append("cached comparison corrected from verified source snapshots")
                changed = changed or not offline
            if verified_comparison["status"] == "review_required":
                exit_code = max(exit_code, 2)
            continue
        if offline:
            if isinstance(prior, dict):
                prior["latest_evidence_stale"] = not fresh
            report["upstreams"][kind] = prior
            continue
        changed = True
        item = {"pinned_tag": spec["tag"], "pinned_commit": spec["commit"], "warnings": []}
        snapshot, warnings = cached_or_refresh(cache_root, kind, spec, http_fn, now=now, ttl=ttl)
        item["warnings"].extend(warnings)
        item["source_cache"] = "available" if snapshot else "unavailable"
        if snapshot:
            item.update(extracted_at=snapshot["extracted_at"], files=snapshot["files"],
                        source_cache_stale=now - snapshot["extracted_at"] > ttl)
        try:
            latest = discover_latest(kind, http_fn)
            latest["checked_at"] = now
            item["available_release"] = latest
            item["latest_evidence_stale"] = False
            candidate = candidate_spec(kind, latest)
            if snapshot and candidate["commit"] == snapshot["commit"]:
                candidate_snapshot = snapshot
            else:
                candidate_snapshot, candidate_warnings = cached_or_refresh(
                    cache_root, kind, candidate, http_fn, now=now, ttl=ttl)
                item["warnings"].extend(candidate_warnings)
            if snapshot and candidate_snapshot:
                item["comparison"] = compare_snapshots(snapshot, candidate_snapshot)
            else:
                item["comparison"] = {"status": "review_required", "automatic_upgrade": False,
                    "pinned_commit": spec["commit"], "reason": "complete source comparison unavailable"}
            if item["comparison"]["status"] == "review_required":
                exit_code = max(exit_code, 2)
                differences = item["comparison"].get("field_changes", [])
                item["warnings"].append("available release requires semantic review; fixed consumer unchanged")
                item["warnings"].extend("%s:%s (%s)" % (change["file"], change["path"], change["kind"])
                                        for change in differences[:12])
        except ContractError as error:
            exit_code = max(exit_code, 1)
            item["warnings"].append(str(error))
            if release:
                item["last_available_release"] = release
            item["latest_evidence_stale"] = True
        report["upstreams"][kind] = item
    if changed:
        report["checked_at"] = now
        os.makedirs(cache_root, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".release-evidence-", dir=cache_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            os.replace(pending, path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
    return report, exit_code
