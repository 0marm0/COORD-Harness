from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "extract"
sys.path.insert(0, str(TOOLS))
import gate  # noqa: E402


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def initialize_repo(root: Path, files: dict[str, str | bytes], *, authored: set[str] | None = None) -> Path:
    root.mkdir(parents=True)
    write(root / "tools/extract/manifest.json", '{"files": []}\n')
    accounted = authored if authored is not None else set(files)
    write(
        root / "tools/extract/authored.json",
        json.dumps({"files": {name: "test fixture" for name in accounted}, "derived": {}}) + "\n",
    )
    for rel, data in files.items():
        write(root / rel, data)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "noreply" + "@" + "github.com")
    git(root, "add", "-A")
    return root


def commit(
    root: Path, message: str = "fixture", *, env: dict[str, str] | None = None
) -> str:
    git(root, "commit", "-q", "-m", message, env=env)
    return git(root, "rev-parse", "HEAD")


def png(width: int = 1, height: int = 1, *, metadata: bool = False, trailing: bool = False) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    data = gate.PNG_SIGNATURE
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if metadata:
        data += chunk(b"tEXt", b"key\x00value")
    rows = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    data += chunk(b"IDAT", zlib.compress(rows))
    data += chunk(b"IEND", b"")
    return data + (b"trailing" if trailing else b"")


def provenance(rel: str, image: bytes, *, width: int = 1, height: int = 1) -> str:
    return json.dumps(
        {
            "assets": [
                {
                    "path": rel,
                    "sha256": hashlib.sha256(image).hexdigest(),
                    "provenance_class": "synthetic-web-capture",
                    "capture_method": "deterministic fixture renderer",
                    "viewport_or_device": "1x1 test viewport",
                    "deterministic_fixture": "tests/extract/fixture.json",
                    "source_truth": ["tests/extract/fixture.json"],
                    "synthetic": True,
                    "width": width,
                    "height": height,
                }
            ]
        }
    ) + "\n"


def private_path() -> str:
    return "/" + "Users/private"


def test_marker_text_never_exempts_a_match(tmp_path: Path) -> None:
    payload = private_path() + " gate-" + "allow: empty reason\n"
    root = initialize_repo(tmp_path / "repo", {"fixture.txt": payload})
    report = gate.run(root)
    assert any("absolute home path" in item for item in report.patterns)


def test_default_vocabulary_is_hermetic_and_explicit_scan_excludes_definition(tmp_path: Path) -> None:
    root = initialize_repo(
        tmp_path / "repo",
        {
            "safe.txt": "PrivateTerm\n",
            "tools/extract/vocabulary.example.json": json.dumps(
                {"forbidden": [["PrivateTerm", "maintainer term"]]}
            ),
        },
    )
    ignored = root / "tools/extract/vocabulary.json"
    write(ignored, json.dumps({"forbidden": [["PrivateTerm", "maintainer term"]]}))
    assert not gate.run(root).patterns
    report = gate.run(root, vocabulary_path=ignored)
    assert report.patterns == ["safe.txt:1: maintainer term"]


def test_staged_pattern_bytes_cannot_be_masked_by_worktree(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "repo", {"payload.txt": "safe\n"})
    staged_secret = private_path() + "/credential\n"
    write(root / "payload.txt", staged_secret)
    git(root, "add", "payload.txt")
    write(root / "payload.txt", "safe\n")
    assert any("absolute home path" in item for item in gate.run(root).patterns)

    git(root, "add", "payload.txt")
    write(root / "payload.txt", staged_secret)
    assert not gate.run(root).patterns


def test_staged_png_and_provenance_ignore_worktree_replacements(tmp_path: Path) -> None:
    clean = png()
    dirty = png(metadata=True)
    rel = "docs/assets/fixture.png"
    root = initialize_repo(
        tmp_path / "repo",
        {rel: clean, "docs/assets/provenance.json": provenance(rel, clean)},
    )
    write(root / rel, dirty)
    write(root / "docs/assets/provenance.json", provenance(rel, dirty))
    git(root, "add", rel, "docs/assets/provenance.json")
    write(root / rel, clean)
    write(root / "docs/assets/provenance.json", provenance(rel, clean))
    assert any("forbidden metadata" in item for item in gate.run(root).shape)

    git(root, "add", rel, "docs/assets/provenance.json")
    write(root / rel, dirty)
    write(root / "docs/assets/provenance.json", provenance(rel, dirty))
    assert not gate.run(root).shape


