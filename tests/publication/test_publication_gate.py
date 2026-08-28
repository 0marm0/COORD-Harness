from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "publication_gate.py"
_NEUTRAL_NOREPLY = "noreply" + "@" + "github.com"


def _load_gate():
    spec = importlib.util.spec_from_file_location("publication_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return _load_gate()


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _candidate(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    authored = {path: "synthetic publication fixture" for path in files}
    infrastructure = {
        "tools/extract/manifest.json": '{"files": []}\n',
        "tools/extract/authored.json": json.dumps({"files": authored, "derived": {}}) + "\n",
    }
    for path, content in {**files, **infrastructure}.items():
        _write(tmp_path / path, content)
    _git(tmp_path, "add", "-f", "-A")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Coord Harness",
        "GIT_AUTHOR_EMAIL": _NEUTRAL_NOREPLY,
        "GIT_COMMITTER_NAME": "Coord Harness",
        "GIT_COMMITTER_EMAIL": _NEUTRAL_NOREPLY,
    }
    _git(tmp_path, "commit", "-qm", "synthetic candidate", env=env)
    return tmp_path


def _safe_files() -> dict[str, str]:
    return {"README.md": "# Synthetic candidate\n", "src/example.py": "value = 1\n"}


def test_generic_lifecycle_smoke_uses_a_valid_durable_work_id(gate) -> None:
    assert gate._smoke(REPO) == {
        "status": "PASS",
        "fresh_home": True,
        "returncodes": [0, 0, 0],
    }


def test_candidate_manifest_is_exact_stable_and_bound_to_head(tmp_path: Path, gate) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    first = gate.build_report(repo, run_smoke=False)
    second = gate.build_report(repo, run_smoke=False)
    assert first["candidate_manifest"] == second["candidate_manifest"]
    assert first["checks"]["candidate_ref_identity"]["status"] == "PASS"
    assert first["candidate_manifest"]["candidate_tree_oid"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert len(first["candidate_manifest"]["manifest_sha256"]) == 64
    assert first["external_history_gate"]["status"] == "NOT_CHECKED"
    assert first["release_status"] == "NOT_READY"


def test_staged_blob_is_authority_and_worktree_drift_is_separate(
    tmp_path: Path, gate
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    vocabulary = tmp_path / "forbidden.json"
    vocabulary.write_text(
        json.dumps({"forbidden": [["SourcePrivateTerm", "private vocabulary"]]}),
        encoding="utf-8",
    )
    _write(repo / "src/example.py", "SourcePrivateTerm = 1\n")
    _git(repo, "add", "src/example.py")
    _write(repo / "src/example.py", "value = 1\n")
    report = gate.build_report(repo, run_smoke=False, vocabulary_path=vocabulary)
    structural = report["checks"]["exact_object_structural_scan"]
    assert structural["status"] == "FAIL"
    assert any("private vocabulary" in item for item in structural["findings"]["privacy_vocabulary"])
    assert report["checks"]["index_worktree_identity"]["status"] == "FAIL"

    _git(repo, "add", "src/example.py")
    _write(repo / "src/example.py", "SourcePrivateTerm = 1\n")
    report = gate.build_report(repo, run_smoke=False, vocabulary_path=vocabulary)
    assert not report["checks"]["exact_object_structural_scan"]["findings"]["privacy_vocabulary"]
    assert report["checks"]["index_worktree_identity"]["status"] == "FAIL"


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("state.bin", b"SQLite format 3\x00payload", "SQLite database"),
        ("payload.zip", b"PK\x03\x04payload", "ZIP archive"),
    ],
)
def test_opaque_database_and_archive_are_rejected_in_index_and_history(
    tmp_path: Path, gate, path: str, content: bytes, expected: str
) -> None:
    repo = _candidate(tmp_path / "repo", {**_safe_files(), path: content})
    report = gate.build_report(repo, run_smoke=False)
    shape = report["checks"]["exact_object_structural_scan"]["findings"][
        "paths_modes_archives_databases_images"
    ]
    history = report["checks"]["complete_reachable_history_shape"]["findings"]
    assert any(expected.lower().split()[0] in item.lower() for item in [*shape, *history])


def test_symlink_mode_is_rejected_without_following_target(tmp_path: Path, gate) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    outside = tmp_path / "outside.txt"
    outside.write_text("SourcePrivateTerm\n", encoding="utf-8")
    os.symlink(outside, repo / "linked.txt")
    _git(repo, "add", "linked.txt")
    report = gate.build_report(repo, run_smoke=False)
    shape = report["checks"]["exact_object_structural_scan"]["findings"][
        "paths_modes_archives_databases_images"
    ]
    assert any("tracked symlink" in item for item in shape)


def test_complete_history_rejects_deleted_symlink_mode(tmp_path: Path, gate) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    os.symlink(outside, repo / "historical-link")
    authored_path = repo / "tools/extract/authored.json"
    authored = json.loads(authored_path.read_text())
    authored["files"]["historical-link"] = "synthetic historical fixture"
    authored_path.write_text(json.dumps(authored) + "\n")
    _git(repo, "add", "historical-link", "tools/extract/authored.json")
    neutral = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Coord Harness", "GIT_AUTHOR_EMAIL": _NEUTRAL_NOREPLY,
        "GIT_COMMITTER_NAME": "Coord Harness", "GIT_COMMITTER_EMAIL": _NEUTRAL_NOREPLY,
    }
    _git(repo, "commit", "-qm", "synthetic historical mode", env=neutral)
    (repo / "historical-link").unlink()
    authored = json.loads(authored_path.read_text())
    del authored["files"]["historical-link"]
    authored_path.write_text(json.dumps(authored) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove synthetic historical mode", env=neutral)
    history = gate.build_report(repo, run_smoke=False)["checks"]["complete_reachable_history_shape"]
    assert history["status"] == "FAIL"
    assert any("historical blob mode 120000" in item for item in history["findings"])


def test_private_inputs_and_generated_receipts_must_stay_outside_repo(
    tmp_path: Path, gate, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    vocabulary = repo / "private-vocabulary.json"
    vocabulary.write_text('{"forbidden": []}\n')
    with pytest.raises(gate.GateError, match="outside the candidate repository"):
        gate.build_report(repo, run_smoke=False, vocabulary_path=vocabulary)
    monkeypatch.setattr(gate, "build_report", lambda *_args, **_kwargs: {
        "schema": gate.SCHEMA,
        "candidate_manifest": {"schema": gate.MANIFEST_SCHEMA},
        "external_history_receipt_template": {"schema": gate.RECEIPT_SCHEMA},
        "local_status": "PASS", "release_status": "NOT_READY",
    })
    assert gate.main([
        "--repo-root", str(repo), "--local-only",
        "--write-candidate-manifest", str(repo / "candidate-manifest.json"),
    ]) == 3
    assert json.loads(capsys.readouterr().out)["release_status"] == "ERROR"


def test_deleted_history_blob_and_commit_metadata_use_external_vocabulary(
    tmp_path: Path, gate
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    vocabulary = tmp_path / "forbidden.json"
    vocabulary.write_text(
        json.dumps({"forbidden": [["Personal Fixture", "private identity"]]}),
        encoding="utf-8",
    )
    _write(repo / "old.txt", "Personal Fixture\n")
    _git(repo, "add", "old.txt")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Personal Fixture",
        "GIT_AUTHOR_EMAIL": _NEUTRAL_NOREPLY,
        "GIT_COMMITTER_NAME": "Personal Fixture",
        "GIT_COMMITTER_EMAIL": _NEUTRAL_NOREPLY,
    }
    _git(repo, "commit", "-qm", "temporary history", env=env)
    (repo / "old.txt").unlink()
    _git(repo, "add", "-A")
    neutral = {**env, "GIT_AUTHOR_NAME": "Coord Harness", "GIT_COMMITTER_NAME": "Coord Harness"}
    _git(repo, "commit", "-qm", "remove temporary file", env=neutral)
    report = gate.build_report(repo, run_smoke=False, vocabulary_path=vocabulary)
    history = report["checks"]["exact_object_structural_scan"]["findings"][
        "candidate_reachable_history"
    ]
    assert any("private identity" in item for item in history)
    assert "Personal Fixture" not in "\n".join(history)


def test_builtin_secret_scanner_is_explicit_fallback_and_can_be_required(
    tmp_path: Path, gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    monkeypatch.setattr(gate.shutil, "which", lambda _name: None)
    fallback = gate.build_report(repo, run_smoke=False)["checks"]["secret_scan"]
    assert fallback["status"] == "PASS"
    assert fallback["engine"] == "builtin-fallback"
    assert fallback["assurance"] == "FALLBACK"
    required = gate.build_report(
        repo, run_smoke=False, require_gitleaks=True
    )["checks"]["secret_scan"]
    assert required["status"] == "FAIL"
    assert required["required_engine_missing"] == "gitleaks"


def _passing_receipt(template: dict) -> dict:
    receipt = json.loads(json.dumps(template))
    assert receipt["schema"] == "coordharness.external-history-receipt.v2"
    assert receipt["scan"]["ref_tips_source"] == "git ls-remote --refs origin"
    assert receipt["scan"]["reachable_commits_source"] == (
        "git rev-list --stdin from provider ref OIDs"
    )
    assert receipt["scan"]["reachable_objects_source"] == (
        "git rev-list --objects --stdin from provider ref OIDs"
    )
    receipt["remote"].update(
        {
            "provider": "private-fixture",
            "repository": "fixture/repository",
            "all_refs_fetched": True,
            "provider_refs_verified": True,
            "server_owned_refs_checked": True,
            "release_assets_checked": True,
            "forks_and_mirrors_checked": True,
        }
    )
    receipt["scan"].update(
        {
            "status": "PASS",
            "secret_scanner": {"name": "gitleaks", "version": "8.fixture", "status": "PASS"},
            "provider_ref_count": 3,
            "ref_tips_sha256": "1" * 64,
            "reachable_commit_count": 4,
            "reachable_commits_sha256": "2" * 64,
            "reachable_object_count": 620,
            "reachable_objects_sha256": "3" * 64,
        }
    )
    receipt["review"].update(
        {"status": "PASS", "reviewer": "release-maintainer", "completed_at": "2026-08-28T12:00:00Z"}
    )
    return receipt


def test_external_receipt_rejects_noncanonical_ref_source(
    tmp_path: Path, gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    base = gate.build_report(repo, run_smoke=False)
    receipt = _passing_receipt(base["external_history_receipt_template"])
    receipt["scan"]["ref_tips_source"] = "git for-each-ref"
    receipt_path = tmp_path / "external-history-receipt.json"
    receipt_path.write_bytes(gate._canonical(receipt))
    signature = tmp_path / "external-history-receipt.json.sig"
    signature.write_text("fixture", encoding="utf-8")
    allowed = tmp_path / "allowed_signers"
    allowed.write_text("release-maintainer fixture\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_verify_signature", lambda *_args: True)

    report = gate.build_report(
        repo,
        run_smoke=False,
        external_receipt_path=receipt_path,
        external_signature_path=signature,
        allowed_signers_path=allowed,
        signer_identity="release-maintainer",
    )
    external = report["external_history_gate"]
    assert external["status"] == "FAIL"
    assert (
        "scan.ref_tips_source must be git ls-remote --refs origin"
        in external["findings"]
    )

    receipt["scan"]["ref_tips_source"] = gate.REF_TIPS_SOURCE
    receipt["scan"]["ref_tips_sha256"] = gate.EMPTY_SHA256
    receipt_path.write_bytes(gate._canonical(receipt))
    empty_digest = gate.build_report(
        repo,
        run_smoke=False,
        external_receipt_path=receipt_path,
        external_signature_path=signature,
        allowed_signers_path=allowed,
        signer_identity="release-maintainer",
    )["external_history_gate"]
    assert empty_digest["status"] == "FAIL"
    assert (
        "scan.ref_tips_sha256 must not be SHA-256 of empty input"
        in empty_digest["findings"]
    )


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen unavailable")
def test_ready_requires_receipt_bound_to_manifest_and_valid_ssh_signature(
    tmp_path: Path, gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    monkeypatch.setattr(gate, "_smoke", lambda _repo: {"status": "PASS"})
    monkeypatch.setattr(
        gate,
        "_secret_scan",
        lambda *_args, **_kwargs: {
            "status": "PASS", "engine": "gitleaks", "assurance": "EXTERNAL_SCANNER"
        },
    )
    base = gate.build_report(repo)
    receipt_path = tmp_path / "external-history-receipt.json"
    receipt_path.write_bytes(gate._canonical(_passing_receipt(base["external_history_receipt_template"])))

    key = tmp_path / "release_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    signature = Path(str(receipt_path) + ".sig")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", gate.SIGNATURE_NAMESPACE, str(receipt_path)],
        check=True,
        capture_output=True,
    )
    public_key = Path(str(key) + ".pub").read_text(encoding="utf-8").strip()
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"release-maintainer {public_key}\n", encoding="utf-8")

    ready = gate.build_report(
        repo,
        external_receipt_path=receipt_path,
        external_signature_path=signature,
        allowed_signers_path=allowed,
        signer_identity="release-maintainer",
    )
    assert ready["local_status"] == "PASS"
    assert ready["external_history_gate"]["status"] == "PASS"
    assert ready["external_history_gate"]["signature_verified"] is True
    assert ready["release_status"] == "READY"

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["candidate"]["tree_oid"] = "0" * 40
    receipt_path.write_bytes(gate._canonical(receipt))
    tampered = gate.build_report(
        repo,
        external_receipt_path=receipt_path,
        external_signature_path=signature,
        allowed_signers_path=allowed,
        signer_identity="release-maintainer",
    )
    assert tampered["release_status"] == "NOT_READY"
    assert tampered["external_history_gate"]["signature_verified"] is False


def test_cli_writes_canonical_manifest_and_portable_receipt_template(
    tmp_path: Path, gate, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _candidate(tmp_path / "repo", _safe_files())
    monkeypatch.setattr(gate, "build_report", lambda *_args, **_kwargs: {
        "schema": gate.SCHEMA,
        "candidate_manifest": {"schema": gate.MANIFEST_SCHEMA, "manifest_sha256": "a" * 64},
        "external_history_receipt_template": {"schema": gate.RECEIPT_SCHEMA},
        "local_status": "PASS",
        "release_status": "NOT_READY",
    })
    manifest = tmp_path / "candidate.json"
    receipt = tmp_path / "receipt.json"
    assert gate.main([
        "--repo-root", str(repo), "--local-only",
        "--write-candidate-manifest", str(manifest),
        "--write-external-receipt-template", str(receipt),
    ]) == 0
    assert manifest.read_bytes() == gate._canonical(json.loads(manifest.read_text()))
    assert receipt.read_bytes() == gate._canonical(json.loads(receipt.read_text()))
    assert json.loads(capsys.readouterr().out)["release_status"] == "NOT_READY"
