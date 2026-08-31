from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "public_hygiene_sweep.py"
SPEC = importlib.util.spec_from_file_location("public_hygiene_sweep", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sweep_mod = importlib.util.module_from_spec(SPEC)
# The module defines a dataclass, whose machinery looks itself up in
# sys.modules by name -- it must be registered before exec_module runs.
sys.modules[SPEC.name] = sweep_mod
SPEC.loader.exec_module(sweep_mod)


def _assembled(*parts: str) -> str:
    """Build a needle at runtime so this file's own bytes never carry it.

    A literal instance here would trip the repository's own scanners
    (privacy_hygiene.py, gitleaks) the moment this file is tracked --
    exactly the "two tests carrying their own needle" incident this
    convention exists to prevent.
    """
    return "".join(parts)


# ---------------------------------------------------------------------------
# Class 1: absolute home paths
# ---------------------------------------------------------------------------


def test_real_home_path_is_caught_but_generic_placeholder_is_not() -> None:
    private_home = _assembled("/", "Users", "/", "jsmith", "/project/notes.md")
    findings = sweep_mod._scan_text("fixture.txt", private_home, [])
    assert any(f.category == "home_path" for f in findings)

    generic = "/home/runner/work/checkout"
    assert not sweep_mod._scan_text("fixture.txt", generic, [])


# ---------------------------------------------------------------------------
# Class 2: personal-looking emails, vs. generic/service/filename lookalikes
# ---------------------------------------------------------------------------


def test_personal_email_is_caught() -> None:
    address = _assembled("j", "smith", "84", "@", "gmail", ".com")
    findings = sweep_mod._scan_text("fixture.txt", f"contact {address}", [])
    assert any(f.category == "email" for f in findings)


@pytest.mark.parametrize(
    "text",
    [
        # Assembled at runtime (see _assembled's docstring above): a literal
        # instance of any of these four would itself trip
        # tools/extract/gate.py's structural email-address rule, which --
        # unlike this module's own SCANNER_OWNED_PATHS allowlist -- has no
        # exemption for this file.
        f"reach the bot at {_assembled('noreply', '@', 'anthropic.com')}",
        f"file a bug at {_assembled('support', '@', 'github.com')}",
        "placeholder: demo@example.invalid",
        "asset: icon_512x512@2x.png",
        "asset: logo@2x.svg",
        # RFC 2606 reserved domain under a subdomain: registrable label is
        # still "example", so this must not be flagged as personal either.
        f"placeholder: {_assembled('demo', '@', 'sub.example.com')}",
        f"placeholder: {_assembled('demo', '@', 'deeply.nested.example.org')}",
    ],
)
def test_generic_and_filename_like_addresses_are_not_flagged(text: str) -> None:
    assert not sweep_mod._scan_text("fixture.txt", text, [])


# ---------------------------------------------------------------------------
# Class 3: the private origin project's internal work-id grammar
# ---------------------------------------------------------------------------


def test_internal_workid_grammar_is_caught() -> None:
    work_id = _assembled("N", "0831", "-", "CLA", "-", "DEMO", "-", "SLUG")
    findings = sweep_mod._scan_text("fixture.txt", f"see {work_id} for detail", [])
    assert any(f.category == "workid_grammar" for f in findings)


def test_an_ordinary_adopter_id_is_not_the_private_grammar() -> None:
    # Mirrors tests/test_public_generalization.py: a stranger's own id
    # convention is not required, and must not be misread as a leak either.
    for work_id in ("ACME-CLA-BILLING-EXPORT", "Q3-CDX-INDEX-REBUILD", "PLAIN"):
        findings = sweep_mod._scan_text("fixture.txt", work_id, [])
        assert not any(f.category == "workid_grammar" for f in findings), work_id


# ---------------------------------------------------------------------------
# Class 4: maintainer-supplied private vocabulary, and the absent-vocabulary case
# ---------------------------------------------------------------------------


def test_vocabulary_hit_is_caught_when_configured() -> None:
    codename = _assembled("project", " ", "nightjar")
    findings = sweep_mod._scan_text("fixture.txt", f"the {codename} rollout", [codename])
    assert any(f.category == "vocabulary" for f in findings)
    # The phrase itself must never appear in the rendered finding.
    assert codename not in findings[0].render()


def test_absent_vocabulary_reports_none_configured(monkeypatch) -> None:
    monkeypatch.delenv("PUBLIC_HYGIENE_VOCAB_FILE", raising=False)
    monkeypatch.delenv("PUBLIC_HYGIENE_VOCAB", raising=False)
    phrases, source = sweep_mod._load_vocabulary(None)
    assert phrases == []
    assert source == "none"


def test_load_vocabulary_reads_file_and_env(tmp_path: Path, monkeypatch) -> None:
    vocab_file = tmp_path / "vocab.txt"
    vocab_file.write_text("alpha term\n# a comment\nbeta term\n", encoding="utf-8")
    phrases, source = sweep_mod._load_vocabulary(vocab_file)
    assert phrases == ["alpha term", "beta term"]
    assert "--vocabulary" in source

    monkeypatch.delenv("PUBLIC_HYGIENE_VOCAB_FILE", raising=False)
    monkeypatch.setenv("PUBLIC_HYGIENE_VOCAB", "gamma term, delta term")
    phrases, source = sweep_mod._load_vocabulary(None)
    assert phrases == ["gamma term", "delta term"]
    assert source == "PUBLIC_HYGIENE_VOCAB"


# ---------------------------------------------------------------------------
# The scanner-owned allowlist: a detector pattern is not a leak
# ---------------------------------------------------------------------------


def test_scanner_owned_path_is_exempt_from_the_same_payload_that_trips_elsewhere() -> None:
    payload = _assembled("/", "Users", "/", "jsmith", "/x").encode("utf-8")

    ordinary_findings, ordinary_skip = sweep_mod._scan_payload("docs/example.md", payload, [])
    assert ordinary_findings, "the payload must be a real hit on an ordinary path"
    assert ordinary_skip is None

    for owned in sweep_mod.SCANNER_OWNED_PATHS:
        findings, skip_reason = sweep_mod._scan_payload(owned, payload, [])
        assert findings == []
        assert skip_reason == "scanner_owned"


def test_the_scanner_owned_allowlist_is_pinned() -> None:
    """The exemption is whole-file, so the membership itself must be pinned.

    The test above iterates the tuple and so passes no matter how long it
    grows: appending a real content file to silence one false positive would
    exempt that file from every leak class -- home paths, addresses, work-id
    grammar, vocabulary -- permanently, with the suite still green and only a
    printed line in a CI log to notice. Pinning the exact membership means a
    widening cannot land without editing this assertion in the same diff,
    where a reviewer sees it.
    """
    assert sweep_mod.SCANNER_OWNED_PATHS == (
        "tools/public_hygiene_sweep.py",
        "tests/test_public_hygiene_sweep.py",
    ), (
        "SCANNER_OWNED_PATHS changed: each entry is wholly exempt from every "
        "leak class, so justify the addition here rather than only in the "
        "scanner"
    )


# ---------------------------------------------------------------------------
# Coverage honesty: a skipped payload must never be counted as scanned
# ---------------------------------------------------------------------------


def test_oversize_and_binary_payloads_are_skipped_not_scanned(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sweep_mod, "ROOT", tmp_path)

    (tmp_path / "clean.txt").write_text("nothing to see here\n", encoding="utf-8")

    oversize = tmp_path / "big.json"
    oversize.write_bytes(b"0" * (sweep_mod.MAX_SCAN_BYTES + 1))

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02binary stuff")

    findings, counts = sweep_mod.sweep(["clean.txt", "big.json", "blob.bin"], [])

    assert findings == []
    assert counts.scanned == 1, "only the genuinely-decoded file counts as scanned"
    assert counts.skipped_oversize == 1
    assert counts.skipped_binary == 1
    assert counts.skipped_scanner_owned == 0
    assert counts.skipped == 2

    rendered = counts.render()
    assert "1 file(s) scanned" in rendered
    assert "2 file(s) skipped" in rendered
    assert "1 binary" in rendered
    assert "1 oversize" in rendered


def test_a_leak_inside_a_skipped_payload_produces_no_finding(
    tmp_path: Path, monkeypatch
) -> None:
    """This is the defect P2-1 describes: a skipped payload is never
    decoded, so a real leak inside one is invisible. The summary line must
    say the file was skipped, never that it was scanned clean.
    """
    monkeypatch.setattr(sweep_mod, "ROOT", tmp_path)
    leaky = _assembled("/", "Users", "/", "jsmith", "/notes").encode("utf-8")

    oversize = tmp_path / "big.md"
    oversize.write_bytes(leaky + b"0" * sweep_mod.MAX_SCAN_BYTES)

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01" + leaky)

    findings, counts = sweep_mod.sweep(["big.md", "blob.bin"], [])

    assert findings == [], "a skipped payload must not be decoded or scanned"
    assert counts.scanned == 0
    assert counts.skipped_oversize == 1
    assert counts.skipped_binary == 1


# ---------------------------------------------------------------------------
# End-to-end: a small real git repo, full tree and diff-range modes
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "clean.txt").write_text("nothing to see here\n", encoding="utf-8")
    _git(repo, "add", "clean.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_main_passes_clean_tree_and_reports_reduced_coverage(
    fixture_repo: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sweep_mod, "ROOT", fixture_repo)
    monkeypatch.delenv("PUBLIC_HYGIENE_VOCAB_FILE", raising=False)
    monkeypatch.delenv("PUBLIC_HYGIENE_VOCAB", raising=False)

    exit_code = sweep_mod.main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS" in out
    assert "REDUCED COVERAGE" in out


def test_main_fails_on_a_planted_leak_and_diff_range_scopes_to_changed_files(
    fixture_repo: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sweep_mod, "ROOT", fixture_repo)

    leaky = _assembled("/", "Users", "/", "jsmith", "/notes")
    (fixture_repo / "leak.txt").write_text(leaky, encoding="utf-8")
    _git(fixture_repo, "add", "leak.txt")
    _git(fixture_repo, "commit", "-q", "-m", "add leak")

    exit_code = sweep_mod.main([])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "leak.txt" in err
    assert "home_path" in err

    # Now diff-range mode: only the second commit changed leak.txt.
    exit_code = sweep_mod.main(["--diff-range", "HEAD~1...HEAD"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "leak.txt" in err

    # A range with no changes finds nothing to scan and passes clean.
    exit_code = sweep_mod.main(["--diff-range", "HEAD...HEAD"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 file(s) scanned" in out
