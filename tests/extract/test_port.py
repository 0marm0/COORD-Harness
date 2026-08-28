from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "extract"
sys.path.insert(0, str(TOOLS))
import port  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit_source(root: Path, message: str = "source fixture") -> str:
    if not (root / ".git").exists():
        git(root, "init", "-q")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "noreply" + "@" + "github.com")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def manifest(
    path: Path, files: list[dict[str, object]], *, source_commit: str | None = None,
) -> Path:
    document: dict[str, object] = {"files": files}
    if source_commit is not None:
        document["source_snapshot"] = {"commit": source_commit}
    write(path, json.dumps(document))
    return path


@pytest.mark.parametrize(
    "entry",
    [
        {"source": "/absolute.py", "dest": "out.py"},
        {"source": "in.py", "dest": "../escape.py"},
        {"source": "in.py", "dest": "/absolute.py"},
    ],
)
def test_manifest_rejects_absolute_and_traversal_paths(tmp_path: Path, entry: dict) -> None:
    path = manifest(tmp_path / "manifest.json", [entry])
    with pytest.raises(ValueError):
        port.load_manifest(path)


def test_manifest_rejects_duplicate_destinations(tmp_path: Path) -> None:
    path = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.py", "dest": "out.py"}, {"source": "two.py", "dest": "out.py"}],
    )
    with pytest.raises(ValueError, match="duplicate destination"):
        port.load_manifest(path)


def test_literal_edit_requires_exact_cardinality_and_supports_explicit_count() -> None:
    text, result = port.port_text(
        "token token\n", dest=Path("out.txt"), is_python=False,
        edits=[{"find": "token", "with": "safe"}],
    )
    assert text == "token token\n"
    assert any("cardinality mismatch" in item for item in result.violations)

    text, result = port.port_text(
        "token token\n", dest=Path("out.txt"), is_python=False,
        edits=[{"find": "token", "with": "safe", "count": 2}],
    )
    assert text == "safe safe\n"
    assert not result.violations


@pytest.mark.parametrize(
    "source",
    ["alpha = beta = 1\nkeep = 2\n", "alpha, beta = (1, 2)\nkeep = 2\n"],
)
def test_partial_multi_target_drop_is_rejected(source: str) -> None:
    text, result = port.port_text(
        source, dest=Path("out.py"), is_python=True, drop=["alpha"]
    )
    assert source.splitlines()[0] in text
    assert any("partial drop_symbol assignment rejected" in item for item in result.violations)

    text, result = port.port_text(
        source, dest=Path("out.py"), is_python=True, drop=["alpha", "beta"]
    )
    assert source.splitlines()[0] not in text
    assert "keep = 2" in text
    assert not result.violations


def test_public_manifest_omits_dictionary_testable_private_fingerprints() -> None:
    source_path = "private/source/module.py"
    find_literal = "PRIVATE_LITERAL_FOR_REVIEW"
    public = port.redact_manifest(
        [
            {
                "source": source_path,
                "dest": "src/module.py",
                "reason": "generic import closure",
                "drop_symbols": ["PRIVATE_ALPHA", "PRIVATE_BETA"],
                "edits": [
                    {
                        "find": find_literal,
                        "with": "safe",
                        "count": 2,
                        "reason": "remove private literal",
                    }
                ],
            }
        ]
    )
    serialized = json.dumps(public, sort_keys=True)
    assert source_path not in serialized
    assert find_literal not in serialized
    assert hashlib.sha256(source_path.encode()).hexdigest()[:16] not in serialized
    assert hashlib.sha256(find_literal.encode()).hexdigest()[:16] not in serialized
    entry = public["files"][0]
    assert "source_sha256" not in entry
    assert "find_sha256" not in entry["edits"][0]
    assert "PRIVATE_ALPHA" not in serialized
    assert "PRIVATE_BETA" not in serialized
    assert "drop_symbols" not in entry
    assert entry["dropped_symbol_count"] == 2
    assert entry["drop_category"] == "reviewed top-level symbol removal"
    assert entry["edits"][0]["count"] == 2
    assert entry["edits"][0]["reason"] == "remove private literal"


def test_empty_find_literal_is_rejected() -> None:
    _, result = port.port_text(
        "safe\n", dest=Path("out.txt"), is_python=False, edits=[{"find": ""}]
    )
    assert any("empty/non-string" in item for item in result.violations)