def test_staged_authored_and_public_manifests_are_authoritative(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "authored", {"payload.txt": "safe\n"})
    write(root / "tools/extract/authored.json", '{"files": {}, "derived": {}}\n')
    assert not [item for item in gate.run(root).coverage if "payload.txt" in item]
    git(root, "add", "tools/extract/authored.json")
    write(
        root / "tools/extract/authored.json",
        '{"files": {"payload.txt": "fresh"}, "derived": {}}\n',
    )
    assert any("payload.txt: unaccounted" in item for item in gate.run(root).coverage)

    root = initialize_repo(
        tmp_path / "manifest", {"payload.txt": "safe\n"}, authored=set()
    )
    write(
        root / "tools/extract/manifest.json",
        '{"files": [{"dest": "payload.txt"}]}\n',
    )
    git(root, "add", "tools/extract/manifest.json")
    write(root / "tools/extract/manifest.json", '{"files": []}\n')
    assert not [item for item in gate.run(root).coverage if "payload.txt" in item]
    git(root, "add", "tools/extract/manifest.json")
    write(
        root / "tools/extract/manifest.json",
        '{"files": [{"dest": "payload.txt"}]}\n',
    )
    assert any("payload.txt: unaccounted" in item for item in gate.run(root).coverage)


def test_structural_ipv4_allows_127_block_and_bind_all_but_rejects_routable(
    tmp_path: Path,
) -> None:
    root = initialize_repo(
        tmp_path / "repo",
        {"network.txt": "127.9.8.7\n0.0.0.0\n192.168.1.4\n"},
    )
    report = gate.run(root)
    assert report.patterns == ["network.txt:3: bare IPv4 address"]


def test_fake_png_is_rejected_even_with_matching_provenance_hash(tmp_path: Path) -> None:
    image = b"not a png"
    rel = "docs/assets/fake.png"
    root = initialize_repo(
        tmp_path / "repo",
        {rel: image, "docs/assets/provenance.json": provenance(rel, image)},
    )
    report = gate.run(root)
    assert any("invalid PNG signature" in item for item in report.shape)


def test_valid_exact_png_provenance_passes_shape(tmp_path: Path) -> None:
    image = png()
    rel = "docs/assets/fixture.png"
    root = initialize_repo(
        tmp_path / "repo",
        {rel: image, "docs/assets/provenance.json": provenance(rel, image)},
    )
    assert not gate.run(root).shape


def test_png_metadata_and_trailing_bytes_are_rejected(tmp_path: Path) -> None:
    for name, image, expected in (
        ("metadata", png(metadata=True), "forbidden metadata"),
        ("trailing", png(trailing=True), "trailing bytes"),
    ):
        rel = f"docs/assets/{name}.png"
        root = initialize_repo(
            tmp_path / name,
            {rel: image, "docs/assets/provenance.json": provenance(rel, image)},
        )
        assert any(expected in item for item in gate.run(root).shape)


def test_tracked_cache_payload_and_symlink_are_failures(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "repo", {"__pycache__/payload.txt": "safe\n"})
    outside = tmp_path / "outside.txt"
    outside.write_text("safe\n")
    os.symlink(outside, root / "linked.txt")
    git(root, "add", "-f", "__pycache__/payload.txt", "linked.txt")
    report = gate.run(root)
    assert any("skipped/cache" in item for item in report.shape)
    assert any("tracked symlink" in item for item in report.shape)


