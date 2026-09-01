"""Every third-party action must be pinned to a commit, not to a moving ref.

A tag or a branch in `uses:` is a name someone else can repoint. That is a
supply-chain hole everywhere, but it is a credential hole in exactly one place
here: the publish job holds `id-token: write` for trusted publishing, so an
action resolved through a moving ref runs with the right to mint a PyPI token
for this project on the next `v*` tag. The rest of the workflows were already
pinned by SHA; `pypa/gh-action-pypi-publish@release/v1` was the single
exception, and it sat in that job.

Checked as text rather than parsed YAML: this repository declares no YAML
dependency. The scan reports how many `uses:` lines it saw, so a regex that
silently stops matching cannot pass as "nothing unpinned".
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

USES = re.compile(r"(?m)^\s*-?\s*uses:\s*(\S+)")
SHA = re.compile(r"^[0-9a-f]{40}$")

#: Actions published from this repository need no pin: they are the same commit
#: as the workflow that calls them.
LOCAL_PREFIXES = ("./", "../")


def _uses() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        for reference in USES.findall(path.read_text(encoding="utf-8")):
            found.append((path.name, reference))
    return found


def test_the_scan_finds_the_actions_it_claims_to_check() -> None:
    found = _uses()
    assert len(found) >= 20, f"only {len(found)} uses: lines found; the scan lost its grip"
    assert any(name == "release.yml" for name, _ in found)


def test_every_third_party_action_is_pinned_to_a_commit() -> None:
    unpinned = [
        f"{name}: {reference}"
        for name, reference in _uses()
        if not reference.startswith(LOCAL_PREFIXES)
        and not SHA.match(reference.partition("@")[2])
    ]
    assert unpinned == [], "actions pinned to a moving ref: " + ", ".join(unpinned)


def test_the_publish_job_that_holds_id_token_write_is_pinned() -> None:
    """The one job where a moving ref is a credential hole, checked by name."""
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in text
    publish = text[text.index("id-token: write") :]
    publish = publish[: publish.index("\n  attach-release-assets:")]
    references = USES.findall(publish)
    assert references, "the publish job uses no actions; this test is measuring nothing"
    for reference in references:
        assert SHA.match(reference.partition("@")[2]), reference
