"""Guard against hardcoded loopback endpoints.

This class of defect has landed twice. Thirteen literal `127.0.0.1:<port>`
endpoints survived a port of the native clients and pointed them at the *source*
system's live board instead of this harness's own board. The literals were
individually plausible, so no reviewer caught them by reading.

Two rules, deliberately asymmetric:

1. A loopback literal on a port this harness does not own is a violation with no
   allowlist. That is the exact shape of the shipped defect -- a client wired to
   somebody else's server -- and no justification makes it safe.
2. A loopback literal on the harness's own port is a violation *unless* the file
   is named below with a reason. Those are legitimate but they are the soil the
   defect grows in, so each one is enumerated rather than tolerated by pattern.

The scanner is a pure function over ``(path, text)`` pairs so it can be proven
able to go red against synthetic fixtures without editing any real file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Loopback endpoint literals, e.g. ``127.0.0.1:7870``.
ENDPOINT_RE = re.compile(r"127\.0\.0\.1:(\d+)")

#: Ports this harness itself listens on. Anything else in a loopback literal is
#: a foreign server. :65535 and :65432 are reserved synthetic sentinel ports
#: used only to prove that the scanner rejects foreign endpoints.
HARNESS_PORTS = frozenset({7870})
HARNESS_PORT_LITERAL_RE = re.compile(
    rf"(?<!\d)(?:{'|'.join(str(port) for port in sorted(HARNESS_PORTS))})(?!\d)"
)
NATIVE_TEST_LITERAL_HOME = "apps/Tests/EndpointTestFixtures.swift"

#: Files permitted to spell out a harness-port loopback literal, each with the
#: reason it is not a wiring defect. Paths are repo-relative and POSIX-style.
SANCTIONED_LITERALS: dict[str, str] = {
    # The one sanctioned Swift home: every native endpoint resolves through this
    # enum, which builds the default from `defaultPort` and honours
    # COORD_BOARD_URL. Listed so the legitimate home stays legal if it is ever
    # rewritten to spell the literal out.
    "apps/menubar/Sources/App/HarnessEndpoint.swift": (
        "sanctioned single source of the native base URL, with an env override"
    ),
    # A user-editable default for the base-URL field the shared SwiftUI clients
    # expose; apps/Shared is a separate target that does not link the menubar
    # app's HarnessEndpoint. Config the user can change, not fixed wiring.
    "apps/Shared/Sources/CockpitModel.swift": (
        "UserDefaults fallback for the operator-editable base URL field"
    ),
    # The only sanctioned XCTest home. Tests consume these values as inputs and
    # expectations; no production target imports this file.
    NATIVE_TEST_LITERAL_HOME: "centralized endpoint values for XCTest fixtures",
    # An argparse default the caller overrides with --url: config, not wiring.
    "tools/capture_board_screens.py": "--url argparse default for a capture tool",
}

#: Paths excluded from scanning entirely, with the reason. This is not an
#: allowlist for violations: it is for files whose endpoint strings are *rules
#: about* endpoints rather than uses of them. Each entry is asserted below to be
#: untracked by git, so an exclusion can never quietly cover a shipped file.
EXCLUDED_PATHS: dict[str, str] = {
    # The private extraction manifest. Its `find` values are the source
    # product's endpoints listed so the porter *removes* them; the paired `with`
    # values route through HarnessEndpoint. Scanning it would flag the scrub
    # rules as the thing they exist to delete.
    "tools/extract/manifest.private.json": (
        "private port manifest; the endpoints are find-patterns being stripped"
    ),
}

#: Trees the guard covers.
SCAN_GLOBS: tuple[tuple[str, str], ...] = (
    ("apps", "**/*.swift"),
    ("src/coordharness", "**/*"),
    ("tools", "**/*"),
)

#: Suffixes read as bytes rather than source. Skips are surfaced, never silent.
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".ico",
        ".zip",
        # Fonts. Listed rather than left to be "skipped silently", which this
        # test treats as a failure on purpose: a scanner that quietly drops a
        # file it cannot decode reports a clean sweep over a smaller tree.
        ".woff",
        ".woff2",
        ".otf",
        ".ttf",
        ".icns",
    }
)

# Finder metadata is neither shipped source nor a binary application asset.
# Ignore it explicitly so an ambient macOS file cannot make the source scan
# nondeterministic while every real skipped file remains suffix-audited below.
IGNORED_FILENAMES = frozenset({".DS_Store"})


class Finding(NamedTuple):
    """One offending loopback literal."""

    path: str
    line: int
    port: int
    kind: str
    text: str


def scan(
    pairs: Iterable[tuple[str, str]],
    *,
    sanctioned: Iterable[str] = tuple(SANCTIONED_LITERALS),
    excluded: Iterable[str] = tuple(EXCLUDED_PATHS),
) -> tuple[Finding, ...]:
    """Return every offending endpoint literal in ``(path, text)`` pairs.

    Pure: no filesystem, no module state. The policy is passed in (defaulted
    from the tables above) so an ablation can re-run it with an empty policy.
    """
    sanctioned_paths = frozenset(sanctioned)
    excluded_paths = frozenset(excluded)
    findings: list[Finding] = []
    for path, text in pairs:
        if path in excluded_paths:
            continue
        is_sanctioned = path in sanctioned_paths
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in ENDPOINT_RE.finditer(line):
                port = int(match.group(1))
                if port not in HARNESS_PORTS:
                    kind = "foreign-port"
                elif is_sanctioned:
                    continue
                else:
                    kind = "unsanctioned-literal"
                findings.append(Finding(path, lineno, port, kind, line.strip()))
    return tuple(findings)


def _collect() -> tuple[list[tuple[str, str]], list[str]]:
    """Read the scanned trees. Returns ``(pairs, skipped)``; skips are binary."""
    pairs: list[tuple[str, str]] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for root, pattern in SCAN_GLOBS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for candidate in sorted(base.glob(pattern)):
            if not candidate.is_file() or "__pycache__" in candidate.parts:
                continue
            if candidate.name in IGNORED_FILENAMES:
                continue
            rel = candidate.relative_to(REPO_ROOT).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            if candidate.suffix.lower() in BINARY_SUFFIXES:
                skipped.append(rel)
                continue
            try:
                pairs.append((rel, candidate.read_text(encoding="utf-8")))
            except UnicodeDecodeError:
                skipped.append(rel)
    return pairs, skipped


def _format(findings: Iterable[Finding]) -> str:
    return "\n".join(f"  {f.path}:{f.line} [{f.kind}] :{f.port} -- {f.text}" for f in findings)


# --------------------------------------------------------------------------
# The scanner is proven able to go red, against synthetic input only.
# --------------------------------------------------------------------------

FIXTURE_MUST_FAIL: tuple[tuple[str, str], ...] = (
    (
        "apps/menubar/Sources/Data/HarnessClient.swift",
        'let url = URL(string: "http://127.0.0.1:65535/api/snapshot")!\n',
    ),
    ("src/coordharness/board/server.py", 'BASE = "http://127.0.0.1:7870"\n'),
)

FIXTURE_MUST_PASS: tuple[tuple[str, str], ...] = (
    ("apps/menubar/Sources/Data/HarnessClient.swift", 'let url = HarnessEndpoint.url("/api")\n'),
    (NATIVE_TEST_LITERAL_HOME, 'static let base = "http://127.0.0.1:7870"\n'),
    ("tools/extract/manifest.private.json", '{"find": "http://127.0.0.1:65535"}\n'),
    ("src/coordharness/board/server.py", "DEFAULT_PORT = 7870\n"),
)


def test_scanner_flags_a_synthetic_foreign_endpoint_and_an_unsanctioned_literal() -> None:
    findings = scan(FIXTURE_MUST_FAIL)
    assert [(f.path, f.port, f.kind) for f in findings] == [
        ("apps/menubar/Sources/Data/HarnessClient.swift", 65535, "foreign-port"),
        ("src/coordharness/board/server.py", 7870, "unsanctioned-literal"),
    ]


def test_scanner_passes_a_synthetic_clean_tree() -> None:
    assert scan(FIXTURE_MUST_PASS) == ()


def test_scanner_reports_line_numbers_and_every_match_on_a_line() -> None:
    text = 'ok\nlet a = "127.0.0.1:65535"; let b = "127.0.0.1:65432"\n'
    findings = scan((("apps/x.swift", text),))
    assert [(f.line, f.port) for f in findings] == [(2, 65535), (2, 65432)]


# --------------------------------------------------------------------------
# The real trees.
# --------------------------------------------------------------------------


def test_repository_has_no_hardcoded_endpoints() -> None:
    pairs, skipped = _collect()
    assert len(pairs) > 100, f"scan collected only {len(pairs)} files; globs are wrong"
    assert all(Path(rel).suffix.lower() in BINARY_SUFFIXES for rel in skipped), (
        f"unreadable non-binary files were skipped silently: {skipped}"
    )
    findings = scan(pairs)
    assert not findings, "hardcoded loopback endpoints:\n" + _format(findings)


def test_native_test_port_literals_have_one_fixture_home() -> None:
    pairs, _ = _collect()
    literal_homes = {
        path
        for path, text in pairs
        if path.startswith("apps/Tests/") and HARNESS_PORT_LITERAL_RE.search(text)
    }
    assert literal_homes == {NATIVE_TEST_LITERAL_HOME}, (
        "native XCTest harness-port literals must be centralized in "
        f"{NATIVE_TEST_LITERAL_HOME}: {sorted(literal_homes)}"
    )


def test_the_real_scan_can_go_red() -> None:
    """Ablation: drop the sanctioned entries and the real trees must fail.

    A guard that passes because it looks at nothing is indistinguishable from a
    guard that passes because the tree is clean. This proves the tree is clean.
    """
    pairs, _ = _collect()
    findings = scan(pairs, sanctioned=(), excluded=())
    assert findings, "ablated scan found nothing; the guard is not reading real files"
    assert {f.path for f in findings} >= set(SANCTIONED_LITERALS) - {
        # HarnessEndpoint builds its default by interpolation, so it carries no
        # literal today; it is listed to keep the sanctioned home legal.
        "apps/menubar/Sources/App/HarnessEndpoint.swift",
    }


def test_sanctioned_policy_names_files_that_exist() -> None:
    """Every shipped exception must name a shipped file.

    Exclusions are different: they name private, gitignored inputs that may be
    present in the extraction checkout but must be absent from a public clone.
    Requiring those paths here made the suite pass only in the private working
    tree and fail in the exact publication tree.
    """
    missing = [p for p in SANCTIONED_LITERALS if not (REPO_ROOT / p).exists()]
    assert not missing, f"sanctioned policy names paths that no longer exist: {missing}"


def test_excluded_paths_are_untracked() -> None:
    """An exclusion may not cover a file that ships."""
    present = [p for p in EXCLUDED_PATHS if (REPO_ROOT / p).exists()]
    if not present:
        pytest.skip("no excluded path present in this checkout")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", *present],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    assert not tracked, f"excluded paths are tracked and must be scanned: {tracked}"