def test_classification_overlaps_and_unreviewed_derived_are_rejected(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "repo", {"same.txt": "safe\n"})
    write(root / "tools/extract/manifest.json", json.dumps({"files": [{"dest": "same.txt"}]}) + "\n")
    write(
        root / "tools/extract/authored.json",
        json.dumps(
            {
                "files": {"same.txt": "fresh"},
                "derived": {"same.txt": {"origin": "port", "reason": "changed"}},
            }
        ) + "\n",
    )
    git(root, "add", "-A")
    report = gate.run(root)
    assert any("overlaps manifest and authored" in item for item in report.coverage)
    assert any("overlaps manifest and derived" in item for item in report.coverage)
    assert any("reviewed_sha256" in item for item in report.coverage)


def _source_and_dest(tmp_path: Path, *, missing: bool = False, identity: bool = True) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
    write(source / "source.py", "value = 1\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "noreply" + "@" + "github.com")
    git(source, "add", "-A")
    source_commit = commit(source)
    dest = initialize_repo(tmp_path / "dest", {"dest.py": "value = 1\n"}, authored=set())
    write(dest / "tools/extract/manifest.json", json.dumps({"files": [{"dest": "dest.py"}]}) + "\n")
    git(dest, "add", "-A")
    source_manifest = tmp_path / "source-manifest.json"
    doc = {
        "files": [{"source": "missing.py" if missing else "source.py", "dest": "dest.py"}]
    }
    if identity:
        doc["source_snapshot"] = {"commit": source_commit}
    write(source_manifest, json.dumps(doc))
    return source, dest, source_manifest


def test_fidelity_fails_missing_source_and_missing_snapshot_identity(tmp_path: Path) -> None:
    source, dest, source_manifest = _source_and_dest(tmp_path / "missing", missing=True)
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("pinned source path is absent" in item for item in report.fidelity)
    source, dest, source_manifest = _source_and_dest(tmp_path / "identity", identity=False)
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("source_snapshot.commit" in item for item in report.fidelity)


def test_fidelity_reads_pinned_blob_not_dirty_source_worktree(tmp_path: Path) -> None:
    source, dest, source_manifest = _source_and_dest(tmp_path)
    write(source / "source.py", "value = 999\n")
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert not report.fidelity


def test_fidelity_allows_the_source_head_to_advance_past_the_pin(tmp_path: Path) -> None:
    """A source repository under active development must not fail the gate.

    Fidelity reads every blob at the pinned commit, so where the source checkout
    happens to be parked is irrelevant to the comparison. Requiring HEAD to equal
    the pin meant the check could only run while the source repository was idle,
    which for one being worked in is close to never -- so the check that mattered
    was the one that never got to run.
    """
    source, dest, source_manifest = _source_and_dest(tmp_path)
    write(source / "other.txt", "new commit\n")
    git(source, "add", "other.txt")
    commit(source, "advance source head")
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert not report.fidelity, report.fidelity


def test_fidelity_rejects_a_pin_that_no_ref_reaches(tmp_path: Path) -> None:
    """A commit that was reset away is not evidence of a published state.

    It still resolves through `rev-parse --verify`, so the exact-commit check
    cannot tell it apart from a real one. Only reachability can.
    """
    source, dest, source_manifest = _source_and_dest(tmp_path)
    pinned = json.loads(source_manifest.read_text())["source_snapshot"]["commit"]
    # Move the branch off the pin entirely, leaving it dangling but resolvable.
    git(source, "checkout", "-q", "--orphan", "elsewhere")
    git(source, "rm", "-rq", "--cached", ".")
    write(source / "unrelated.txt", "different history\n")
    git(source, "add", "unrelated.txt")
    commit(source, "unrelated root")
    git(source, "branch", "-qD", "main")

    resolved = git(source, "rev-parse", "--verify", f"{pinned}^{{commit}}")
    assert resolved == pinned, "the dangling pin must still resolve, or this proves nothing"

    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("not reachable from any ref" in item for item in report.fidelity), report.fidelity


def test_fidelity_rejects_source_symlink_escape(tmp_path: Path) -> None:
    source, dest, source_manifest = _source_and_dest(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n")
    os.symlink(outside, source / "link.py")
    git(source, "add", "link.py")
    source_commit = commit(source, "link")
    write(
        source_manifest,
        json.dumps({"source_snapshot": {"commit": source_commit}, "files": [{"source": "link.py", "dest": "dest.py"}]}),
    )
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("pinned source entry is a symlink" in item for item in report.fidelity)


def test_fidelity_rejects_pinned_tree_and_undecodable_blob(tmp_path: Path) -> None:
    source, dest, source_manifest = _source_and_dest(tmp_path / "tree")
    write(source / "nested/file.py", "value = 1\n")
    git(source, "add", "nested/file.py")
    source_commit = commit(source, "add tree")
    write(
        source_manifest,
        json.dumps(
            {
                "source_snapshot": {"commit": source_commit},
                "files": [{"source": "nested", "dest": "dest.py"}],
            }
        ),
    )
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("pinned source entry is non-regular" in item for item in report.fidelity)

    source, dest, source_manifest = _source_and_dest(tmp_path / "binary")
    write(source / "source.py", b"\xff\xfe\x00")
    git(source, "add", "source.py")
    source_commit = commit(source, "binary source")
    write(
        source_manifest,
        json.dumps(
            {
                "source_snapshot": {"commit": source_commit},
                "files": [{"source": "source.py", "dest": "dest.py"}],
            }
        ),
    )
    report = gate.run(dest, source, source_manifest_path=source_manifest)
    assert any("pinned source blob is not UTF-8" in item for item in report.fidelity)


def test_reachable_history_scans_deleted_blob_without_echoing_secret(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "repo", {"old.txt": private_path() + "/secret\n"})
    commit(root, "old")
    (root / "old.txt").unlink()
    write(root / "safe.txt", "safe\n")
    authored = {"safe.txt": "current fixture"}
    write(root / "tools/extract/authored.json", json.dumps({"files": authored, "derived": {}}))
    git(root, "add", "-A")
    commit(root, "current")
    report = gate.run(root, history=True)
    joined = "\n".join(report.history)
    assert "absolute home path" in joined
    assert private_path() not in joined


def test_history_scans_each_identity_field_without_echoing_identity(tmp_path: Path) -> None:
    personal_name = "Personal Fixture"
    author_email = "author" + "@" + "corp.example"
    committer_email = "committer" + "@" + "corp.example"
    vocabulary = {
        "forbidden": [
            [personal_name, "personal identity"],
            [author_email, "personal identity"],
            [committer_email, "personal identity"],
        ]
    }
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(vocabulary))
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": personal_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": personal_name,
        "GIT_COMMITTER_EMAIL": committer_email,
    }
    sha = commit(root, env=env)

    report = gate.run(root, vocabulary_path=vocabulary_path, history=True)
    joined = "\n".join(report.history)
    for field in ("author name", "author email", "committer name", "committer email"):
        assert f"history commit {sha} {field}: personal identity" in report.history
    assert personal_name not in joined
    assert author_email not in joined
    assert committer_email not in joined


