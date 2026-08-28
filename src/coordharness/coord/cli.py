from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
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
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("session")
    p.add_argument("action", choices=["start", "heartbeat", "end"])
    p.add_argument("--actor", default=None)
    p.add_argument("--runner-type", default=None)

    p = sub.add_parser("claim")
    p.add_argument("work_id")
    p.add_argument("--step", default=None)
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
        _emit({"count": len(rows), "rows": [
            {"work_id": row["work_id"], "title": row.get("title"), "status": row["status"],
             "group": row["group"], "assignee": row.get("assignee")} for row in rows
        ]})
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
            if _work_row(conn, args.work_id):
                raise ValueError(f"work_id {args.work_id!r} already exists")
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
            coord_db.upsert_work(conn, args.work_id, **normalized)
            _emit(
                {
                    "ok": True,
                    "created": True,
                    "work_id": args.work_id,
                    "assignee": normalized["assignee"],
                    "done_signal": normalized["done_signal"],
                    "tier": normalized["tier"],
                }
            )

        elif args.cmd == "work-context":
            row = _work_row(conn, args.work_id)
            if row is None:
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
            policy = _run_lifecycle_policy(
                conn,
                action="claim",
                work_id=args.work_id,
                ident=ident,
                payload={"step": args.step},
            )
            _register_identity_session(conn, ident)
            cid = coord_db.claim_work(conn, sid, args.work_id, step=args.step)
            claim_row = conn.execute(
                "SELECT lease_token FROM claims WHERE claim_id=?", (cid,)
            ).fetchone()
            if claim_row is None or not str(claim_row["lease_token"] or ""):
                raise RuntimeError("new claim is missing its exact custody fence")
            _emit(
                {
                    "ok": True,
                    "claim_id": cid,
                    "claim_fence": str(claim_row["lease_token"]),
                    "work_id": args.work_id,
                    "policy": policy,
                }
            )

        elif args.cmd == "heartbeat-claim":
            work_id = _claim_work_id(conn, args.claim_id)
            policy = _run_lifecycle_policy(
                conn,
                action="heartbeat",
                work_id=work_id,
                ident=ident,
                payload={"step": args.step, "claim_id": args.claim_id},
            )
            coord_db.heartbeat_claim(conn, args.claim_id, step=args.step)
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
            msgs = coord_db.read_inbox(
                conn, recipient_actor=recipient, limit=args.limit,
                newest_first=not args.backlog,
            )
            # Say what was NOT shown. A caller that asks for twenty and gets
            # twenty cannot otherwise tell a drained queue from a truncated one,
            # and mid-flight that is the difference between "nothing arrived"
            # and "I did not look far enough".
            unread = coord_db.unread_inbox_count(conn, recipient_actor=recipient)
            _emit({
                "count": len(msgs),
                "unread_total": unread,
                "not_shown": max(0, unread - len(msgs)),
                "order": "backlog" if args.backlog else "newest_first",
                "messages": [
                    {"id": m["event_id"], "kind": m["kind"], "from": m.get("actor"),
                     "to": m.get("to_selector"), "work_id": m.get("work_id"),
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
            receipt = coord_db.post_note(
                conn,
                work_id=args.work_id,
                actor=sender,
                session_id=sid,
                to_actor=recipient,
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
