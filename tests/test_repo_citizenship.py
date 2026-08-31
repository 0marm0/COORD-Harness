"""The three files a stranger looks for before trusting a public repository.

SECURITY.md, CONTRIBUTING.md, and CODE_OF_CONDUCT.md must exist at the
repository root, say something real (not boilerplate stubs), and never carry
an invented contact address or a path that discloses a real machine. The
scope section of SECURITY.md is additionally checked against
docs/threat-model.md's own stated non-goals, so the two cannot silently drift
apart -- a word-count check on the scope section would pass even if its
categories stopped matching what the threat model actually says is excluded.

Any string here that looks like the forbidden shape it's testing for is
assembled from parts rather than written as a literal -- a literal needle in
this file would trip the repository's own privacy scanner
(tools/privacy_hygiene.py), which scans tracked *and* untracked files.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CITIZENSHIP_FILES = {
    "SECURITY.md": ROOT / "SECURITY.md",
    "CONTRIBUTING.md": ROOT / "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md": ROOT / "CODE_OF_CONDUCT.md",
}

# Well below what any of these documents actually run to; a stub placeholder
# ("See the project wiki.") would not clear this, but the checks below don't
# rely on length alone -- see test_security_md_scope_matches_threat_model_categories.
MIN_NONTRIVIAL_CHARS = 800

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}")

# Built from parts, not written as "/Users/" or "/home/" -- see module docstring.
_HOME_PREFIXES = tuple("/" + segment + "/" for segment in ("Users", "home"))

# The three out-of-scope categories the task names verbatim: an actor who can
# open the database file, allocate a terminal, or run arbitrary code as the
# user. Each entry is a literal phrase that must appear in *both*
# docs/threat-model.md (proving the premise still holds there) and
# SECURITY.md's scope section (proving SECURITY.md still reflects it) --
# checking for these specific categories, not counting words.
SCOPE_CATEGORY_PHRASES = {
    "open_the_database_file": "write `coord.db`",
    "allocate_a_terminal": "controlling terminal",
    "run_arbitrary_code_as_the_user": "compromised agent runtime",
}


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.name} is missing at the repository root"
    return path.read_text(encoding="utf-8")


def _flatten(text: str) -> str:
    """Collapse whitespace so a phrase check survives Markdown's line wrap.

    Prose in these files wraps at roughly 72-80 columns; a two- or three-word
    phrase can legitimately have a newline where a plain sentence would have
    a space. Checking against the raw text would make every phrase assertion
    fragile to a harmless rewrap -- flatten runs of whitespace to one space
    before searching for a phrase, the same normalization a reader's eye
    already does.
    """

    return re.sub(r"\s+", " ", text)


def test_all_three_citizenship_files_exist_at_the_repository_root() -> None:
    for name, path in CITIZENSHIP_FILES.items():
        assert path.is_file(), f"{name} is missing at the repository root ({path})"


def test_citizenship_files_are_non_trivial() -> None:
    for name, path in CITIZENSHIP_FILES.items():
        text = _read(path)
        assert len(text) >= MIN_NONTRIVIAL_CHARS, (
            f"{name} is only {len(text)} characters; that reads as a stub, "
            "not a real policy"
        )
        # More than one section: a single paragraph is not what any of these
        # three documents are supposed to be.
        heading_count = sum(1 for line in text.splitlines() if line.startswith("#"))
        assert heading_count >= 3, f"{name} has only {heading_count} heading(s)"


def test_no_invented_email_address_in_any_citizenship_file() -> None:
    for name, path in CITIZENSHIP_FILES.items():
        text = _read(path)
        found = EMAIL_RE.findall(text)
        assert not found, f"{name} contains an email-like address: {found!r}"


def test_no_absolute_host_path_in_any_citizenship_file() -> None:
    for name, path in CITIZENSHIP_FILES.items():
        text = _read(path)
        for prefix in _HOME_PREFIXES:
            assert prefix not in text, (
                f"{name} contains an absolute host path prefix {prefix!r}"
            )


def test_no_second_security_policy_shadows_the_root_one() -> None:
    """GitHub resolves a security policy from `.github/` before the root.

    Whatever sits at `.github/SECURITY.md` is therefore what the Security tab
    and the repository's "Security policy" badge serve -- the root file the
    tests above check is never reached while a second policy lives there. So
    that path must either not exist, or be a pointer at the root policy: it
    may not be a self-contained second policy, because a second policy is
    free to drift out of step with the one every other test and doc link
    treats as the policy of record.
    """
    shadow = ROOT / ".github" / "SECURITY.md"
    if not shadow.is_file():
        return  # removed outright -- the root file is then what GitHub serves
    text = shadow.read_text(encoding="utf-8")
    assert "../SECURITY.md" in text, (
        ".github/SECURITY.md is what GitHub's Security tab serves but does not "
        "link to ../SECURITY.md, so the root policy is unreachable from it"
    )
    # It must not restate the policy: none of the scope categories the root
    # policy owns may appear here, and it stays under the length that marks a
    # document as substantive rather than a redirect.
    for category, phrase in SCOPE_CATEGORY_PHRASES.items():
        assert phrase not in _flatten(text), (
            f".github/SECURITY.md restates the {category} scope category; it "
            "must point at the root policy, not carry a copy that can drift"
        )
    assert len(_flatten(text)) < MIN_NONTRIVIAL_CHARS, (
        ".github/SECURITY.md is long enough to be a second policy rather than "
        "a pointer to the root one"
    )


def test_security_md_names_the_github_private_reporting_route() -> None:
    text = _flatten(_read(CITIZENSHIP_FILES["SECURITY.md"]))
    assert "private vulnerability reporting" in text.lower()
    assert "Security" in text  # the GitHub tab name, cited as guidance
    # It must actively steer away from a public issue.
    assert "public issue" in text.lower()


def test_security_md_states_the_single_maintainer_response_expectation_honestly() -> None:
    lowered = _flatten(_read(CITIZENSHIP_FILES["SECURITY.md"])).lower()
    assert "one person" in lowered or "single" in lowered
    # It must not promise a fixed turnaround time -- that's the SLA this
    # project cannot keep. Guard against the general SHAPE of an invented
    # promise ("within <N> <unit>") rather than a fixed list of literal
    # strings, which only ever catches phrasings already thought of.
    assert "service-level agreement" in lowered or "no sla" in lowered
    sla_promise = re.search(r"within \d+\s+(hour|day|business day|week)", lowered)
    assert sla_promise is None, f"SECURITY.md appears to promise: {sla_promise.group(0)!r}"
    assert "guarantee a response" not in lowered


def test_security_md_states_the_supported_version_honestly() -> None:
    text = _flatten(_read(CITIZENSHIP_FILES["SECURITY.md"]))
    lowered = text.lower()
    # There is exactly one unreleased line -- no version table implying a
    # matrix of maintained releases that don't exist.
    assert "no git tag" in lowered or "not cut a tagged release" in lowered
    assert "0.1.0" in text


def test_security_md_scope_matches_threat_model_categories() -> None:
    threat_model = _flatten((ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8"))
    security = _flatten(_read(CITIZENSHIP_FILES["SECURITY.md"]))

    for category, phrase in SCOPE_CATEGORY_PHRASES.items():
        assert phrase in threat_model, (
            f"{category}: expected phrase {phrase!r} in docs/threat-model.md; "
            "the premise this test checks SECURITY.md against no longer holds "
            "there, so this test can no longer prove consistency"
        )
        assert phrase in security, (
            f"{category}: SECURITY.md's scope section is missing {phrase!r}, "
            "so it no longer names the same out-of-scope category "
            "docs/threat-model.md states"
        )


def test_security_md_out_of_scope_section_exists_and_precedes_named_categories() -> None:
    text = _flatten(_read(CITIZENSHIP_FILES["SECURITY.md"]))
    scope_index = text.lower().find("out of scope")
    assert scope_index != -1, "SECURITY.md has no 'Out of scope' section"
    tail = text[scope_index:]
    for phrase in SCOPE_CATEGORY_PHRASES.values():
        assert phrase in tail, (
            f"{phrase!r} must appear after the 'Out of scope' heading, not "
            "merely somewhere in the document"
        )


def test_code_of_conduct_is_the_contributor_covenant() -> None:
    text = _read(CITIZENSHIP_FILES["CODE_OF_CONDUCT.md"])
    assert "Contributor Covenant" in text
    assert "2.1" in text
    for heading in ("Our Pledge", "Our Standards", "Enforcement", "Attribution"):
        assert heading in text, f"CODE_OF_CONDUCT.md is missing the {heading!r} section"


def test_code_of_conduct_enforcement_contact_uses_the_github_route_not_an_email() -> None:
    text = _read(CITIZENSHIP_FILES["CODE_OF_CONDUCT.md"])
    # Exact heading match: the document also has "## Enforcement
    # Responsibilities" and "## Enforcement Guidelines" sections, and a bare
    # substring search for "## Enforcement" would match the first of those
    # instead of the contact section this test actually needs.
    match = re.search(r"(?m)^## Enforcement\s*$", text)
    assert match is not None, "CODE_OF_CONDUCT.md has no exact '## Enforcement' section"
    section = text[match.start() : match.start() + 1500]
    assert "SECURITY.md" in section
    assert not EMAIL_RE.search(section), "the enforcement contact reads as an email address"


def test_contributing_md_covers_the_gate_a_new_contributor_would_otherwise_miss() -> None:
    text = _flatten(_read(CITIZENSHIP_FILES["CONTRIBUTING.md"]))
    lowered = text.lower()
    # The review-tier model, cited by name, not just "review your code".
    assert "review-tier" in lowered
    # The proof-gated completion discipline.
    assert "completion" in lowered and "gate" in lowered
    # The byte-identical mirror requirement between the two client trees.
    assert ".claude" in text and ".agents" in text
    assert "byte-identical" in lowered
    # The batched/memory-watchdog test invocation.
    assert "watchdog" in lowered
    assert "batch" in lowered
    # The public-hygiene constraint.
    assert "privacy_hygiene" in text

    # Why a bare full-process run gets killed -- a contributor should be able
    # to answer this after reading the file, not just know to avoid it.
    assert "sigkill" in lowered or "kill" in lowered