def test_history_boundary_free_source_token_scans_full_message(tmp_path: Path) -> None:
    token = "widget" + "source"
    vocabulary = {
        "renames": [[rf"\b{token}\b", "neutralname"]],
        "forbidden": [[rf"\b{token}\b", "source project name", "i"]],
    }
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(vocabulary))
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    sha = commit(root, f"Neutral subject\n\nRemove My{token.capitalize()}Client")

    report = gate.run(root, vocabulary_path=vocabulary_path, history=True)
    expected = (
        f"history commit {sha} message: source token 0 occurs "
        "(boundary-free match)"
    )
    assert expected in report.history
    joined = "\n".join(report.history).casefold()
    assert token not in joined and "neutralname" not in joined


def test_history_is_scoped_to_selected_ref_and_neutral_candidate_passes(
    tmp_path: Path, capsys,
) -> None:
    token = "widget" + "source"
    vocabulary = {"renames": [[rf"\b{token}\b", "neutralname"]]}
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(vocabulary))
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    candidate_sha = commit(root, "neutral candidate")
    git(root, "branch", "publication-candidate", candidate_sha)
    git(root, "checkout", "-q", "-b", "excluded-history")
    write(root / "excluded.txt", private_path() + f"/{token}\n")
    git(root, "add", "excluded.txt")
    excluded_sha = commit(root, f"Remove My{token.capitalize()}Client")
    git(root, "checkout", "-q", "publication-candidate")

    candidate = gate.run(
        root,
        vocabulary_path=vocabulary_path,
        history=True,
        ref="publication-candidate",
    )
    assert candidate.history == []

    excluded = gate.run(
        root, vocabulary_path=vocabulary_path, history=True, ref="excluded-history"
    )
    assert any(excluded_sha in item for item in excluded.history)
    assert any("source token 0" in item for item in excluded.history)

    assert (
        gate.main(
            [
                "--root",
                str(root),
                "--vocabulary",
                str(vocabulary_path),
                "--history",
                "--ref",
                "publication-candidate",
            ]
        )
        == 0
    )
    assert "history      : 0 failure(s)" in capsys.readouterr().out


