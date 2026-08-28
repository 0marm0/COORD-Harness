#!/usr/bin/env python3
"""Exact Git-object publication gate for a proposed COORD release.

The candidate is Git's index. Candidate bytes are read from indexed blobs; the
worktree is consulted only to prove it has not drifted. READY additionally
requires a maintainer-signed receipt for a complete private-remote scan.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

SCHEMA = "coordharness.publication-gate.v2"
MANIFEST_SCHEMA = "coordharness.candidate-manifest.v1"
RECEIPT_SCHEMA = "coordharness.external-history-receipt.v2"
SIGNATURE_NAMESPACE = "coordharness-release"
REF_TIPS_SOURCE = "git ls-remote --refs origin"
REACHABLE_COMMITS_SOURCE = "git rev-list --stdin from provider ref OIDs"
REACHABLE_OBJECTS_SOURCE = "git rev-list --objects --stdin from provider ref OIDs"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
OID_RE = re.compile(r"[0-9a-f]{40,64}\Z")
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("openai_token", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("credentialed_url", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I)),
)
OPAQUE_MAGIC = (
    (b"SQLite format 3\0", "SQLite database"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"BZh", "bzip2 archive"),
    (b"\xfd7zXZ\0", "xz archive"),
)
OPAQUE_SUFFIXES = {
    ".7z", ".bz2", ".db", ".dmg", ".egg", ".gz", ".jar", ".parquet", ".pdf",
    ".pkl", ".rar", ".safetensors", ".sqlite", ".sqlite3", ".tar", ".tgz",
    ".whl", ".xz", ".zip",
}


class GateError(RuntimeError):
    pass


def _load_extract_gate():
    path = Path(__file__).resolve().parent / "extract" / "gate.py"
    spec = importlib.util.spec_from_file_location("_coord_extract_gate", path)
    if spec is None or spec.loader is None:
        raise GateError("unable to load structural extraction gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXTRACT = _load_extract_gate()


def _run(command: list[str], repo: Path, *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=repo, input=data, capture_output=True, check=False)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return _run(["git", *args], repo)


def _git_text(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode:
        raise GateError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", "strict")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _index(repo: Path):
    report = EXTRACT.Report()
    entries = EXTRACT.indexed_paths(repo, report)
    blobs = EXTRACT._indexed_blobs(repo, entries, report)
    return entries, blobs, report.shape


def _manifest(repo: Path, entries, blobs: dict[str, bytes]) -> dict[str, Any]:
    rows = []
    for entry in entries:
        blob = blobs.get(entry.rel)
        rows.append({
            "path": entry.rel,
            "mode": entry.mode,
            "oid": entry.oid,
            "stage": entry.stage,
            "bytes": len(blob) if blob is not None else None,
            "sha256": _sha256(blob) if blob is not None else None,
        })
    core = {
        "schema": MANIFEST_SCHEMA,
        "candidate_tree_oid": _git_text(repo, "write-tree").strip(),
        "entries": rows,
    }
    return {**core, "manifest_sha256": _sha256(_canonical(core))}


def _ref_identity(repo: Path, ref: str, tree_oid: str) -> dict[str, Any]:
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if commit.returncode:
        return {"status": "FAIL", "ref": ref, "reason": "candidate ref is not a commit"}
    commit_oid = commit.stdout.decode().strip()
    ref_tree = _git_text(repo, "rev-parse", "--verify", f"{commit_oid}^{{tree}}").strip()
    match = ref_tree == tree_oid
    return {
        "status": "PASS" if match else "FAIL",
        "ref": ref,
        "commit_oid": commit_oid,
        "tree_oid": ref_tree,
        "index_tree_oid": tree_oid,
        "reason": None if match else "candidate ref tree differs from Git index tree",
    }


def _worktree_identity(repo: Path) -> dict[str, Any]:
    changed = _git(repo, "diff", "--no-ext-diff", "--name-only", "-z")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if changed.returncode or untracked.returncode:
        return {"status": "FAIL", "findings": ["unable to compare index and worktree"]}
    findings = [
        {"path": item.decode("utf-8", "replace"), "reason": reason}
        for payload, reason in ((changed.stdout, "unstaged drift"), (untracked.stdout, "untracked path"))
        for item in payload.split(b"\0") if item
    ]
    findings.sort(key=lambda item: (item["path"], item["reason"]))
    return {"status": "PASS" if not findings else "FAIL", "findings": findings}


def _external_path(repo: Path, path: Path | None, label: str, *, must_exist: bool = True) -> None:
    if path is None:
        return
    resolved = path.resolve(strict=False)
    if resolved == repo or resolved.is_relative_to(repo):
        raise GateError(f"{label} must remain outside the candidate repository")
    if must_exist and (path.is_symlink() or not path.is_file()):
        raise GateError(f"{label} must be an external regular non-symlink file")


def _vocabulary_digest(path: Path | None) -> str:
    if path is None:
        return _sha256(b"")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"external forbidden vocabulary is invalid: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("forbidden"), list):
        raise GateError("external forbidden vocabulary requires a forbidden list")
    return _sha256(_canonical(value))


def _structural_report(repo: Path, vocabulary: Path | None, candidate_ref: str) -> dict[str, Any]:
    report = EXTRACT.run(repo, vocabulary_path=vocabulary, history=True, ref=candidate_ref)
    checks = {
        "coverage": report.coverage,
        "fidelity": report.fidelity,
        "privacy_vocabulary": report.patterns,
        "paths_modes_archives_databases_images": report.shape,
        "candidate_reachable_history": report.history,
    }
    failures = sum(len(value) for value in checks.values())
    return {
        "status": "PASS" if failures == 0 else "FAIL",
        "files_scanned": report.files_scanned,
        "findings": checks,
    }


def _complete_history_shape(repo: Path, vocabulary: Path | None) -> dict[str, Any]:
    result = _git(repo, "rev-list", "--objects", "--all")
    if result.returncode:
        return {"status": "FAIL", "findings": ["unable to enumerate reachable objects"]}
    findings: list[str] = []
    objects: set[str] = set()
    commits = set(_git_text(repo, "rev-list", "--all").splitlines())
    for raw in result.stdout.splitlines():
        oid_raw, _, path_raw = raw.partition(b" ")
        oid = oid_raw.decode("ascii", "replace")
        if not OID_RE.fullmatch(oid) or oid in objects:
            continue
        objects.add(oid)
        if not path_raw:
            continue
        path = path_raw.decode("utf-8", "replace")
        kind = _git(repo, "cat-file", "-t", oid)
        if kind.returncode or kind.stdout.strip() != b"blob":
            continue
        blob = _git(repo, "cat-file", "blob", oid)
        if blob.returncode:
            findings.append(f"{oid[:12]}:{path}: unreadable reachable blob")
            continue
        suffix = Path(path).suffix.lower()
        for magic, label in OPAQUE_MAGIC:
            if blob.stdout.startswith(magic):
                findings.append(f"{oid[:12]}:{path}: {label}")
                break
        if suffix in OPAQUE_SUFFIXES:
            findings.append(f"{oid[:12]}:{path}: opaque suffix {suffix}")
        if path.lower().endswith((".png", ".jpg", ".jpeg")) and len(blob.stdout) > EXTRACT.MAX_IMAGE_BYTES:
            findings.append(f"{oid[:12]}:{path}: oversized historical image")
        if path.lower().endswith(".png"):
            _dimensions, image_error = EXTRACT._png_dimensions(blob.stdout)
            if image_error:
                findings.append(f"{oid[:12]}:{path}: historical {image_error}")
    try:
        vocabulary_doc = EXTRACT.port.load_vocabulary(vocabulary) if vocabulary else {}
        _renames, forbidden = EXTRACT.port.compile_vocabulary(vocabulary_doc)
    except (OSError, ValueError, re.error):
        forbidden = EXTRACT.port.compile_vocabulary({})[1]
    for commit in commits:
        tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
        if tree.returncode:
            findings.append(f"{commit[:12]}: unreadable history tree")
            continue
        for record in tree.stdout.split(b"\0"):
            if not record:
                continue
            try:
                meta, raw_path = record.split(b"\t", 1)
                mode, kind, _oid = meta.decode("ascii").split(" ")
                tree_path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                findings.append(f"{commit[:12]}: malformed history tree entry")
                continue
            path_findings: list[str] = []
            EXTRACT._scan_text(tree_path, f"history path {commit[:12]}", forbidden, path_findings)
            findings.extend(path_findings)
            if kind != "blob" or mode not in {"100644", "100755"}:
                findings.append(f"{commit[:12]}:{tree_path}: historical {kind} mode {mode}")
    findings = sorted(set(findings))
    return {
        "status": "PASS" if not findings else "FAIL",
        "scope": "all local refs (--all)",
        "reachable_commits": len(commits),
        "reachable_objects": len(objects),
        "findings": findings,
    }


def _fallback_secrets(repo: Path, entries, blobs: dict[str, bytes]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    candidates = [(entry.rel, blobs.get(entry.rel)) for entry in entries]
    seen: set[str] = set()
    revs = _git(repo, "rev-list", "--objects", "--all")
    if revs.returncode:
        return {"status": "FAIL", "engine": "builtin-fallback", "findings": ["history unavailable"]}
    for raw in revs.stdout.splitlines():
        oid_raw, _, _path = raw.partition(b" ")
        oid = oid_raw.decode("ascii", "replace")
        if not OID_RE.fullmatch(oid) or oid in seen:
            continue
        seen.add(oid)
        kind = _git(repo, "cat-file", "-t", oid)
        if kind.returncode or kind.stdout.strip() != b"blob":
            continue
        blob = _git(repo, "cat-file", "blob", oid)
        if blob.returncode == 0:
            candidates.append((f"history blob {oid[:12]}", blob.stdout))
    for location, data in candidates:
        if data is None or b"\0" in data[:8192] or len(data) > EXTRACT.MAX_BYTES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line, value in enumerate(text.splitlines(), 1):
            for category, pattern in SECRET_PATTERNS:
                if pattern.search(value):
                    findings.append({"location": location, "line": line, "category": category})
    findings.sort(key=lambda item: (item["location"], item["line"], item["category"]))
    return {
        "status": "PASS" if not findings else "FAIL",
        "engine": "builtin-fallback",
        "assurance": "FALLBACK",
        "findings": findings,
        "limitations": "Common token formats only; release CI and READY receipts require an external scanner.",
    }


def _gitleaks(repo: Path) -> dict[str, Any]:
    executable = shutil.which("gitleaks")
    if executable is None:
        raise FileNotFoundError
    with tempfile.TemporaryDirectory(prefix="coord-release-index-") as raw:
        checkout = Path(raw) / "candidate"
        checkout.mkdir()
        exported = _git(repo, "checkout-index", "--all", f"--prefix={checkout}{os.sep}")
        if exported.returncode:
            return {"status": "FAIL", "engine": "gitleaks", "error": "index export failed"}
        commands = (
            [executable, "dir", str(checkout), "--redact", "--report-format", "json"],
            [executable, "git", str(repo), "--redact", "--report-format", "json", "--log-opts=--all"],
        )
        count = 0
        for command in commands:
            result = _run(command, repo)
            if result.returncode not in {0, 1}:
                return {
                    "status": "FAIL", "engine": "gitleaks",
                    "error": result.stderr.decode("utf-8", "replace")[-500:],
                }
            try:
                payload = json.loads(result.stdout or b"[]")
                count += len(payload) if isinstance(payload, list) else int(result.returncode == 1)
            except json.JSONDecodeError:
                count += int(result.returncode == 1)
        version = _run([executable, "version"], repo).stdout.decode(errors="replace").strip()
        return {
            "status": "PASS" if count == 0 else "FAIL", "engine": "gitleaks",
            "assurance": "EXTERNAL_SCANNER", "version": version, "findings_count": count,
        }


def _secret_scan(repo: Path, entries, blobs: dict[str, bytes], require_gitleaks: bool) -> dict[str, Any]:
    try:
        return _gitleaks(repo)
    except FileNotFoundError:
        fallback = _fallback_secrets(repo, entries, blobs)
        if require_gitleaks:
            fallback["status"] = "FAIL"
            fallback["required_engine_missing"] = "gitleaks"
        return fallback


def _smoke(repo: Path) -> dict[str, Any]:
    smoke_work_id = "DEMO-CDX-PUBLICATION-SMOKE-R1"
    with tempfile.TemporaryDirectory(prefix="coord-release-smoke-") as raw:
        root = Path(raw)
        env = {
            **os.environ, "HOME": str(root / "home"), "PYTHONPATH": str(repo / "src"),
            "COORD_PROJECT_ROOT": str(root), "COORD_HOME": str(root / ".coordharness"),
            "COORD_DEPLOYMENT_PROFILE": "generic", "COORD_ACTOR": "codex",
            "COORD_SESSION_ID": "codex:release-smoke", "CODEX_SESSION_ID": "codex:release-smoke",
        }
        subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
        base = [sys.executable, "-m", "coordharness.entry", "--db", str(root / ".coordharness/coord.db")]
        commands = [
            [*base, "create", smoke_work_id, "--title", "Synthetic release smoke",
             "--module", "publication", "--surface", "job", "--done-signal", "proof.json",
             "--acceptance", "Synthetic proof exists", "--note", "Clean-room fixture"],
            [*base, "claim", smoke_work_id, "--step", "synthetic acceptance"],
            [*base, "board"],
        ]
        codes = [subprocess.run(command, cwd=root, env=env, capture_output=True).returncode for command in commands]
        return {"status": "PASS" if not any(codes) else "FAIL", "fresh_home": True, "returncodes": codes}


def _receipt_template(manifest: dict[str, Any], ref: dict[str, Any], vocabulary_sha256: str) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "candidate": {
            "commit_oid": ref.get("commit_oid"),
            "tree_oid": manifest["candidate_tree_oid"],
            "manifest_sha256": manifest["manifest_sha256"],
        },
        "remote": {
            "provider": "REPLACE_WITH_PROVIDER",
            "repository": "REPLACE_WITH_PRIVATE_REMOTE_ID",
            "visibility": "private",
            "all_refs_fetched": False,
            "provider_refs_verified": False,
            "server_owned_refs_checked": False,
            "release_assets_checked": False,
            "forks_and_mirrors_checked": False,
        },
        "scan": {
            "status": "NOT_CHECKED",
            "secret_scanner": {"name": "gitleaks", "version": "REPLACE", "status": "NOT_CHECKED"},
            "forbidden_vocabulary_sha256": vocabulary_sha256,
            "provider_ref_count": 0,
            "ref_tips_source": REF_TIPS_SOURCE,
            "ref_tips_sha256": "REPLACE_WITH_SHA256",
            "reachable_commit_count": 0,
            "reachable_commits_source": REACHABLE_COMMITS_SOURCE,
            "reachable_commits_sha256": "REPLACE_WITH_SHA256",
            "reachable_object_count": 0,
            "reachable_objects_source": REACHABLE_OBJECTS_SOURCE,
            "reachable_objects_sha256": "REPLACE_WITH_SHA256",
        },
        "review": {
            "status": "NOT_CHECKED",
            "reviewer": "REPLACE_WITH_SIGNER_IDENTITY",
            "completed_at": "REPLACE_WITH_RFC3339_UTC",
        },
    }


def _verify_signature(repo: Path, raw: bytes, signature: Path, allowed: Path, identity: str) -> bool:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        return False
    result = _run([
        executable, "-Y", "verify", "-f", str(allowed), "-I", identity,
        "-n", SIGNATURE_NAMESPACE, "-s", str(signature),
    ], repo, data=raw)
    return result.returncode == 0


def _external_receipt(
    repo: Path, path: Path | None, signature: Path | None, allowed: Path | None,
    identity: str | None, manifest: dict[str, Any], ref: dict[str, Any], vocabulary_sha256: str,
) -> dict[str, Any]:
    if path is None:
        return {"status": "NOT_CHECKED", "required": True, "reason": "signed remote receipt not supplied"}
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "required": True, "findings": [f"receipt unreadable: {exc}"]}
    findings: list[str] = []
    expected = {
        "commit_oid": ref.get("commit_oid"),
        "tree_oid": manifest["candidate_tree_oid"],
        "manifest_sha256": manifest["manifest_sha256"],
    }
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        findings.append("receipt schema mismatch")
        value = value if isinstance(value, dict) else {}
    if value.get("candidate") != expected:
        findings.append("candidate identity differs from frozen manifest")
    remote = value.get("remote") if isinstance(value.get("remote"), dict) else {}
    for key in ("provider", "repository"):
        if not isinstance(remote.get(key), str) or not remote[key] or remote[key].startswith("REPLACE"):
            findings.append(f"remote.{key} is missing")
    if remote.get("visibility") != "private":
        findings.append("remote.visibility must be private")
    for key in (
        "all_refs_fetched", "provider_refs_verified", "server_owned_refs_checked",
        "release_assets_checked", "forks_and_mirrors_checked",
    ):
        if remote.get(key) is not True:
            findings.append(f"remote.{key} must be true")
    scan = value.get("scan") if isinstance(value.get("scan"), dict) else {}
    secret = scan.get("secret_scanner") if isinstance(scan.get("secret_scanner"), dict) else {}
    if scan.get("status") != "PASS":
        findings.append("scan.status must be PASS")
    if secret.get("status") != "PASS" or secret.get("name") in {None, "", "builtin-fallback"}:
        findings.append("remote secret scanner must be an identified external scanner with PASS")
    if scan.get("forbidden_vocabulary_sha256") != vocabulary_sha256:
        findings.append("forbidden vocabulary digest differs")
    expected_sources = {
        "ref_tips_source": REF_TIPS_SOURCE,
        "reachable_commits_source": REACHABLE_COMMITS_SOURCE,
        "reachable_objects_source": REACHABLE_OBJECTS_SOURCE,
    }
    for key, expected_source in expected_sources.items():
        if scan.get(key) != expected_source:
            findings.append(f"scan.{key} must be {expected_source}")
    for key in ("provider_ref_count", "reachable_commit_count", "reachable_object_count"):
        field_value = scan.get(key)
        if type(field_value) is not int or field_value <= 0:
            findings.append(f"scan.{key} must be a positive integer")
    for key in ("ref_tips_sha256", "reachable_commits_sha256", "reachable_objects_sha256"):
        field_value = scan.get(key)
        if not isinstance(field_value, str) or not SHA256_RE.fullmatch(field_value):
            findings.append(f"scan.{key} must be a lowercase SHA-256")
        elif field_value == EMPTY_SHA256:
            findings.append(f"scan.{key} must not be SHA-256 of empty input")
    review = value.get("review") if isinstance(value.get("review"), dict) else {}
    if review.get("status") != "PASS" or not isinstance(review.get("reviewer"), str):
        findings.append("review must identify a reviewer and report PASS")
    if identity and review.get("reviewer") != identity:
        findings.append("review.reviewer must equal the SSH signer identity")
    completed_at = review.get("completed_at")
    if not isinstance(completed_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", completed_at
    ):
        findings.append("review.completed_at must be RFC3339 UTC")
    if not isinstance(secret.get("version"), str) or not secret["version"] or secret["version"].startswith("REPLACE"):
        findings.append("remote secret scanner version is missing")
    verified = bool(signature and allowed and identity and _verify_signature(repo, raw, signature, allowed, identity))
    if not verified:
        findings.append("valid SSH signature and allowed signer identity are required")
    return {
        "status": "PASS" if not findings else "FAIL", "required": True,
        "receipt_sha256": _sha256(raw), "signature_verified": verified,
        "signer_identity": identity, "findings": findings,
    }


def build_report(
    repo: Path, *, run_smoke: bool = True, candidate_ref: str = "HEAD",
    vocabulary_path: Path | None = None, external_receipt_path: Path | None = None,
    external_signature_path: Path | None = None, allowed_signers_path: Path | None = None,
    signer_identity: str | None = None, require_gitleaks: bool = False,
) -> dict[str, Any]:
    repo = repo.resolve()
    if _git(repo, "rev-parse", "--git-dir").returncode:
        raise GateError(f"not a Git worktree: {repo}")
    _external_path(repo, vocabulary_path, "forbidden vocabulary")
    _external_path(repo, external_receipt_path, "external history receipt")
    _external_path(repo, external_signature_path, "external history signature")
    _external_path(repo, allowed_signers_path, "allowed signers file")
    vocabulary_sha256 = _vocabulary_digest(vocabulary_path)
    entries, blobs, index_findings = _index(repo)
    manifest = _manifest(repo, entries, blobs)
    ref = _ref_identity(repo, candidate_ref, manifest["candidate_tree_oid"])
    structural = _structural_report(repo, vocabulary_path, candidate_ref)
    if index_findings:
        structural["status"] = "FAIL"
        structural["findings"]["index"] = index_findings
    checks = {
        "candidate_ref_identity": ref,
        "index_worktree_identity": _worktree_identity(repo),
        "exact_object_structural_scan": structural,
        "complete_reachable_history_shape": _complete_history_shape(repo, vocabulary_path),
        "secret_scan": _secret_scan(repo, entries, blobs, require_gitleaks),
        "generic_lifecycle_smoke": _smoke(repo) if run_smoke else {
            "status": "NOT_CHECKED", "reason": "disabled by caller"
        },
    }
    local_status = "PASS" if all(item["status"] == "PASS" for item in checks.values()) else "FAIL"
    template = _receipt_template(manifest, ref, vocabulary_sha256)
    external = _external_receipt(
        repo, external_receipt_path, external_signature_path, allowed_signers_path,
        signer_identity, manifest, ref, vocabulary_sha256,
    )
    return {
        "schema": SCHEMA, "candidate_manifest": manifest,
        "forbidden_vocabulary_sha256": vocabulary_sha256,
        "local_status": local_status,
        "release_status": "READY" if local_status == "PASS" and external["status"] == "PASS" else "NOT_READY",
        "checks": checks, "external_history_gate": external,
        "external_history_receipt_template": template,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate-ref", default="HEAD")
    parser.add_argument("--forbidden-vocabulary", type=Path)
    parser.add_argument("--external-history-receipt", type=Path)
    parser.add_argument("--external-history-signature", type=Path)
    parser.add_argument("--allowed-signers", type=Path)
    parser.add_argument("--signer-identity")
    parser.add_argument("--write-candidate-manifest", type=Path)
    parser.add_argument("--write-external-receipt-template", type=Path)
    parser.add_argument("--require-gitleaks", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.repo_root, run_smoke=not args.skip_smoke, candidate_ref=args.candidate_ref,
            vocabulary_path=args.forbidden_vocabulary,
            external_receipt_path=args.external_history_receipt,
            external_signature_path=args.external_history_signature,
            allowed_signers_path=args.allowed_signers, signer_identity=args.signer_identity,
            require_gitleaks=args.require_gitleaks,
        )
        _external_path(args.repo_root.resolve(), args.write_candidate_manifest, "candidate manifest output", must_exist=False)
        _external_path(args.repo_root.resolve(), args.write_external_receipt_template, "receipt template output", must_exist=False)
        if args.write_candidate_manifest:
            args.write_candidate_manifest.write_bytes(_canonical(report["candidate_manifest"]))
        if args.write_external_receipt_template:
            args.write_external_receipt_template.write_bytes(
                _canonical(report["external_history_receipt_template"])
            )
    except (GateError, OSError) as exc:
        print(json.dumps({"schema": SCHEMA, "release_status": "ERROR", "error": str(exc)}, sort_keys=True))
        return 3
    print(json.dumps(report, sort_keys=True, indent=None if args.compact else 2))
    if args.local_only:
        return 0 if report["local_status"] == "PASS" else 1
    return 0 if report["release_status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