def test_source_and_destination_symlink_escapes_publish_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    source.mkdir()
    dest.mkdir()
    outside.mkdir()
    write(outside / "source.py", "value = 1\n")
    os.symlink(outside / "source.py", source / "link.py")
    source_commit = commit_source(source, "symlink source")
    working = manifest(
        tmp_path / "source-manifest.json",
        [{"source": "link.py", "dest": "out.py"}],
        source_commit=source_commit,
    )
    assert port.run(working, source, dest, dry_run=False) == 1
    assert not (dest / "out.py").exists()
    assert not (dest / "tools/extract/manifest.json").exists()
    assert not (dest / "tools/extract/port_receipt.json").exists()

    write(source / "safe.py", "value = 1\n")
    os.symlink(outside, dest / "linked")
    source_commit = commit_source(source, "safe source")
    working = manifest(
        tmp_path / "dest-manifest.json",
        [{"source": "safe.py", "dest": "linked/out.py"}],
        source_commit=source_commit,
    )
    assert port.run(working, source, dest, dry_run=False) == 1
    assert not (outside / "out.py").exists()


def test_any_transform_failure_publishes_no_outputs(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    write(source / "one.txt", "token token\n")
    source_commit = commit_source(source)
    working = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.txt", "dest": "out.txt", "edits": [{"find": "token"}]}],
        source_commit=source_commit,
    )
    assert port.run(working, source, dest, dry_run=False) == 1
    assert not (dest / "out.txt").exists()
    assert not (dest / "tools/extract/manifest.json").exists()
    assert not (dest / "tools/extract/port_receipt.json").exists()


def test_dirty_source_worktree_cannot_change_pinned_port_bytes(tmp_path: Path) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    write(source / "one.txt", "pinned\n")
    source_commit = commit_source(source)
    working = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.txt", "dest": "out.txt"}],
        source_commit=source_commit,
    )
    write(source / "one.txt", "dirty worktree\n")
    assert port.run(working, source, dest, dry_run=False) == 0
    assert (dest / "out.txt").read_text() == "pinned\n"


def test_public_receipt_uses_relative_dest_and_omits_source_or_stage_oracles(
    tmp_path: Path,
) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    write(source / "one.txt", "pinned receipt bytes\n")
    source_commit = commit_source(source)
    working = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.txt", "dest": "nested/out.txt"}],
        source_commit=source_commit,
    )
    assert port.run(working, source, dest, dry_run=False) == 0
    receipt_path = dest / "tools/extract/port_receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt[0]["dest"] == "nested/out.txt"
    assert "source" not in receipt[0]
    assert "source_sha256" not in receipt[0]
    serialized = receipt_path.read_text()
    assert str(tmp_path) not in serialized
    assert ".coord-port-stage-" not in serialized
    assert "/" + "Users/" not in serialized


def test_pinned_missing_tree_and_non_utf8_sources_fail_closed(tmp_path: Path) -> None:
    for case in ("missing", "tree", "binary"):
        source, dest = tmp_path / case / "source", tmp_path / case / "dest"
        source.mkdir(parents=True)
        dest.mkdir()
        write(source / "safe.txt", "safe\n")
        if case == "tree":
            write(source / "nested/file.txt", "safe\n")
        elif case == "binary":
            (source / "binary.txt").write_bytes(b"\xff\xfe")
        source_commit = commit_source(source)
        source_rel = {
            "missing": "absent.txt", "tree": "nested", "binary": "binary.txt",
        }[case]
        working = manifest(
            tmp_path / case / "manifest.json",
            [{"source": source_rel, "dest": "out.txt"}],
            source_commit=source_commit,
        )
        assert port.run(working, source, dest, dry_run=False) == 1
        assert not (dest / "out.txt").exists()
        assert not (dest / "tools/extract/manifest.json").exists()
        assert not (dest / "tools/extract/port_receipt.json").exists()


