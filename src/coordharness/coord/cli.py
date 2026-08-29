from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from coordharness import config as harness_config
from . import coord_db
from .config import connect
from .continuation_contract import (
    normalize_resume_trigger_contract,
    require_park_resume_contract,
)
from .ingest import normalize_session_id, resolve_identity
from .policy.pipeline import run_boundary_policy

AUDIT_VERDICTS = ("PASS", "FLAG", "BLOCKED")
_REF_LIMIT = 32
_REF_BYTES_LIMIT = 2_048
_AUDIT_PAYLOAD_JSON_LIMIT = 12_000


def _emit(obj) -> None:
    print(json.dumps(obj, default=str))


def _raise_if_policy_blocked(policy: dict, *, action: str, work_id: str) -> None:
    if policy.get("blocked"):
        raise ValueError(f"coord policy blocked {action} {work_id}: {policy.get('block_reason')}")


def _claim_work_id(conn, claim_id: str) -> str:
    row = conn.execute("SELECT work_id FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown claim_id {claim_id!r}")
    return str(row["work_id"])


def _work_row(conn, work_id: str) -> dict:
    row = conn.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
    return dict(row) if row is not None else {}


def _register_identity_session(
    conn, ident: dict, *, actor: str | None = None
) -> str:
    label_fields = {
        key: ident.get(key)
        for key in (
            "human_label",
            "external_thread_id",
            "conversation_title",
            "worktree_id",
            "label_source",
        )
        if ident.get(key)
    }
    sid = str(ident["session_id"])
    coord_db.register_session(
        conn,
        sid,
        actor or ident["actor"],
        parent_session_id=ident.get("parent_session_id"),
        runner_type=ident.get("runner_type"),
        cwd=os.getcwd(),
        pid=os.getpid(),
        **label_fields,
    )
    return sid


def _review_identity(ident: dict, *, session: str | None, action: str) -> tuple[str, str]:
    """Resolve the exact reviewing lane for a review verb, or refuse.

    Review attribution is derived by INVERTING the actor: the lane that did not
    author the work is the lane that may clear it. A wrong or approximate actor
    therefore lets a lane pass its own work while every log line still reads
    correct, which is why the MCP surface binds these two verbs to the client
    process identity. The CLI has no caller-asserted actor to spoof -- the
    identity IS the process that ran the command -- so the equivalent guard here
    is to refuse the two identities that are not an exact lane: the ``local``
    fallback actor, and the pid-derived session id that ``resolve_identity``
    returns when no session variable is set. ``--session`` is an assertion that
    must match the process, never an override of it.
    """
    actor = str(ident.get("actor") or "").strip().lower()
    sid = str(ident.get("session_id") or "").strip()
    if actor not in {"claude", "codex"}:
        raise ValueError(
            f"{action} requires an exact coordination lane, got actor "
            f"{actor or '<unset>'!r}; set COORD_ACTOR=claude or COORD_ACTOR=codex"
        )
    if (
        not sid
        or sid.startswith("pid:")
        or ":pid:" in sid
        or sid.startswith("starship:")
        or ":starship:" in sid
    ):
        raise ValueError(
            f"{action} requires an exact client session identity, got "
            f"{sid or '<unset>'!r}; set CLAUDE_CODE_SESSION_ID, CODEX_SESSION_ID "
            "or COORD_SESSION_ID"
        )
    asserted = str(session or "").strip()
    if asserted:
        normalized = normalize_session_id(actor, asserted)
        if normalized != sid:
            raise ValueError(
                f"{action} identity must match this process: expected "
                f"{actor}/{sid}, got {actor}/{normalized}"
            )
    return actor, sid


def _clean_refs(refs, *, action: str) -> list[str]:
    cleaned = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if not cleaned:
        raise ValueError(f"{action} requires at least one evidence --ref")
    if len(cleaned) > _REF_LIMIT:
        raise ValueError(f"{action} refs are bounded to {_REF_LIMIT} pointers")
    if any(len(ref.encode("utf-8")) > _REF_BYTES_LIMIT for ref in cleaned):
        raise ValueError(f"each {action} ref is bounded to {_REF_BYTES_LIMIT} bytes")
    return cleaned


_WRITE_SCOPE_LIMIT = 64


def _parse_write_scopes(raw_scopes, *, action: str) -> list[tuple[str, str]]:
    """Turn ``KIND=VALUE`` (or a bare path) into normalized write scopes.

    ``path`` is the default kind because it is the case the collision problem is
    actually about, and requiring ``path=`` in front of every argument would be
    ceremony on the common call. The split is only honoured when the head is a
    kind this module knows: a path may legitimately contain ``=``, and silently
    treating ``src/a=b.py`` as kind ``src/a`` would refuse a valid scope while
    naming the wrong thing.

    Normalization happens here, before any claim is taken, so an ungrantable
    scope refuses the whole command instead of leaving a claim behind that the
    caller then has to release.
    """
    from .work_contracts import SCOPE_KINDS, normalize_scope

    scopes: list[tuple[str, str]] = []
    for raw in raw_scopes or []:
        text = str(raw).strip()
        if not text:
            continue
        head, sep, tail = text.partition("=")
        if sep and head.strip().lower() in SCOPE_KINDS:
            kind, value = head.strip().lower(), tail
        else:
            kind, value = "path", text
        scope = normalize_scope(kind, value)
        scopes.append(scope.as_tuple())
    if not scopes:
        raise ValueError(
            f"{action} requires at least one --write-scope, for example "
            "--write-scope src/billing/ or --write-scope table=orders"
        )
    if len(scopes) > _WRITE_SCOPE_LIMIT:
        raise ValueError(
            f"{action} write scopes are bounded to {_WRITE_SCOPE_LIMIT} entries; "
            "declare a prefix rather than enumerating files"
        )
    return scopes


