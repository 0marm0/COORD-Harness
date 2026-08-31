"""The Pages workflow must not need a permission its job was never granted.

`actions/configure-pages` resolves and enables the Pages site through the Pages
API, so it fails on a job whose token lacks `pages: write`. Placed in the build
job -- which runs on every matching push and pull request -- it would red the
whole workflow on any repository where Pages has not been turned on, which is
the state the workflow's own header assumes. Checked as text rather than parsed
YAML: this repository declares no YAML dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"

CONFIGURE_PAGES = "actions/configure-pages@"
JOB_HEADER = re.compile(r"(?m)^  ([a-z][a-z0-9_-]*):$")


def _jobs() -> dict[str, str]:
    """Split the file into job name -> job body, by two-space indentation."""
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs_at = text.index("\njobs:\n")
    body = text[jobs_at:]
    starts = [(match.group(1), match.start()) for match in JOB_HEADER.finditer(body)]
    assert starts, "no job headers found; the workflow layout changed"
    bounds = [start for _, start in starts] + [len(body)]
    return {
        name: body[bounds[index] : bounds[index + 1]]
        for index, (name, _) in enumerate(starts)
    }


def test_configure_pages_only_runs_where_pages_write_is_granted() -> None:
    jobs = _jobs()
    assert "build" in jobs and "deploy" in jobs, sorted(jobs)

    using_it = [name for name, source in jobs.items() if CONFIGURE_PAGES in source]
    assert using_it, "configure-pages disappeared; the deploy job needs it"
    for name in using_it:
        assert "pages: write" in jobs[name], (
            f"job {name!r} runs configure-pages without `pages: write`"
        )


def test_the_build_job_holds_no_pages_permission() -> None:
    # Top-level permissions are contents: read, so a build job that granted
    # itself none is what keeps every push and pull request green pre-enablement.
    assert "permissions:" not in _jobs()["build"]
