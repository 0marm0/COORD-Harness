#!/usr/bin/env python3
"""Generate the claim lifecycle diagram from the lifecycle implementation.

The diagram deliberately derives its state vocabulary from ``coord_db.py``.
Adding a claim status without first deciding how it should be drawn is an error,
not an invitation to silently omit it.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "src/coordharness/coord/coord_db.py"
DEFAULT_OUTPUT = REPO / "docs/assets/lifecycle.svg"

# Geometry is presentation policy, so it is necessarily declared here. State
# vocabulary is not: every key below is checked against what the source yields.
STATUS_LAYOUT = {
    "unclaimed": (90, 210),
    "released": (90, 310),
    "running": (360, 260),
    "paused": (630, 165),
    "blocked": (630, 260),
    "completed": (630, 355),
}
REQUIRED_FUNCTIONS = (
    "claim_work",
    "heartbeat_claim",
    "release_claim",
    "complete_claim",
)


class DiagramSourceError(ValueError):
    """The lifecycle source cannot be represented without guessing."""


def _assigned_value(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is None:
                break
            return node.value
    raise DiagramSourceError(f"missing lifecycle constant {name}")


def _string_collection(node: ast.AST, name: str) -> set[str]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        if len(node.args) != 1 or node.keywords:
            raise DiagramSourceError(f"{name} must construct frozenset from one literal")
        node = node.args[0]
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise DiagramSourceError(f"{name} must be a literal string collection") from exc
    if not isinstance(value, (tuple, list, set, frozenset)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise DiagramSourceError(f"{name} must contain only non-empty strings")
    return set(value)


def _function_text(source: str, tree: ast.Module, name: str) -> str:
    node = next(
        (item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name),
        None,
    )
    if node is None:
        raise DiagramSourceError(f"missing lifecycle function {name}")
    return ast.get_source_segment(source, node) or ""


def inspect_lifecycle(source_path: Path) -> dict[str, object]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    held = _string_collection(_assigned_value(tree, "_HELD_CLAIM_STATUSES"), "_HELD_CLAIM_STATUSES")
    releasable = _string_collection(
        _assigned_value(tree, "RELEASABLE_CLAIM_STATUSES"),
        "RELEASABLE_CLAIM_STATUSES",
    )
    functions = {name: _function_text(source, tree, name) for name in REQUIRED_FUNCTIONS}

    # These are guards and mutations, not prose copied into a parallel model.
    # Their presence proves the named verbs still implement the transitions the
    # diagram describes. If an implementation changes shape, regeneration fails.
    required_fragments = {
        "claim_work": ("status='running'", "intent_state='running'", "typed handoff"),
        "heartbeat_claim": ('!= "running"', "heartbeat_at=?", "expires_at=?"),
        "release_claim": ("RELEASABLE_CLAIM_STATUSES", "UPDATE claims SET status=?"),
        "complete_claim": ("UPDATE claims SET status=", "intent_state='done'", "done_signal"),
    }
    for name, fragments in required_fragments.items():
        missing = [fragment for fragment in fragments if fragment not in functions[name]]
        if missing:
            raise DiagramSourceError(f"{name} no longer exposes expected lifecycle guard(s): {missing}")

    completion_match = re.search(
        r"UPDATE claims SET status='([^']+)'",
        functions["complete_claim"],
    )
    if completion_match is None:
        raise DiagramSourceError("complete_claim does not expose a literal terminal claim status")
    terminal_status = completion_match.group(1)
    statuses = held | releasable | {terminal_status}
    unknown = sorted(statuses - STATUS_LAYOUT.keys())
    missing = sorted(STATUS_LAYOUT.keys() - statuses)
    if unknown:
        raise DiagramSourceError(f"no lifecycle diagram layout for status(es): {unknown}")
    if missing:
        raise DiagramSourceError(f"diagram layout names status(es) absent from source: {missing}")
    return {
        "held": held,
        "releasable": releasable,
        "statuses": statuses,
        "terminal_status": terminal_status,
    }


def _box(status: str, terminal_status: str) -> str:
    x, y = STATUS_LAYOUT[status]
    css = "terminal" if status == terminal_status else "held" if status in {"running", "paused", "blocked"} else "free"
    return (
        f'  <g class="state {css}" transform="translate({x} {y})">\n'
        '    <rect width="170" height="58" rx="12"/>\n'
        f'    <text x="85" y="35" text-anchor="middle">{status}</text>\n'
        "  </g>"
    )


def render_svg(model: dict[str, object], source_display: str) -> str:
    statuses = sorted(model["statuses"])
    terminal_status = str(model["terminal_status"])
    boxes = "\n".join(_box(status, terminal_status) for status in statuses)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 860 500" role="img" aria-labelledby="title desc">
  <title id="title">Claim lifecycle generated from coord_db.py</title>
  <desc id="desc">The source-defined claim statuses are {", ".join(statuses)}. Claim moves available work to running; heartbeat renews running; release moves it to released, unclaimed, paused, or blocked; complete marks the claim {terminal_status} and the work done only after the declared done signal passes the artifact proof gate.</desc>
  <style>
    :root{{
      --ink:#22292f; --muted:#5b666d; --line:#7d8a91;
      --panel:#f2f5f6; --green:#1F6F63; --amber:#8A6212; --red:#A6392B;
    }}
    @media (prefers-color-scheme: dark){{
      :root{{
        --ink:#dfe6ec; --muted:#9aa4ad; --line:#8b959c;
        --panel:#161c22; --green:#4FC3AC; --amber:#E0B252; --red:#F0887A;
      }}
    }}
    text {{ font-family: ui-sans-serif, system-ui, sans-serif; fill: var(--ink); }}
    .state rect {{ fill: var(--panel); stroke: var(--line); stroke-width: 2; }}
    .state text {{ font-size: 17px; font-weight: 650; }}
    .held rect {{ stroke: var(--green); }}
    .terminal rect {{ stroke: var(--green); stroke-width: 3; }}
    .flow {{ fill: none; stroke: var(--line); stroke-width: 2; marker-end: url(#arrow); }}
    .proof {{ fill: var(--panel); stroke: var(--amber); stroke-width: 2; }}
    .label {{ font-size: 13px; }}
    .caption {{ font-size: 12px; fill: var(--muted); }}
  </style>
  <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 Z" fill="var(--line)"/></marker></defs>
  <text x="32" y="38" font-size="23" font-weight="700">Lifecycle verbs and proof gate</text>
  <path class="flow" d="M260 239 H350"/><text class="label" x="304" y="227" text-anchor="middle">claim_work</text>
  <path class="flow" d="M530 274 H620"/><text class="label" x="575" y="262" text-anchor="middle">release_claim</text>
  <path class="flow" d="M445 250 C445 105 630 105 700 155"/><text class="label" x="500" y="112">pause</text>
  <path class="flow" d="M445 290 C445 405 630 405 700 365"/><text class="label" x="490" y="416">complete_claim</text>
  <path class="flow" d="M445 260 C520 220 520 300 445 278"/><text class="label" x="520" y="244">heartbeat renews lease</text>
  <polygon class="proof" points="545,334 598,365 545,396 492,365"/>
  <text class="label" x="545" y="360" text-anchor="middle">done_signal</text><text class="label" x="545" y="377" text-anchor="middle">proof passes?</text>
{boxes}
  <text class="caption" x="32" y="466">Generated from {source_display}. Arrows carry lifecycle meaning; box positions and distances do not.</text>
  <text class="caption" x="32" y="484">Completion is refused until the controller-declared, Git-custodied artifact exists and satisfies the proof contract.</text>
</svg>
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        model = inspect_lifecycle(args.source)
        source_display = (
            str(args.source.relative_to(REPO)) if args.source.is_relative_to(REPO) else str(args.source)
        )
        rendered = render_svg(model, source_display)
    except (OSError, SyntaxError, DiagramSourceError) as exc:
        parser.exit(2, f"lifecycle diagram: {exc}\n")
    if args.check:
        try:
            observed = args.output.read_text(encoding="utf-8")
        except OSError as exc:
            parser.exit(2, f"lifecycle diagram: cannot read {args.output}: {exc}\n")
        if observed != rendered:
            parser.exit(1, f"lifecycle diagram: {args.output} is stale; regenerate it\n")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