def test_explicit_ref_fails_when_its_tree_differs_from_neutral_index(
    tmp_path: Path, capsys,
) -> None:
    token = "widget" + "source"
    vocabulary = {"renames": [[rf"\b{token}\b", "neutralname"]]}
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(vocabulary))
    root = initialize_repo(
        tmp_path / "repo",
        {"payload.py": f"class My{token.capitalize()}Client:\n    pass\n"},
    )
    leaky_ref = commit(root, "historical candidate")
    write(root / "payload.py", "class NeutralClient:\n    pass\n")
    git(root, "add", "payload.py")

    staged = gate.run(root, vocabulary_path=vocabulary_path)
    assert staged.failures == 0

    selected = gate.run(root, vocabulary_path=vocabulary_path, ref=leaky_ref)
    assert selected.shape == [
        "candidate identity: selected ref and Git index trees differ"
    ]
    assert selected.patterns == []

    assert (
        gate.main(
            [
                "--root",
                str(root),
                "--vocabulary",
                str(vocabulary_path),
                "--ref",
                leaky_ref,
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "NOT PUBLISHABLE" in output
    assert "CANDIDATE CLEAN" not in output


def test_history_defaults_to_head_and_rejects_an_invalid_ref(tmp_path: Path) -> None:
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    commit(root)
    assert gate.run(root, history=True).history == []
    report = gate.run(root, history=True, ref="missing-ref")
    assert report.shape == [
        "candidate identity: selected ref is invalid or not a commit"
    ]
    assert report.history == []


def test_main_never_labels_skipped_release_checks_publishable(
    tmp_path: Path, capsys,
) -> None:
    root = initialize_repo(tmp_path / "repo", {"safe.txt": "safe\n"})
    assert gate.main(["--root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "CANDIDATE CLEAN" in output
    assert "not a publication verdict" in output
    assert "\nPUBLISHABLE" not in output


def test_source_snapshot_must_be_reachable_from_a_ref(tmp_path: Path) -> None:
    """A pinned commit that was reset away must not satisfy the fidelity check.

    `git rev-parse --verify` resolves a dangling commit perfectly well, so the
    exact-commit check it performs cannot tell a published state from an object
    that was created and abandoned. This asserts both halves: the reachable
    commit is accepted, and the dangling one -- which resolves identically -- is
    refused. Without the second half the check could be vacuous and still pass.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid",
    }
    git(tmp_path, "init", "-q", ".", env=env)
    write(tmp_path / "a.txt", "one\n")
    git(tmp_path, "add", "-A", env=env)
    git(tmp_path, "commit", "-qm", "one", env=env)
    reachable = git(tmp_path, "rev-parse", "HEAD", env=env)

    write(tmp_path / "a.txt", "two\n")
    git(tmp_path, "add", "-A", env=env)
    git(tmp_path, "commit", "-qm", "two", env=env)
    dangling = git(tmp_path, "rev-parse", "HEAD", env=env)
    git(tmp_path, "reset", "-q", "--hard", reachable, env=env)

    def contained(commit: str) -> str:
        return git(tmp_path, "for-each-ref", "--contains", commit,
                   "--count=1", "--format=%(refname)", env=env)

    # Both resolve, which is exactly why resolving is not enough.
    assert git(tmp_path, "rev-parse", "--verify", f"{reachable}^{{commit}}", env=env) == reachable
    assert git(tmp_path, "rev-parse", "--verify", f"{dangling}^{{commit}}", env=env) == dangling

    assert contained(reachable), "a committed state must be reachable from a ref"
    assert not contained(dangling), "a reset-away commit must not be reachable"


def test_boundary_free_token_scan_catches_what_word_boundaries_miss(tmp_path: Path) -> None:
    r"""A token in the rename table must not survive in any tracked text.

    The four shapes below are the four escapes this check exists for: a leading
    underscore, a trailing underscore, the token inside a longer identifier, and
    the token adjoined to a capital. Every one of them passed a `forbidden`
    table that already had a rule for the bare word, because neither an
    underscore nor a capital letter is a word boundary. The first assertion
    proves that miss is real rather than assumed -- the `\b`-anchored rule finds
    nothing in this tree -- so the second is not measuring an already-caught
    leak. The clean tree then proves the check can pass at all.
    """
    token = "widget" + "co"
    vocabulary = {
        "renames": [[rf"\b{token}\b", "harnessco"]],
        "forbidden": [[rf"\b{token}\b", "source project name", "i"]],
    }
    leaks = {
        "leading.py": f"_{token} = 1\n",
        "trailing.py": f"{token}_root = 2\n",
        "embedded.py": f"class My{token.capitalize()}Client:\n    pass\n",
        "capital.py": f"{token.upper()}Spec = 3\n",
    }
    root = initialize_repo(tmp_path / "repo", leaks)
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(vocabulary))

    report = gate.run(root, vocabulary_path=vocabulary_path)
    assert not [item for item in report.patterns if "source project name" in item], (
        "the word-boundary rule must miss these, or this test proves nothing"
    )
    assert sorted(report.patterns) == [
        "capital.py:1: source token 0 occurs (boundary-free match)",
        "embedded.py:1: source token 0 occurs (boundary-free match)",
        "leading.py:1: source token 0 occurs (boundary-free match)",
        "trailing.py:1: source token 0 occurs (boundary-free match)",
    ]
    # Reporting what you redact is disclosure: the index locates the rule, the
    # message never spells the token.
    joined = " ".join(report.patterns).casefold()
    assert token not in joined and "harnessco" not in joined

    clean = initialize_repo(
        tmp_path / "clean",
        {"clean.py": "harnessco_root = 2\n", "notes.md": "No source names here.\n"},
    )
    assert gate.run(clean, vocabulary_path=vocabulary_path).patterns == []


def test_short_tokens_and_non_literal_patterns_yield_no_boundary_free_token() -> None:
    """The scan asserts only what the vocabulary concretely names.

    A three-character token as a bare substring fires inside ordinary words, so
    it is skipped; a pattern with no literal core of its own contributes no
    token rather than a guess at one.
    """
    assert gate.source_tokens(((r"\bfoo\b", "bar"),)) == ()
    assert gate.source_tokens(((r"\d{4}-[A-Z]+", "x"),)) == ()
    derived = gate.source_tokens(((r"\bfoo\b", "bar"), (r"\bAlpha\.Beta\b", "y")))
    assert [(item.index, item.text) for item in derived] == [(1, "alpha.beta")]


def test_vocabulary_example_placeholders_do_not_fail_their_own_check(tmp_path: Path) -> None:
    """The shipped example names its placeholders; the check must not fire on it.

    A rule-definition file necessarily contains the strings the rules refuse,
    and a gate that fails on its own vocabulary is a gate nobody can run.
    """
    example = json.loads(
        (TOOLS / "vocabulary.example.json").read_text(encoding="utf-8")
    )
    assert gate.source_tokens(
        tuple((str(rule[0]), str(rule[1])) for rule in example["renames"])
    ), "the example must yield at least one token for this test to mean anything"
    root = initialize_repo(
        tmp_path / "repo",
        {"tools/extract/vocabulary.example.json": json.dumps(example)},
    )
    vocabulary_path = tmp_path / "vocabulary.json"
    write(vocabulary_path, json.dumps(example))
    assert gate.run(root, vocabulary_path=vocabulary_path).patterns == []
