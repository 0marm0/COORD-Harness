from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "privacy_hygiene.py"
SPEC = importlib.util.spec_from_file_location("privacy_hygiene", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
privacy_hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy_hygiene)


def _digest(phrase: str) -> str:
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def test_opaque_phrase_digest_blocks_without_printing_phrase() -> None:
    phrase = "confidential alpha"
    findings = privacy_hygiene._matches(
        "tree:fixture.txt",
        f"prefix {phrase} suffix".encode(),
        {_digest(phrase)},
    )

    assert len(findings) == 1
    assert phrase not in findings[0]
    assert _digest(phrase)[:12] in findings[0]


def test_real_home_path_blocks_but_generic_ci_fixture_is_allowed() -> None:
    private_home = "".join(("/", "Users", "/", "alice", "/project")).encode()
    assert privacy_hygiene._matches(
        "tree:fixture.txt", private_home, set()
    )
    assert not privacy_hygiene._matches(
        "tree:fixture.txt", b"/home/runner/work/project", set()
    )


def test_denylist_must_be_nonempty_and_sha256_only(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# comments only", encoding="utf-8")
    with pytest.raises(ValueError, match="must not be empty"):
        privacy_hygiene._load_digests(empty)

    malformed = tmp_path / "malformed.txt"
    malformed.write_text("not-a-digest", encoding="utf-8")
    with pytest.raises(ValueError, match="expected one SHA-256"):
        privacy_hygiene._load_digests(malformed)