def test_source_head_may_advance_but_the_pin_must_stay_reachable(tmp_path: Path) -> None:
    """Porting reads pinned blobs, so the source checkout may be anywhere.

    Requiring HEAD to equal the pin meant the tool refused to run whenever
    anyone was working in the source repository. What still has to hold is that
    the pin is a published state: `rev-parse --verify` resolves a commit that
    was reset away just as happily as a real one, so reachability is the check
    that does the work.
    """
    source = tmp_path / "source"
    source.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@invalid"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main", "."], cwd=source, check=True, env=env
    )
    (source / "a.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=source, check=True, env=env)
    pinned = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True,
                            text=True, check=True, env=env).stdout.strip()

    # HEAD advances past the pin: still valid, because blobs are read by commit.
    (source / "a.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=source, check=True, env=env)
    port._validate_source_snapshot(source, pinned)

    # The pin is reset away entirely: it still resolves, and must be refused.
    subprocess.run(["git", "checkout", "-q", "--orphan", "elsewhere"], cwd=source, check=True, env=env)
    subprocess.run(["git", "rm", "-rq", "--cached", "."], cwd=source, check=True, env=env)
    (source / "b.py").write_text("value = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=source, check=True, env=env)
    subprocess.run(["git", "branch", "-qD", "main"], cwd=source, check=True, env=env)

    still_resolves = subprocess.run(["git", "rev-parse", "--verify", f"{pinned}^{{commit}}"],
                                    cwd=source, capture_output=True, text=True, env=env)
    assert still_resolves.stdout.strip() == pinned, "the dangling pin must resolve, or this proves nothing"

    with pytest.raises(port.PortRefusal):
        port._validate_source_snapshot(source, pinned)


def test_partial_publish_failure_rolls_back_all_destinations_and_receipts(tmp_path: Path, monkeypatch) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    write(source / "one.txt", "one\n")
    write(source / "two.txt", "two\n")
    source_commit = commit_source(source)
    working = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.txt", "dest": "one.txt"}, {"source": "two.txt", "dest": "two.txt"}],
        source_commit=source_commit,
    )
    real_replace = os.replace
    calls = 0

    def fail_second(source_path, dest_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        return real_replace(source_path, dest_path)

    monkeypatch.setattr(port, "_atomic_replace", fail_second)
    assert port.run(working, source, dest, dry_run=False) == 1
    assert not (dest / "one.txt").exists()
    assert not (dest / "two.txt").exists()
    assert not (dest / "tools/extract/manifest.json").exists()
    assert not (dest / "tools/extract/port_receipt.json").exists()


def test_failure_restores_preexisting_destination_bytes(tmp_path: Path, monkeypatch) -> None:
    source, dest = tmp_path / "source", tmp_path / "dest"
    source.mkdir()
    dest.mkdir()
    write(source / "one.txt", "new one\n")
    write(source / "two.txt", "new two\n")
    write(dest / "one.txt", "old one\n")
    source_commit = commit_source(source)
    working = manifest(
        tmp_path / "manifest.json",
        [{"source": "one.txt", "dest": "one.txt"}, {"source": "two.txt", "dest": "two.txt"}],
        source_commit=source_commit,
    )
    real_replace = os.replace
    calls = 0

    def fail_second(source_path, dest_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        return real_replace(source_path, dest_path)

    monkeypatch.setattr(port, "_atomic_replace", fail_second)
    assert port.run(working, source, dest, dry_run=False) == 1
    assert (dest / "one.txt").read_text() == "old one\n"
    assert not (dest / "two.txt").exists()


def test_porting_without_a_vocabulary_is_refused_when_one_exists(tmp_path: Path) -> None:
    """A run that would leave every source identifier in place must not succeed.

    This is a regression test for a real incident: `port.py` was re-run without
    `--vocabulary`, every file ported, the summary said `violations: 0`, and 55
    files kept the source project's name. The only difference between that run
    and a correct one was `renames applied: 0` in the summary, which is exactly
    the kind of signal a person skims past.
    """
    vocabulary = tmp_path / port.VOCABULARY_BASENAME
    vocabulary.write_text('{"renames": [["alpha", "beta"]]}', encoding="utf-8")

    with pytest.raises(port.PortRefusal) as refusal:
        port._require_vocabulary(None, (), default=vocabulary)
    # The message has to name the fix, not just the failure -- a bare exception
    # type is what let the original mistake stand.
    assert "--vocabulary" in str(refusal.value)

    # A vocabulary that exists but renames nothing is the same failure wearing a
    # different hat, and is refused too.
    with pytest.raises(port.PortRefusal):
        port._require_vocabulary(vocabulary, (), default=vocabulary)

    # Ablation: with renames present the guard must stay silent, otherwise it
    # would be indistinguishable from a check that always fires.
    port._require_vocabulary(vocabulary, (("alpha", "beta"),), default=vocabulary)

    # And with no vocabulary anywhere, porting raw is a legitimate choice.
    port._require_vocabulary(None, (), default=tmp_path / "absent.json")


def test_email_rule_ignores_retina_asset_names_but_not_addresses(tmp_path: Path) -> None:
    """`icon_16x16@2x.png` is not an email address.

    Apple's retina convention produces exactly the shape the rule looks for --
    local part, at-sign, dotted right-hand side, alphabetic suffix -- so every
    project with an asset catalogue used to fail the gate on its own icons. The
    ablation half matters more than the exemption half: an exclusion list is
    easy to widen until the rule stops catching anything.
    """
    pattern, _, flags = next(
        spec for spec in port._STRUCTURAL_FORBIDDEN_SPECS if spec[1] == "email address"
    )
    rule = re.compile(pattern, flags)

    for benign in ("icon_16x16@2x.png", "icon_512x512@2x.png", "shot@3x.jpeg",
                   "logo@2x.svg", "someone@example.org"):
        assert rule.search(benign) is None, f"{benign} must not read as an address"

    # Assembled at runtime rather than written out: a literal address in this
    # file would be a real finding, and the gate is right to say so.
    at = chr(64)
    for local, domain in (("real.person", "company.com"), ("a", "b.co"),
                          ("dev", "team.io"), ("first.last+tag", "sub.domain.net")):
        address = f"{local}{at}{domain}"
        assert rule.search(address), f"{local}-at-{domain} must still be caught"
