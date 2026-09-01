from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from coordharness import config as harness_config

from . import coord_db
from .config import lane_set as _lane_set
from .process_liveness import pid_matches

_logger = logging.getLogger(__name__)

_LEGACY_CODEX_PRIMARY_SID = "codex:primary"
_SAFE_SESSION_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_GROUPING_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")

_STATUS_TO_INTENT = {
    "running": "running", "in_progress": "running", "claimed": "running",
    "blocked": "blocked", "done": "done", "complete": "done", "completed": "done",
    "failed": "failed", "error": "failed", "queued": "queued", "planned": "planned",
    "archived": "archived", "superseded": "superseded", "closed": "closed",
    "skipped": "closed", "stopped": "archived", "cancelled": "cancelled",
    "canceled": "cancelled",
}
_BACKLOG_DEMOTION_EVENT_KINDS = {
    "audit_verdict",
    "block",
    "blocked",
    "claim",
    "claim_conflict",
    "claude_block",
    "claude_claim",
    "claude_done",
    "claude_heartbeat",
    "claude_park",
    "codex_block",
    "codex_claim",
    "codex_done",
    "codex_heartbeat",
    "codex_park",
    "complete",
    "completed",
    "done",
    "failed",
    "handoff",
    "park",
    "parked",
    "release",
    "released",
    "rubric_verdict",
}


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def ingest_demote_intent_writes_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return _truthy(source.get("COORD_R6_INGEST_DEMOTE_INTENT_WRITES"))


def _session_component(value: Any) -> str:
    clean = _SAFE_SESSION_COMPONENT_RE.sub("-", str(value or "").strip()).strip(".-")
    return clean or "unknown"