def _bounded_audit_payload_json(
    payload: dict,
    *,
    limit: int = _AUDIT_PAYLOAD_JSON_LIMIT,
) -> str:
    """Serialize an audit payload under the same bound as the MCP surface."""
    raw = json.dumps(payload, sort_keys=True, default=str)
    raw_bytes = len(raw.encode("utf-8"))
    if raw_bytes <= limit:
        return raw
    summary = {
        "_truncated": True,
        "_original_bytes": raw_bytes,
        "_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "_keys_sample": sorted(str(key) for key in payload)[:50],
    }
    for key in (
        "schema_version",
        "work_id",
        "kind",
        "verdict",
        "operation_id",
        "operation_request_sha256",
        "output_budget",
    ):
        if key in payload:
            summary[key] = payload[key]
    return json.dumps(summary, sort_keys=True, default=str)


def _verdict_request_sha256(
    *,
    operation_id: str,
    work_id: str,
    actor: str,
    session_id: str,
    to_selector: str,
    verdict: str,
    severity: str | None,
    refs: list[str],
    title: str | None,
) -> str:
    """Canonical hash of one verdict request.

    Field-for-field the envelope the MCP verdict tool hashes, so the same
    operation_id replayed across the two surfaces is recognised as the same
    request instead of being rejected as a colliding one.
    """
    envelope = {
        "schema_version": 1,
        "operation_id": operation_id,
        "work_id": work_id,
        "actor": actor,
        "session_id": session_id,
        "to_selector": to_selector,
        "verdict": verdict,
        "severity": severity,
        "refs": refs,
        "title": title,
        "body_sha256": hashlib.sha256(b"").hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run_lifecycle_policy(conn, *, action: str, work_id: str, ident: dict, payload: dict | None = None) -> dict:
    full_payload = {"run_event_category": "lifecycle", "row": _work_row(conn, work_id)}
    full_payload.update(dict(payload or {}))
    policy = run_boundary_policy(
        boundary="coord_cli",
        action=action,
        work_id=work_id,
        session_id=str(ident.get("session_id") or ""),
        actor=str(ident.get("actor") or ""),
        payload=full_payload,
        conn=conn,
    )
    _raise_if_policy_blocked(policy, action=action, work_id=work_id)
    return policy


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="coord")
    ap.add_argument("--db", default=None)
    # Declared here so `coord --help` lists it and a direct call to this module
    # accepts it. The flag is consumed by the process entry point, which owns
    # the error boundary it turns off, so it never reaches this parser from the
    # installed command and nothing below reads it.
    ap.add_argument("--traceback", action="store_true",
                    help="show the full Python traceback instead of a one-line "
                         "refusal")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("session")
    p.add_argument("action", choices=["start", "heartbeat", "end"])
    p.add_argument("--actor", default=None)
    p.add_argument("--runner-type", default=None)

    p = sub.add_parser("claim")
    p.add_argument("work_id")
    p.add_argument("--step", default=None)
    # Declared with the claim rather than afterwards because the window this
    # protects opens the moment the claim is held. A scope declared two commands
    # later is a scope declared after the first edit.
    p.add_argument("--write-scope", action="append", default=[], dest="write_scopes",
                   metavar="[KIND=]VALUE",
                   help="a scope this claim intends to write, as path=PREFIX, "
                        "table=NAME or service=NAME; a bare value is a path. "
                        "Repeatable")
    p = sub.add_parser(
        "declare-write-set",
        help="declare which scopes a held claim intends to write",
    )
    p.add_argument("claim_id")
    p.add_argument("--write-scope", action="append", required=True, default=[],
                   dest="write_scopes", metavar="[KIND=]VALUE",
                   help="a scope this claim intends to write; repeatable")
    p = sub.add_parser(
        "conflicts",
        help="which currently-held claims declare overlapping write scopes",
    )
    # Read-only and non-blocking by design: it reports, it does not refuse. See
    # docs/agent-protocol.md -- the first version of this deliberately ships
    # with no blocking behaviour, because a naive block would stop plenty of
    # harmless concurrent work.
    p.add_argument("--include-expired", action="store_true",
                   help="also scan claims whose lease has already expired")
    p = sub.add_parser(
        "create",
        help="create the first or next proof-gated work item in this coord database",
    )
    p.add_argument("work_id", help="durable work id (for example DEMO-CDX-RUNTIME-CHECK)")
    p.add_argument("--title", required=True, help="descriptive work title")
    p.add_argument("--display", default=None, help="short board label; defaults to title")
    p.add_argument("--assignee", default=None, help="owner lane; defaults to the current actor")
    p.add_argument("--module", required=True, help="specific module or workstream")
    p.add_argument("--sublane", default=None)
    p.add_argument("--surface", choices=("epic", "job", "task"), default="job")
    p.add_argument("--parent", default=None, help="parent work id (required for a task without dependencies)")
    p.add_argument("--depends-on", action="append", default=[], dest="depends_on")
    p.add_argument("--done-signal", required=True, help="repo-relative proof path")
    p.add_argument(
        "--acceptance",
        action="append",
        required=True,
        help="acceptance criterion; repeat for multiple criteria",
    )
    p.add_argument("--note", required=True, help="why this work item exists")
    p.add_argument("--context-pack-ref", default=None)
    p.add_argument("--tier", choices=("T0", "T1", "T2"), default=None)
    p.add_argument("--priority", type=int, default=None)
    p = sub.add_parser("work-context", help="read one exact row and its typed-handoff fences")
    p.add_argument("work_id")
    p = sub.add_parser("handoff", help="typed, fenced transfer of existing work")
    p.add_argument("work_id")
    p.add_argument("--owner-lane", required=True, choices=("claude", "codex"))
    p.add_argument("--task", required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--acceptance", required=True)
    p.add_argument("--operation-id", required=True)
    p.add_argument("--expected-version", required=True, type=int)
    p.add_argument("--expected-assignee", required=True, choices=("claude", "codex"))
    p.add_argument("--expected-head-event-id", action="append", type=int, default=[])
    p.add_argument("--ref", action="append", required=True)
    p.add_argument("--constraint", action="append", required=True)
    p.add_argument("--target-intent", choices=("queued", "blocked"), default="queued")
    p = sub.add_parser("release")
    p.add_argument("claim_id")
    p.add_argument(
        "--status",
        default="released",
        choices=sorted(coord_db.RELEASABLE_CLAIM_STATUSES),
    )
    p.add_argument("--next-step", default=None)
    p.add_argument("--resume-when", default=None)
    release_trigger = p.add_mutually_exclusive_group()
    release_trigger.add_argument("--resume-predicate", default=None)
    release_trigger.add_argument("--resume-manual", action="store_true")
    p = sub.add_parser("done")
    p.add_argument("work_id")
    p.add_argument("--artifact", default=None)
    p = sub.add_parser("board")
    p.add_argument("--group-by", default="module")
    p = sub.add_parser("inbox", help="messages addressed to this actor")
    p.add_argument("--actor", default=None)
    p.add_argument("--limit", type=int, default=20)
    # Newest first is the default because the question an agent asks mid-run is
    # "did anything arrive while I was working", and the oldest-first reading
    # puts exactly that message outside the window. --backlog restores queue
    # order for draining a backlog in the order it was written.
    p.add_argument("--backlog", action="store_true",
                   help="read oldest-first, in queue order, instead of newest-first")
    # Opt-in, never the default. Broadcasts are legitimate traffic; the defect
    # was that they were indistinguishable from a message addressed here, not
    # that they were shown. Making this the default would only move the silence.
    p.add_argument("--directed", action="store_true",
                   help="show only messages addressed to this actor, not board-wide "
                        "broadcasts")

    p = sub.add_parser("route", help="which provider has headroom, on measured usage")
    p.add_argument("--usage-db", required=True,
                   help="path to the usage ledger to read (never written)")
    p.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    p.add_argument("--budget", action="append", default=[], dest="budgets",
                   metavar="PROVIDER=TOKENS",
                   help="declared token allowance for this window; repeatable")
    p.add_argument("--require-complete", action="store_true",
                   help="exclude any provider whose coverage is incomplete instead of "
                        "reporting its total as a floor")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("note", help="send a mid-flight message to the other lane")
    p.add_argument("work_id", help="an existing row the message is about")
    p.add_argument("--body", required=True, help="what the other lane needs to know")
    p.add_argument("--title", default=None)
    p.add_argument("--ref", action="append", default=[], dest="refs",
                   help="pointer to the evidence; repeatable")
    p.add_argument("--to", default=None, choices=("claude", "codex"),
                   help="recipient lane; defaults to the row's other lane")
    # A lane address reaches every session in that lane, which is the right
    # default and the wrong one for a fleet: three concurrent claude sessions
    # all match actor:claude, so the message is everyone's and therefore
    # nobody's. Naming a session narrows it to the one that needs it.
    p.add_argument("--to-session", default=None, metavar="SESSION_ID",
                   help="recipient session id, narrower than --to; the session "
                        "must exist and be live or the note is refused")
    p = sub.add_parser(
        "verdict",
        help="record this lane's independent review verdict on the other lane's work",
    )
    p.add_argument("work_id", help="the row under review")
    p.add_argument("--verdict", required=True, choices=AUDIT_VERDICTS)
    p.add_argument("--severity", default=None)
    p.add_argument("--ref", action="append", required=True, default=[], dest="refs",
                   help="pointer to the evidence actually read; repeatable, at least one")
    p.add_argument("--to-lane", default=None, choices=("claude", "codex"),
                   help="the authoring lane under review; defaults to the lane this "
                        "reviewer is not")
    p.add_argument("--title", default=None)
    p.add_argument(
        "--operation-id",
        default=None,
        help="stable id for replay safety; defaults to a hash of this exact request, "
             "so re-running the identical command replays instead of double-posting",
    )
    p.add_argument("--session", default=None,
                   help="assert the reviewing session id; must match this process")
    p = sub.add_parser(
        "request-audit",
        help="ask the other lane to review an existing row of yours",
    )
    p.add_argument("work_id", help="an existing row this lane authored")
    p.add_argument("--task", required=True, help="what the reviewer is being asked to check")
    p.add_argument("--why", required=True, help="why this needs independent eyes")
    p.add_argument("--ref", action="append", required=True, default=[], dest="refs",
                   help="pointer to the evidence to review; repeatable, at least one")
    p.add_argument("--acceptance", default=None)
    p.add_argument("--session", default=None,
                   help="assert the requesting session id; must match this process")
    p = sub.add_parser("heartbeat-claim")
    p.add_argument("claim_id")
    p.add_argument("--step", default=None)
    p = sub.add_parser("doctor", help="run read-only safety and integrity checks")
    p.add_argument("--project-root", default=None)
    p.add_argument("--state-root", default=None)
    p.add_argument("--mcp-config", action="append", default=[])
    p.add_argument("--now", type=float, default=None)
    p = sub.add_parser(
        "onboard",
        help="verify clean-room agent instructions, configs, database, and MCP wiring",
    )
    p.add_argument("--project-root", default=None)
    p.add_argument("--write-configs", action="store_true",
                   help="create portable .codex/config.toml and .mcp.json when absent")
    p.add_argument("--register-clients", action="store_true",
                   help="idempotently register missing installed Codex/Claude MCP clients")
    p.add_argument("--skip-client-probes", action="store_true")
    p.add_argument("--skip-mcp-probe", action="store_true")

    args = ap.parse_args(argv)
    from coordharness.bootstrap import bootstrap_database, database_current

    if args.cmd == "onboard":
        from .onboarding import register_clients, run_onboarding_doctor, write_portable_configs

        root = (Path(args.project_root).expanduser().resolve()
                if args.project_root else harness_config.project_root())
        config_write = write_portable_configs(root) if args.write_configs else None
        client_registration = register_clients(root) if args.register_clients else None
        db_path = Path(args.db) if args.db is not None else root / ".coordharness" / "coord.db"
        report = run_onboarding_doctor(
            project_root=root,
            db_path=db_path,
            probe_clients=not args.skip_client_probes,
            probe_mcp=not args.skip_mcp_probe,
        )
        if config_write is not None:
            report["read_only"] = False
            report["config_write"] = config_write
            if not config_write["ok"]:
                report["status"] = "BLOCKED"
        if client_registration is not None:
            report["read_only"] = False
            report["client_registration"] = client_registration
            if not client_registration["ok"]:
                report["status"] = "BLOCKED"
        _emit(report)
        return 0 if report["status"] == "PASS" else 2

    if args.cmd == "doctor":
        from coordharness.safety.doctor import run_doctor

        db_path = Path(args.db) if args.db is not None else harness_config.coord_db_path()
        report = run_doctor(
            db_path=db_path,
            project_root=(Path(args.project_root) if args.project_root else harness_config.project_root()),
            state_root=(Path(args.state_root) if args.state_root else harness_config.state_dir()),
            mcp_config_paths=args.mcp_config,
            now=args.now,
        )
        _emit(report)
        return 0 if report["status"] == "PASS" else 2

    if args.cmd == "board":
        db_path = Path(args.db) if args.db is not None else harness_config.coord_db_path()
        if not database_current(db_path):
            bootstrap_database(db_path)
        from coordharness.board.snapshot import _materialized_connection

        with _materialized_connection(db_path) as read_conn:
            rows = coord_db.board_rows(read_conn, group_by=args.group_by)
        # `assignee` is the LANE the row belongs to, and three concurrent
        # claude sessions all render as "claude" -- which is the whole board
        # for a fleet, printed as one name. The owner fields say which session
        # is actually holding the row: v_work_owner already derives
        # owner_session_label as human_label, then conversation_title, then the
        # actor, so it degrades back to the lane name only when nothing better
        # was ever registered. `assignee` stays exactly where it was, because
        # everything downstream reads it.
        _emit({"count": len(rows), "rows": [
            {"work_id": row["work_id"], "title": row.get("title"), "status": row["status"],
             "group": row["group"], "assignee": row.get("assignee"),
             "owner_session_id": row.get("owner_session_id"),
             "owner_session_actor": row.get("owner_session_actor"),
             "owner_session_label": row.get("owner_session_label")} for row in rows
        ]})
        return 0

    if args.cmd == "conflicts":
        from .config import connect_ro
        from .work_contracts import write_set_overlaps

        db_path = Path(args.db) if args.db is not None else harness_config.coord_db_path()
        if not database_current(db_path):
            bootstrap_database(db_path)
        # Opened read-only on purpose. Asking who collides must not be able to
        # change who collides, and the query used to reach for a CREATE TABLE on
        # a database that had never carried a declaration -- which failed here,
        # on exactly this kind of connection.
        read_conn = connect_ro(db_path)
        try:
            report = write_set_overlaps(
                read_conn, include_expired=args.include_expired
            )
        finally:
            read_conn.close()
        _emit({
            "ok": True,
            **report.as_dict(),
            "include_expired": bool(args.include_expired),
        })
        # Advisory, so a detected overlap is still a successful read. The exit
        # code says the question was answered, not that the board is clean; the
        # answer is in `count`.
        return 0

    bootstrap_database(args.db)
    ident = resolve_identity()
    sid = ident["session_id"]
    conn = connect(args.db)
    try:
        if args.cmd == "session":
            if args.action == "start":
                if args.runner_type:
                    ident = {**ident, "runner_type": args.runner_type}
                _register_identity_session(conn, ident, actor=args.actor)
                _emit({"ok": True, "session_id": sid, "actor": args.actor or ident["actor"]})
            elif args.action == "heartbeat":
                coord_db.renew_lease(conn, sid)
                _emit({"ok": True, "session_id": sid})
            else:
                coord_db.end_session(conn, sid)
                _emit({"ok": True, "ended": sid})

        elif args.cmd == "create":
            # No pre-check here on purpose. The one this replaced ran outside
            # the write transaction, so two sessions that picked the same
            # date-and-lane id both read "free", both wrote, and the loser was
            # told "created": true while its content was overwritten. The
            # collision is now surfaced by coord_db.create_work from inside the
            # transaction that does the insert, which is the only place the
            # answer cannot go stale between the check and the write.
            from .creation_lint import normalize_creation_fields

            _register_identity_session(conn, ident)
            fields = {
                "title": args.title,
                "display": args.display,
                "assignee": args.assignee or ident["actor"],
                "assigned_by": ident["actor"],
                "module": args.module,
                "sublane": args.sublane,
                "surface": args.surface,
                "parent_id": args.parent,
                "depends_on": (
                    json.dumps(args.depends_on, ensure_ascii=True)
                    if args.depends_on
                    else None
                ),
                "done_signal": args.done_signal,
                "acceptance_json": json.dumps(args.acceptance, ensure_ascii=True),
                "note": args.note,
                "context_pack_ref": args.context_pack_ref,
                "tier": args.tier,
                "priority": args.priority,
                "intent_state": "queued",
                "created_by_session_id": sid,
                "authority_declaration_json": coord_db.new_work_quarantine_declaration(
                    args.work_id,
                    source_kind="public_cli_create",
                ),
            }
            fields = {
                key: value for key, value in fields.items() if value not in (None, "")
            }
            normalized = normalize_creation_fields(
                args.work_id,
                fields,
                source="coord create",
            )
            created = coord_db.create_work(conn, args.work_id, **normalized)
            _emit(
                {
                    "ok": True,
                    # The measured outcome of the insert, not an assertion that
                    # one happened.
                    "created": created,
                    "work_id": args.work_id,
                    "assignee": normalized["assignee"],
                    "done_signal": normalized["done_signal"],
                    "tier": normalized["tier"],
                }
            )

        elif args.cmd == "work-context":
            row = _work_row(conn, args.work_id)
            # _work_row returns an empty dict for a missing row, never None, so
            # an `is None` test here never fired and an unknown id fell through
            # to `row["version"]` below as a bare KeyError.
            if not row:
                _emit({
                    "ok": False,
                    "work_id": args.work_id,
                    "error": {
                        "code": "work_not_found",
                        "message": f"work_id {args.work_id!r} is not present in coord.db",
                    },
                })
                return 2
            head = coord_db._typed_handoff_head_state_unlocked(conn, args.work_id)
            active_ids = list(head["active_event_ids"])
            _emit({
                "ok": True,
                "work": {
                    key: row.get(key)
                    for key in (
                        "work_id", "title", "intent_state", "assignee", "module",
                        "done_signal", "acceptance_json", "context_pack_ref", "version",
                    )
                },
                "handoff_preconditions": {
                    "expected_version": int(row["version"]),
                    "expected_assignee": str(row.get("assignee") or "").strip().lower(),
                    "expected_head_event_ids": active_ids,
                    "writer_head_set_eligible": len(active_ids) <= 64,
                },
            })

        elif args.cmd == "handoff":
            _register_identity_session(conn, ident)
            policy = _run_lifecycle_policy(
                conn,
                action="handoff",
                work_id=args.work_id,
                ident=ident,
                payload={"target_intent": args.target_intent},
            )
            result = coord_db.post_existing_work_handoff(
                conn,
                work_id=args.work_id,
                actor=ident["actor"],
                session_id=sid,
                owner_lane=args.owner_lane,
                target_intent=args.target_intent,
                task=args.task,
                why=args.why,
                acceptance=args.acceptance,
                refs=args.ref,
                constraints=args.constraint,
                operation_id=args.operation_id,
                expected_version=args.expected_version,
                expected_assignee=args.expected_assignee,
                expected_head_event_ids=args.expected_head_event_id,
            )
            _emit({
                "ok": True,
                **coord_db.compact_existing_work_handoff_result(result),
                "policy": policy,
            })

        elif args.cmd == "claim":
            # Parsed before the claim is taken. normalize_scope refuses an
            # ungrantable scope by raising, and a refusal after the insert would
            # leave the caller holding a claim it did not get to declare.
            write_scopes = (
                _parse_write_scopes(args.write_scopes, action="coord claim")
                if args.write_scopes else []
            )
            policy = _run_lifecycle_policy(
                conn,
                action="claim",
                work_id=args.work_id,
                ident=ident,
                payload={"step": args.step},
            )
            _register_identity_session(conn, ident)
            try:
                cid = coord_db.claim_work(conn, sid, args.work_id, step=args.step)
            except sqlite3.IntegrityError as exc:
                # The one-held-claim unique index is the only integrity rule
                # this insert can plausibly trip, but "only" is an assumption,
                # so it is confirmed by reading the holder back rather than
                # translated on faith. No live claim means something else broke,
                # and that keeps its stack.
                holder = conn.execute(
                    "SELECT claim_id, session_id FROM claims WHERE work_id=?"
                    " AND status IN ('running','paused','blocked')"
                    " ORDER BY acquired_at DESC LIMIT 1",
                    (args.work_id,),
                ).fetchone()
                if holder is None:
                    raise
                raise ValueError(
                    f"work {args.work_id!r} is already claimed by session "
                    f"{str(holder['session_id'])!r} (claim "
                    f"{str(holder['claim_id'])}); that session must release it "
                    "before another can take it"
                ) from exc
            claim_row = conn.execute(
                "SELECT lease_token FROM claims WHERE claim_id=?", (cid,)
            ).fetchone()
            if claim_row is None or not str(claim_row["lease_token"] or ""):
                raise RuntimeError("new claim is missing its exact custody fence")
            result = {
                "ok": True,
                "claim_id": cid,
                "claim_fence": str(claim_row["lease_token"]),
                "work_id": args.work_id,
                "policy": policy,
            }
            if write_scopes:
                from .work_contracts import declare_write_set, write_set_overlaps

                declared = declare_write_set(conn, claim_id=cid, scopes=write_scopes)
                result["write_set"] = [
                    {"kind": scope.kind, "value": scope.value} for scope in declared
                ]
                # Reported, never enforced. The point of declaring at claim time
                # is to find out before the first edit; deciding what to do about
                # an overlap is the agents' call, not this command's.
                overlaps = write_set_overlaps(conn)
                mine = [
                    finding for finding in overlaps.findings
                    if cid in (finding.claim_a, finding.claim_b)
                ]
                result["write_set_conflicts"] = {
                    "count": len(mine),
                    "findings": [finding.describe() for finding in mine],
                }
            _emit(result)

        elif args.cmd == "declare-write-set":
            from .work_contracts import declare_write_set, write_set_overlaps

            scopes = _parse_write_scopes(
                args.write_scopes, action="coord declare-write-set"
            )
            # Resolve the work id first: it refuses an unknown claim id with the
            # message the rest of the CLI uses, rather than the one the contract
            # module raises for its own callers.
            work_id = _claim_work_id(conn, args.claim_id)
            declared = declare_write_set(conn, claim_id=args.claim_id, scopes=scopes)
            overlaps = write_set_overlaps(conn)
            mine = [
                finding for finding in overlaps.findings
                if args.claim_id in (finding.claim_a, finding.claim_b)
            ]
            _emit({
                "ok": True,
                "claim_id": args.claim_id,
                "work_id": work_id,
                "write_set": [
                    {"kind": scope.kind, "value": scope.value} for scope in declared
                ],
                "write_set_conflicts": {
                    "count": len(mine),
                    "findings": [finding.describe() for finding in mine],
                },
            })

        elif args.cmd == "heartbeat-claim":
            work_id = _claim_work_id(conn, args.claim_id)
            policy = _run_lifecycle_policy(
                conn,
                action="heartbeat",
                work_id=work_id,
                ident=ident,
                payload={"step": args.step, "claim_id": args.claim_id},
            )
            coord_db.heartbeat_claim(
                conn,
                args.claim_id,
                step=args.step,
                session_id=sid,
                actor=ident["actor"],
            )
            _emit({"ok": True, "policy": policy})

        elif args.cmd == "release":
            next_step = args.next_step
            resume_when = args.resume_when
            if args.status == "paused":
                next_step, resume_when = require_park_resume_contract(
                    next_step=next_step,
                    resume_when=resume_when,
                )
            canonical_resume_predicate = None
            if args.status in {"paused", "blocked"}:
                canonical_resume_predicate = normalize_resume_trigger_contract(
                    resume_when=resume_when,
                    resume_predicate=args.resume_predicate,
                    resume_manual=args.resume_manual,
                )
            work_id = _claim_work_id(conn, args.claim_id)
            policy = _run_lifecycle_policy(
                conn,
                action="release",
                work_id=work_id,
                ident=ident,
                payload={
                    "status": args.status,
                    "claim_id": args.claim_id,
                    "next_step": next_step,
                    "resume_when": resume_when,
                    "resume_predicate_json": canonical_resume_predicate,
                },
            )
            coord_db.release_claim(
                conn,
                args.claim_id,
                status=args.status,
                next_step=next_step,
                resume_when=resume_when,
                resume_predicate_json=args.resume_predicate,
                resume_manual=args.resume_manual,
                session_id=sid,
                actor=ident["actor"],
            )
            _emit({
                "ok": True,
                "resume_predicate_json": canonical_resume_predicate,
                "policy": policy,
            })

        elif args.cmd == "done":
            row = conn.execute(
                "SELECT 1 FROM work_items WHERE work_id=?",
                (args.work_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown work_id {args.work_id!r}")
            claim = conn.execute(
                "SELECT claim_id FROM claims WHERE work_id=?"
                " AND session_id=?"
                " AND status IN ('running','paused','blocked')"
                " ORDER BY acquired_at DESC LIMIT 1",
                (args.work_id, sid),
            ).fetchone()
            if claim is None:
                raise ValueError(
                    f"coord done requires this session to hold a claim and artifact proof for {args.work_id!r}; "
                    "claim the work before completing it"
                )
            policy = _run_lifecycle_policy(
                conn,
                action="done",
                work_id=args.work_id,
                ident=ident,
                payload={"artifact_path": args.artifact, "claim_id": claim["claim_id"]},
            )
            proof = coord_db.complete_claim(
                conn,
                claim["claim_id"],
                artifact_path=args.artifact,
                receipt_source="coordharness.coord.cli.done",
                session_id=sid,
                actor=ident["actor"],
            )
            receipt = conn.execute(
                "SELECT event_id FROM events WHERE idempotency_key=?",
                (f"coord-complete:{claim['claim_id']}",),
            ).fetchone()
            if receipt is None:
                raise RuntimeError("completed claim is missing its canonical atomic receipt")
            _emit({
                "ok": True,
                "work_id": args.work_id,
                "artifact_path": proof,
                "canonical_event_id": int(receipt["event_id"]),
                "policy": policy,
            })

        elif args.cmd == "inbox":
            recipient = args.actor or ident["actor"]
            # Bind the reading to this session when the reader IS this session,
            # so a note addressed to `session:<id>` is visible here and not only
            # over MCP. Under --actor for another lane the process session is
            # not that reader's identity, and passing it would silently mix one
            # session's directed mail into another lane's reading.
            reader_session = sid if recipient == ident["actor"] else ""
            msgs = coord_db.read_inbox(
                conn, recipient_actor=recipient, session_id=reader_session,
                limit=args.limit,
                newest_first=not args.backlog, directed_only=args.directed,
            )
            # Say what was NOT shown. A caller that asks for twenty and gets
            # twenty cannot otherwise tell a drained queue from a truncated one,
            # and mid-flight that is the difference between "nothing arrived"
            # and "I did not look far enough".
            #
            # Split by whom the message named. One total cannot answer "did
            # anything arrive for me": on a working board the broadcasts
            # outnumber the directed messages many times over, so a single
            # figure buries the one event that was actually addressed here.
            unread = coord_db.unread_inbox_counts(
                conn, recipient_actor=recipient, session_id=reader_session
            )
            # Under --directed the broadcast leg was never eligible to be shown,
            # so counting it as "not shown" would report a backlog that this
            # reading is not waiting on.
            scope_unread = unread["directed"] if args.directed else unread["total"]
            _emit({
                "count": len(msgs),
                "unread_total": unread["total"],
                "directed_unread": unread["directed"],
                "broadcast_unread": unread["broadcast"],
                "not_shown": max(0, scope_unread - len(msgs)),
                "order": "backlog" if args.backlog else "newest_first",
                "scope": "directed" if args.directed else "all",
                "messages": [
                    {"id": m["event_id"], "kind": m["kind"], "from": m.get("actor"),
                     "to": m.get("to_selector"), "directed": m.get("directed"),
                     "work_id": m.get("work_id"),
                     "title": m.get("title"), "body": m.get("body")} for m in msgs],
            })

        elif args.cmd == "route":
            # Advice only. This reads the usage ledger and prints a
            # recommendation; it never edits a client config, a board row, or
            # the ledger itself.
            from ..usage.ledger import UsageLedger
            from ..usage import routing

            budgets: dict[str, int] = {}
            for pair in args.budgets:
                name, _, raw = str(pair).partition("=")
                if not name.strip() or not raw.strip().isdigit():
                    raise SystemExit(f"--budget expects PROVIDER=TOKENS, got {pair!r}")
                budgets[name.strip()] = int(raw)

            if not Path(args.usage_db).expanduser().exists():
                # This ledger is only ever read, so a path that does not exist
                # is a typo rather than a store to create. Left to UsageLedger
                # it becomes an OSError about the parent directory, which names
                # the wrong thing entirely.
                raise ValueError(
                    f"coord route --usage-db {args.usage_db!r} does not exist; "
                    "pass the path of an existing usage ledger"
                )
            ledger = UsageLedger(args.usage_db)
            try:
                advice = routing.advise_from_ledger(
                    ledger, budgets, days=args.days,
                    require_complete=args.require_complete,
                )
            finally:
                ledger.close()
            if args.as_json:
                _emit(advice.as_dict())
            else:
                print(routing.render(advice))

        elif args.cmd == "note":
            sender = ident["actor"]
            # Default the recipient to the other lane: a mid-flight note is
            # almost always addressed across the pen split, and an unaddressed
            # note is a broadcast that everyone skims and nobody answers.
            recipient = args.to or ("codex" if sender == "claude" else "claude")
            target_session = str(args.to_session or "").strip()
            receipt = coord_db.post_note(
                conn,
                work_id=args.work_id,
                actor=sender,
                session_id=sid,
                # Passed alongside, not instead of: post_note prefers the
                # session when both are set, and reporting the lane the session
                # belongs to keeps the emitted `to` field meaningful either way.
                to_actor=recipient,
                to_session_id=target_session,
                title=args.title,
                body=args.body,
                refs=args.refs,
            )
            _emit({
                "ok": True,
                "event_id": receipt["event_id"],
                "work_id": args.work_id,
                "from": sender,
                "to": recipient,
                "to_session": target_session or None,
                "to_selector": receipt["to_selector"],
            })

        elif args.cmd == "verdict":
            from .agent_cli import audit_verdict_payload

            reviewer, reviewer_sid = _review_identity(
                ident, session=args.session, action="coord verdict"
            )
            refs = _clean_refs(args.refs, action="coord verdict")
            # The author lane is the lane the reviewer is not. Naming it
            # explicitly is allowed, naming your own lane is not: a verdict is
            # cross-lane review, and coord_db refuses a same-lane PASS outright.
            author_lane = args.to_lane or ("claude" if reviewer == "codex" else "codex")
            if author_lane == reviewer:
                raise ValueError(
                    f"coord verdict --to-lane {author_lane} is this reviewer's own "
                    "lane; a verdict is independent cross-lane review"
                )
            to_selector = f"actor:{author_lane}"
            hash_fields = {
                "work_id": args.work_id,
                "actor": reviewer,
                "session_id": reviewer_sid,
                "to_selector": to_selector,
                "verdict": args.verdict,
                "severity": args.severity,
                "refs": refs,
                "title": args.title,
            }
            operation_id = str(args.operation_id or "").strip() or (
                "cli-verdict-"
                + _verdict_request_sha256(operation_id="", **hash_fields)
            )
            request_sha256 = _verdict_request_sha256(
                operation_id=operation_id, **hash_fields
            )
            _register_identity_session(conn, ident)
            policy = _run_lifecycle_policy(
                conn,
                action="verdict",
                work_id=args.work_id,
                ident=ident,
                payload={"verdict": args.verdict, "to_selector": to_selector},
            )
            payload = audit_verdict_payload(
                work_id=args.work_id,
                actor=reviewer,
                verdict_value=args.verdict,
                severity=args.severity,
                refs=refs,
                receiver_lane=author_lane,
                session_id=reviewer_sid,
                source="coordharness.coord.cli.verdict",
            )
            payload["operation_id"] = operation_id
            payload["operation_request_sha256"] = request_sha256
            result = coord_db.post_audit_verdict(
                conn,
                work_id=args.work_id,
                verdict=args.verdict,
                actor=reviewer,
                session_id=reviewer_sid,
                to_selector=to_selector,
                severity=args.severity,
                trust="agent",
                title=args.title,
                refs_json=json.dumps(refs, ensure_ascii=True),
                payload_json=_bounded_audit_payload_json(payload),
                operation_id=operation_id,
                request_sha256=request_sha256,
            )
            # Report the selector the event actually carries, not the one asked
            # for: coord_db rebinds it to the lane that authored the latest
            # claim, and a caller told otherwise cannot see that it did.
            recorded = conn.execute(
                "SELECT to_selector FROM events WHERE event_id=?",
                (int(result["event_id"]),),
            ).fetchone()
            _emit({
                "ok": True,
                "verb": "verdict",
                "work_id": args.work_id,
                "verdict": result["verdict"],
                "event_id": int(result["event_id"]),
                "operation_id": result["operation_id"],
                "reviewer": reviewer,
                "to_selector": (
                    str(recorded["to_selector"]) if recorded is not None
                    and recorded["to_selector"] else to_selector
                ),
                "refs": refs,
                "replayed": bool(result.get("replayed")),
                "work": result.get("work"),
                "policy": policy,
            })

        elif args.cmd == "request-audit":
            requester, requester_sid = _review_identity(
                ident, session=args.session, action="coord request-audit"
            )
            refs = _clean_refs(args.refs, action="coord request-audit")
            task = str(args.task or "").strip()
            why = str(args.why or "").strip()
            if not task:
                raise ValueError("coord request-audit requires a non-empty --task")
            if not why:
                raise ValueError("coord request-audit requires a non-empty --why")
            target_lane = "codex" if requester == "claude" else "claude"
            _register_identity_session(conn, ident)
            row = conn.execute(
                "SELECT assignee FROM work_items WHERE work_id=?", (args.work_id,)
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"coord request-audit work_id not found: {args.work_id}; audit "
                    "requests are events on an existing author row"
                )
            assignee = str(row["assignee"] or "").strip().lower()
            if assignee in {"claude", "codex"} and assignee == target_lane:
                raise ValueError(
                    f"coord request-audit self-target refused: work {args.work_id} is "
                    f"assigned to {assignee}, the same lane this request targets "
                    f"({target_lane}); a lane cannot be asked to audit its own work "
                    "-- request cross-eyes on work your lane owns"
                )
            policy = _run_lifecycle_policy(
                conn,
                action="request_audit",
                work_id=args.work_id,
                ident=ident,
                payload={"request_kind": "standard", "to_selector": f"actor:{target_lane}"},
            )
            # Only the standard request is offered here. coord_db's flag_repair
            # variant additionally requires a remediation-evidence event bound
            # to the negative verdict by event id, and no CLI verb can post one,
            # so exposing it from this surface would be a flag that can only
            # ever fail. It stays MCP-only until the CLI can produce that event.
            event_id = int(coord_db.post_event(
                conn,
                kind="audit_request",
                actor=requester,
                session_id=requester_sid,
                to_selector=f"actor:{target_lane}",
                work_id=args.work_id,
                refs_json=json.dumps(refs, ensure_ascii=True),
                payload_json=json.dumps(
                    {
                        "task": task,
                        "why": why,
                        "acceptance": str(args.acceptance or "").strip() or None,
                        "schema_version": 1,
                        "source": "coordharness.coord.cli.request_audit",
                        "event_only": True,
                    },
                    sort_keys=True,
                ),
            ))
            _emit({
                "ok": True,
                "verb": "request_audit",
                "work_id": args.work_id,
                "event_id": event_id,
                "request_kind": "standard",
                "from": requester,
                "to_selector": f"actor:{target_lane}",
                "target_lane": target_lane,
                "assignee_unchanged": assignee or None,
                "policy": policy,
            })
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