def normalize_session_id(actor: str, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    clean_actor = harness_config.actor_name(actor)
    lowered = raw.lower()
    if lowered.startswith(f"{clean_actor}:"):
        suffix = raw.split(":", 1)[1].strip()
        return f"{clean_actor}:{suffix}" if suffix else raw
    for prefix in (f"{clean_actor}-", f"{clean_actor}/"):
        if lowered.startswith(prefix):
            suffix = raw[len(prefix):].strip()
            return f"{clean_actor}:{suffix}" if suffix else raw
    if ":" in raw:
        return raw
    return f"{clean_actor}:{raw}"


def _codex_session_id(env: dict) -> str:
    explicit = str(env.get("CODEX_SESSION_ID") or "").strip()
    if explicit and explicit != _LEGACY_CODEX_PRIMARY_SID:
        return explicit
    candidates = (
        ("thread", env.get("CODEX_THREAD_ID")),
        ("worktree", env.get("CODEX_WORKTREE_ID")),
        ("conversation", env.get("CODEX_CONVERSATION_ID")),
        ("starship", env.get("STARSHIP_SESSION_KEY")),
    )
    for label, value in candidates:
        if str(value or "").strip():
            return f"codex:{label}:{_session_component(value)}"
    return f"codex:pid:{os.getpid()}"


def _fallback_human_label(actor: str, session_id: str) -> str:
    component = session_id.split(":")[-1] if ":" in session_id else session_id
    component = component.strip() or "session"
    if len(component) > 12:
        component = component[-12:]
    return f"{actor.capitalize()} {component}"


#: Variables that only a Codex runtime sets. Deliberately EXCLUDES
#: ``STARSHIP_SESSION_KEY``, which the Starship shell prompt sets in any
#: terminal -- including one running Claude. It is weak enough to help *infer*
#: Codex when nothing else is present, but far too weak to declare a conflict.
_CODEX_IDENTITY_KEYS = (
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_WORKTREE_ID",
    "CODEX_CONVERSATION_ID",
)


def _has_codex_identity(env: dict) -> bool:
    """True when the environment carries a genuine Codex session identity."""
    return any(str(env.get(key) or "").strip() for key in _CODEX_IDENTITY_KEYS)


def _git_worktree_dirs(cwd: str) -> tuple[str, str] | None:
    """``(this worktree's git dir, the repository's common git dir)`` or None.

    ``cwd`` is used only to ASK git which worktree the process is in; the
    directory itself never becomes part of the answer.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    return lines[0], lines[1]


@lru_cache(maxsize=8)
def _linked_worktree_id(cwd: str) -> str | None:
    """Git's own identity for a LINKED worktree, or ``None``.

    ``worktree_id`` is a BRIDGE: the projection folds two session ids that
    carry the same worktree id into one orchestrating chat, and
    ``related_session_ids`` treats them as one session family for claim
    authority. So the value has to name something that belongs to ONE chat.

    Two things follow, and both are deliberate:

    * It is NEVER derived from the working directory. Every chat opened in a
      repository shares a cwd, so a cwd-derived id would fold an entire lane
      into a single group and let one chat inherit another's claim family.
    * In the PRIMARY worktree it stays ``None``. Git's identity for the primary
      worktree is the repository itself, which is shared exactly the way cwd is.
      A linked worktree is different in kind: the harness creates one per
      isolated agent, so it does name a single chat. A missing bridge only
      costs a session its alias; a wrong one merges two strangers' work.
    """
    dirs = _git_worktree_dirs(cwd)
    if dirs is None:
        return None
    git_dir, common_dir = dirs
    git_dir = os.path.realpath(git_dir)
    common_dir = os.path.realpath(os.path.join(cwd, common_dir))
    if git_dir == common_dir:
        return None  # primary worktree: shared by every chat in the checkout
    parent = Path(git_dir).parent
    if parent.name != "worktrees" or os.path.realpath(parent.parent) != common_dir:
        return None  # not a shape git produced for a linked worktree
    name = _SAFE_SESSION_COMPONENT_RE.sub("-", Path(git_dir).name).strip("-")
    digest = hashlib.sha256(git_dir.encode("utf-8")).hexdigest()[:12]
    return f"worktree:{name}:{digest}" if name else f"worktree:{digest}"


def _resolve_worktree_id(env: Mapping[str, str]) -> str | None:
    """The worktree bridge for a Claude session: explicit env, else git, else None."""
    explicit = str(env.get("COORD_WORKTREE_ID") or "").strip()
    if explicit:
        return explicit
    try:
        return _linked_worktree_id(os.getcwd())
    except OSError:  # pragma: no cover - cwd deleted underneath the process
        return None


def resolve_identity(env: dict | None = None) -> dict:
    env = dict(os.environ if env is None else env)
    explicit_actor = str(env.get("COORD_ACTOR") or "").strip()
    actor = harness_config.actor_name(explicit_actor) if explicit_actor else None
    explicit_sid = str(env.get("COORD_SESSION_ID") or "").strip()
    raw_sid = str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    # Refuse on ambiguity rather than guessing better. Both vendors' session
    # variables are present whenever one agent's process is spawned from the
    # other's shell, and the review guard derives the reviewing lane by
    # INVERTING this actor -- so a wrong guess here lets a lane clear its own
    # work while every log line still reads correct. COORD_ACTOR settles it.
    if actor is None and raw_sid and _has_codex_identity(env):
        raise ValueError(
            "ambiguous agent identity: both CLAUDE_CODE_SESSION_ID and a Codex "
            "session variable are set. Set COORD_ACTOR=claude or COORD_ACTOR=codex "
            "so review attribution cannot be wrong."
        )
    if actor == "claude" or (actor is None and raw_sid):
        actor = "claude"
        sid = normalize_session_id(actor, explicit_sid or raw_sid or f"pid:{os.getpid()}")
        raw_parent_sid = str(env.get("COORD_PARENT_SESSION_ID") or "").strip()
        parent_sid = normalize_session_id(actor, raw_parent_sid) if raw_parent_sid else None
        label = env.get("COORD_SESSION_LABEL") or env.get("CLAUDE_SESSION_LABEL")
        title = env.get("COORD_CONVERSATION_TITLE") or env.get("CLAUDE_CONVERSATION_TITLE")
        return {
            "actor": actor,
            "session_id": sid,
            "parent_session_id": parent_sid,
            "runner_type": "subagent" if parent_sid else "claude_chat",
            "human_label": label or _fallback_human_label("claude", sid),
            "conversation_title": title,
            "external_thread_id": raw_sid or explicit_sid,
            # The second bridge key. Claude registers the same chat twice --
            # a hook under the raw host id, a later claim under a semantic one
            # -- and only these two columns can join them back together.
            "worktree_id": _resolve_worktree_id(env),
            "label_source": "env" if (label or title) else "inferred",
        }
    if actor is None:
        actor = "codex" if any(
            str(env.get(key) or "").strip()
            for key in (
                "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_WORKTREE_ID",
                "CODEX_CONVERSATION_ID", "STARSHIP_SESSION_KEY",
            )
        ) else "local"
    if actor == "codex":
        csid = normalize_session_id(actor, explicit_sid or _codex_session_id(env))
    else:
        csid = normalize_session_id(actor, explicit_sid or f"pid:{os.getpid()}")
    label = env.get("COORD_SESSION_LABEL") or env.get("CODEX_SESSION_LABEL")
    title = env.get("COORD_CONVERSATION_TITLE") or env.get("CODEX_CONVERSATION_TITLE")
    thread_id = env.get("CODEX_THREAD_ID") or env.get("CODEX_CONVERSATION_ID")
    worktree_id = env.get("CODEX_WORKTREE_ID")
    return {
        "actor": actor,
        "session_id": csid,
        "parent_session_id": None,
        "runner_type": actor if actor in (_lane_set() | {"local"}) else "agent",
        "human_label": label or _fallback_human_label(actor, csid),
        "external_thread_id": thread_id,
        "conversation_title": title,
        "worktree_id": worktree_id,
        "label_source": "env" if (label or title or thread_id or worktree_id) else "inferred",
    }


def _intent(status: Any) -> str:
    return _STATUS_TO_INTENT.get(str(status or "").lower(), "queued")


def _grouping_config(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    inline = str(source.get("COORD_GROUPING_RULES_JSON") or "").strip()
    file_value = str(source.get("COORD_GROUPING_RULES_FILE") or "").strip()
    if inline and file_value:
        raise ValueError("configure only one of COORD_GROUPING_RULES_JSON or FILE")
    if not inline and not file_value:
        return {"domains": (), "modules": {}}
    if file_value:
        root = harness_config.project_root().resolve()
        path = Path(file_value).expanduser()
        candidate = path if path.is_absolute() else root / path
        resolved = candidate.resolve(strict=False)
        allowed = (root, harness_config.state_dir().resolve(strict=False))
        if not any(resolved == base or base in resolved.parents for base in allowed):
            raise ValueError("grouping configuration escapes project and state roots")
        inline = resolved.read_text(encoding="utf-8")
    try:
        parsed = json.loads(inline)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid grouping JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("grouping configuration must be a JSON object")
    raw_domains = parsed.get("domains") or []
    raw_modules = parsed.get("modules") or {}
    if not isinstance(raw_domains, list) or not isinstance(raw_modules, dict):
        raise ValueError("grouping domains must be a list and modules must be an object")
    domains: list[str] = []
    for raw in raw_domains:
        value = str(raw).strip()
        if _SAFE_GROUPING_ID_RE.fullmatch(value) is None:
            raise ValueError(f"invalid grouping domain {raw!r}")
        domains.append(value)
    modules: dict[str, dict[str, str | None]] = {}
    for raw_name, raw_rule in raw_modules.items():
        name = str(raw_name).strip()
        if _SAFE_GROUPING_ID_RE.fullmatch(name) is None or not isinstance(raw_rule, dict):
            raise ValueError(f"invalid grouping module {raw_name!r}")
        domain = str(raw_rule.get("domain") or "").strip() or None
        sublane = str(raw_rule.get("sublane") or "").strip() or None
        for label, value in (("domain", domain), ("sublane", sublane)):
            if value is not None and _SAFE_GROUPING_ID_RE.fullmatch(value) is None:
                raise ValueError(f"invalid {label} for grouping module {name!r}")
        if domain is not None and domains and domain not in domains:
            raise ValueError(f"grouping module {name!r} references undeclared domain {domain!r}")
        modules[name] = {"domain": domain, "sublane": sublane}
    return {"domains": tuple(dict.fromkeys(domains)), "modules": modules}


def _resolve_grouping(row_id: str, row: dict) -> tuple[str | None, str | None, str | None]:
    del row_id
    explicit_module = str(row.get("module") or "").strip() or None
    explicit_sublane = str(row.get("sublane") or row.get("sub") or "").strip() or None
    config = _grouping_config()
    rule = config["modules"].get(explicit_module, {}) if explicit_module else {}
    domain = str(row.get("domain") or rule.get("domain") or "").strip() or None
    sublane = explicit_sublane or (str(rule.get("sublane") or "").strip() or None)
    return explicit_module, domain, sublane


def _valid_domain(value: Any) -> bool:
    text = str(value or "").strip()
    if _SAFE_GROUPING_ID_RE.fullmatch(text) is None:
        return False
    domains = _grouping_config()["domains"]
    return text in domains if domains else True


def _wid(row: dict) -> str | None:
    return row.get("roadmap_id") or row.get("id") or row.get("name")


def _map_work_fields(row: dict, default_intent: str | None = None) -> dict:
    f: dict[str, Any] = {}
    if row.get("title") or row.get("name"):
        f["title"] = row.get("title") or row.get("name")
    for src, dst in (("module", "module"), ("lane", "lane"),
                     ("sublane", "sublane"), ("sub", "sublane"), ("surface", "surface"),
                     ("parent", "parent_id"), ("assignee", "assignee"),
                     ("assigned_by", "assigned_by"), ("done_signal", "done_signal"),
                     ("display", "display"), ("note", "note"),
                     ("context_pack_ref", "context_pack_ref"),
                     ("rubric_verdict", "rubric_verdict"),
                     ("resource_class", "resource_class"),
                     ("visibility", "visibility")):
        if row.get(src) is not None:
            f[dst] = row.get(src)
    if row.get("acceptance_json") not in (None, ""):
        value = row.get("acceptance_json")
        f["acceptance_json"] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
    elif row.get("acceptance") not in (None, ""):
        f["acceptance_json"] = json.dumps({"acceptance": row.get("acceptance")}, ensure_ascii=True)
    rid = _wid(row) or ""
    module, domain, sublane = _resolve_grouping(rid, row)
    if module and not f.get("module"):
        f["module"] = module
    if sublane and not f.get("sublane"):
        f["sublane"] = sublane
    if domain and not _valid_domain(f.get("domain")):
        f["domain"] = domain
    f["intent_state"] = default_intent or _intent(row.get("status"))
    return f


def bootstrap_from_backlog(conn, backlog_path: str | Path) -> dict:
    d = json.loads(Path(backlog_path).read_text())
    n_epics = n_work = 0
    for e in d.get("epics", []):
        eid = e.get("id")
        if not eid:
            continue
        coord_db.upsert_work(conn, eid, title=e.get("title") or e.get("name") or eid,
                             surface="epic", domain=eid)
        n_epics += 1
    pending = []
    for key, default_intent in (("items", None), ("today_live_jobs", None),
                                ("done_archive", "done")):
        for row in d.get(key, []):
            if not isinstance(row, dict):
                continue
            wid = _wid(row)
            if not wid:
                continue
            fields = _map_work_fields(row, default_intent)
            if fields.get("intent_state") in {"blocked", "done", "failed"} and _has_live_pid_backed_local_run(conn, wid):
                fields["intent_state"] = "running"
            fields = _strip_intent_if_demoted_for_backlog(conn, wid, fields)
            pid = fields.get("parent_id")
            if pid and not _row_exists(conn, pid):
                pending.append((wid, pid))
                fields.pop("parent_id")
            coord_db.upsert_projection_work(
                conn,
                wid,
                seed_done_signal_artifact=True,
                **fields,
            )
            n_work += 1
    relinked = 0
    for child, parent in pending:
        if _row_exists(conn, parent) and _row_exists(conn, child):
            cur = conn.execute("SELECT parent_id FROM work_items WHERE work_id=?", (child,)).fetchone()
            if cur and cur["parent_id"] is None:
                coord_db.upsert_work(conn, child, parent_id=parent)
                relinked += 1
    return {"epics": n_epics, "work_rows": n_work, "relinked": relinked}


def _row_exists(conn, wid: str) -> bool:
    return conn.execute("SELECT 1 FROM work_items WHERE work_id=?", (wid,)).fetchone() is not None


def _has_active_claim(conn, wid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM claims WHERE work_id=? AND status IN"
        " ('running','paused','blocked') LIMIT 1",
        (wid,),
    ).fetchone()
    return row is not None


def _has_lifecycle_run(conn, wid: str) -> bool:
    states = ("live", *sorted(coord_db.TERMINAL_RUN_STATES))
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"SELECT 1 FROM runs WHERE work_id=? AND COALESCE(state,'') IN ({placeholders}) LIMIT 1",
        (wid, *states),
    ).fetchone() is not None


def _has_backlog_replay_lifecycle_authority(conn, wid: str) -> bool:
    if conn.execute("SELECT 1 FROM claims WHERE work_id=? LIMIT 1", (wid,)).fetchone() is not None:
        return True
    if _has_lifecycle_run(conn, wid):
        return True
    if conn.execute("SELECT 1 FROM artifacts WHERE work_id=? LIMIT 1", (wid,)).fetchone() is not None:
        return True
    placeholders = ",".join("?" for _ in _BACKLOG_DEMOTION_EVENT_KINDS)
    row = conn.execute(
        f"SELECT 1 FROM events WHERE work_id=? AND kind IN ({placeholders}) LIMIT 1",
        (wid, *_BACKLOG_DEMOTION_EVENT_KINDS),
    ).fetchone()
    return row is not None


def _demote_intent_write_for_backlog(conn, wid: str) -> bool:
    return (
        _row_exists(conn, wid)
        and _has_backlog_replay_lifecycle_authority(conn, wid)
    )


def _strip_intent_if_demoted_for_backlog(conn, wid: str, fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    if "intent_state" in out and _demote_intent_write_for_backlog(conn, wid):
        out.pop("intent_state", None)
    return out


def _has_live_pid_backed_local_run(conn, wid: str) -> bool:
    rows = conn.execute(
        "SELECT pid, pid_started_at, runner_kind FROM runs"
        " WHERE work_id=? AND state='live' AND pid IS NOT NULL",
        (wid,),
    ).fetchall()
    for row in rows:
        runner = str(row["runner_kind"] or "").lower()
        if not (runner.startswith("local") or "job_progress" in runner or "sidecar" in runner):
            continue
        if pid_matches(row["pid"], row["pid_started_at"]):
            return True
    return False


def _parent_edge_is_cycle_safe(conn, candidate_parent: str, work_id: str) -> bool:
    try:
        coord_db._walk_parent_chain_for_cycle_unlocked(conn, candidate_parent, work_id=work_id)
    except coord_db.ParentCycleError:
        return False
    return True


def backfill_grouping(conn, backlog_path: str | Path) -> dict:
    d = json.loads(Path(backlog_path).read_text())

    backlog: dict[str, dict] = {}
    for section in ("items", "today_live_jobs", "done_archive"):
        for row in d.get(section, []):
            if not isinstance(row, dict):
                continue
            wid = _wid(row)
            if wid and wid not in backlog:
                backlog[wid] = row

    for e in d.get("epics", []):
        eid = e.get("id")
        if eid and eid not in backlog:
            backlog[eid] = e

    existing: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT work_id, module, domain, sublane, parent_id, surface FROM work_items").fetchall():
        existing[r["work_id"]] = dict(r)

    epic_ids: set[str] = {wid for wid, row in existing.items() if row.get("surface") == "epic"}

    scanned = module_filled = domain_filled = sublane_filled = parent_filled = 0

    for work_id, cur in existing.items():
        bl = backlog.get(work_id)
        if bl is None:
            continue
        scanned += 1

        updates: dict[str, object] = {}

        resolved_module, resolved_domain, resolved_sublane = _resolve_grouping(work_id, bl)

        if cur["module"] is None:
            if resolved_module:
                updates["module"] = resolved_module
                module_filled += 1

        if cur["domain"] is None or not _valid_domain(cur["domain"]):
            domain_module = updates.get("module") or cur.get("module") or resolved_module
            if domain_module and resolved_domain:
                updates["domain"] = resolved_domain
                domain_filled += 1

        if cur["sublane"] is None:
            if resolved_sublane:
                updates["sublane"] = resolved_sublane
                sublane_filled += 1

        if cur["parent_id"] is None and cur.get("surface") != "epic":
            new_parent: str | None = None

            bl_parent = bl.get("parent")
            if bl_parent and bl_parent != work_id and bl_parent in existing:
                if _parent_edge_is_cycle_safe(conn, bl_parent, work_id):
                    new_parent = bl_parent
                else:
                    _logger.warning(
                        "backfill_grouping: skipping explicit parent=%s for work_id=%s"
                        " — would close a parent cycle",
                        bl_parent,
                        work_id,
                    )

            if new_parent is None:
                bl_epic = bl.get("epic")
                if bl_epic and bl_epic != work_id and bl_epic in epic_ids:
                    if _parent_edge_is_cycle_safe(conn, bl_epic, work_id):
                        new_parent = bl_epic
                    else:
                        _logger.warning(
                            "backfill_grouping: skipping epic parent=%s for work_id=%s"
                            " — would close a parent cycle",
                            bl_epic,
                            work_id,
                        )

            if new_parent is not None:
                updates["parent_id"] = new_parent
                parent_filled += 1

        if updates:
            set_clauses = ", ".join(
                f"{col}=?" if col == "domain" else f"{col}=COALESCE({col}, ?)"
                for col in updates
            )
            with coord_db.tx(conn):
                t = coord_db.db_now(conn)
                conn.execute(
                    f"UPDATE work_items SET {set_clauses}, updated_at=?, version=version+1"
                    f" WHERE work_id=?",
                    [*updates.values(), t, work_id],
                )

    return {
        "scanned": scanned,
        "module_filled": module_filled,
        "domain_filled": domain_filled,
        "sublane_filled": sublane_filled,
        "parent_filled": parent_filled,
    }


_OBSERVER_NAME_RE = re.compile(r"^\s*(startup|resume|compact|session|claude|codex)\b", re.I)


def _is_observer_noise(r: dict) -> bool:
    kind = str(r.get("kind") or "").strip().lower()
    name = str(r.get("name") or r.get("title") or "")
    return kind == "observer" or bool(_OBSERVER_NAME_RE.match(name))


def bootstrap_from_jobs_db(conn, jobs_db_path: str | Path) -> dict:
    from .config import assert_outside_warehouse
    assert_outside_warehouse(Path(jobs_db_path))
    src = sqlite3.connect(f"file:{Path(jobs_db_path)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    runs = events = 0
    try:
        for r in src.execute("SELECT * FROM job_registry"):
            r = dict(r)
            jid = r.get("job_id") or r.get("id")
            if not jid:
                continue
            ext_ref = str(r.get("ext_ref") or "").strip()
            wid = r.get("roadmap_id") or (ext_ref if ext_ref and ext_ref != jid else f"job:{jid}")
            if not r.get("roadmap_id") and _is_observer_noise(r):
                continue
            desired = str(r.get("desired_state") or "").lower()
            terminal = desired in ("stopped", "done", "completed", "failed") or bool(r.get("completed_at"))
            if not _row_exists(conn, wid):
                coord_db.upsert_work(conn, wid, title=(r.get("title") or r.get("name") or wid),
                                     resource_class=r.get("kind"), lane=r.get("kind"),
                                     intent_state="archived" if terminal else _intent(r.get("desired_state")),
                                     note=(
                                         f"legacy jobs.db import: job_id={jid}; "
                                         f"name={r.get('name') or ''}; desired_state={r.get('desired_state') or ''}"
                                     ))
            rid = f"run:jobsdb:{jid}"
            pid = r.get("pid") if isinstance(r.get("pid"), int) else None
            if terminal or pid is not None:
                coord_db.appear_run(conn, work_id=wid, runner_kind=(r.get("kind") or "local"),
                                    resource_class=r.get("kind"),
                                    pid=pid,
                                    run_id=rid)
            if terminal:
                coord_db.finalize_run(conn, rid, state="done")
                runs += 1
            elif pid is not None:
                runs += 1
        try:
            for e in src.execute("SELECT rowid, * FROM job_events"):
                e = dict(e)
                ev_id = e.get("id") or e.get("rowid")
                if ev_id is None:
                    ev_id = hashlib.sha1(
                        json.dumps({k: str(e[k]) for k in sorted(e)[:6]}, sort_keys=True).encode()
                    ).hexdigest()[:16]
                coord_db.post_event(conn, kind=str(e.get("event") or e.get("kind") or "job_event"),
                                    work_id=e.get("roadmap_id"),
                                    body=json.dumps({k: str(e[k]) for k in list(e)[:6]})[:2000],
                                    idempotency_key=f"jobsdb:event:{ev_id}")
                events += 1
        except sqlite3.OperationalError:
            pass
    finally:
        src.close()
    return {"runs": runs, "events": events}


JOBS_DB_TABLES = ("job_registry", "job_events", "job_summaries", "source_cursors",
                  "ingested_records", "action_requests", "current_jobs")


def jobsdb_source_counts(jobs_db_path: str | Path) -> dict:
    src = sqlite3.connect(f"file:{Path(jobs_db_path)}?mode=ro", uri=True)
    try:
        out = {}
        for t in JOBS_DB_TABLES:
            try:
                out[t] = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
        return out
    finally:
        src.close()


def bootstrap_all(conn, *, backlog_path, jobs_db_path) -> dict:
    rep = {}
    rep["backlog"] = bootstrap_from_backlog(conn, backlog_path)
    if Path(jobs_db_path).exists():
        rep["jobs_db"] = bootstrap_from_jobs_db(conn, jobs_db_path)
        rep["jobs_db_source_tables"] = jobsdb_source_counts(jobs_db_path)
    rep["totals"] = {
        "work_items": conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
        "agent_sessions": conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0],
        "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }
    return rep
