from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import mimetypes
import os
from pathlib import PurePosixPath
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from coordharness import config
from coordharness.board.action_registry import build_action_registry_response
from coordharness.board.operations import build_operations
from coordharness.board.semantic_query import (
    QueryContractError,
    build_semantic_query_response,
    query_error_document,
)
from coordharness.board.security import (
    ALLOWED_HOSTS,
    SECURITY_HEADERS,
    host_allowed,
    is_loopback_bind,
    origin_allowed,
)
from coordharness.board.snapshot import (
    _ATTENTION,
    _DONE,
    _RUNNING,
    build_context,
    build_graph,
    build_pulse,
    build_snapshot,
    build_timeline,
    load_schema,
    stable_copy,
)
from coordharness.usage.account_actions import UsageAccountActionForwarder
from coordharness.usage.dashboard_proxy import UsageDashboardProxy
from coordharness.board.system_telemetry_proxy import SystemTelemetryProxy
from coordharness.usage.provider_management import ProviderManagementForwarder

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7870
_PROVIDER_ACTION_SHAPES = {
    "provider_add": {"action", "provider"}, "provider_remove": {"action", "provider_id"},
    "provider_configure": {"action", "provider_id", "enabled", "priority"},
    "account_add": {"action", "provider_id", "label", "auth_mode", "endpoint"},
    "account_select": {"action", "provider_id", "profile_id"},
    "account_remove": {"action", "provider_id", "profile_id"},
    "account_configure": {"action", "provider_id", "profile_id", "enabled", "priority", "endpoint"},
    "credential_set": {"action", "provider_id", "profile_id", "credential"},
    "credential_clear": {"action", "provider_id", "profile_id"},
    "routing_policy_update": {"action", "policy"},
}
_STATIC = files("coordharness.board").joinpath("static")
_STATIC_ALLOWLIST = {
    "index.html",
    "app.css",
    "app.js",
    "comms-board.css",
    "usage-dashboard.css",
    "usage-dashboard.js",
    "cockpit.html",
    "cockpit.css",
    "map-readability.css",
    "cockpit.js",
    "accent.js",
    "ops-atlas.html",
    "ops-atlas.css",
    "ops-atlas-model.js",
    "ops-atlas.js",
    "swarm-mesh.html",
    "swarm-mesh.css",
    "swarm-mesh-model.js",
    "swarm-mesh.js",
    "search.js",
    "search.css",
    "neighbourhood.js",
    "timeline.js",
    "timeline.css",
    "view-flow.js",
    "view-density.js",
    "view-flow.css",
    "view-density.css",
    "view-chronicle.js",
    "view-chronicle.css",
    "view-subjects.js",
    "view-subjects.css",
    "view-orbit.js",
    "view-orbit.css",
    # Owner marks. Named individually rather than by glob: the handler
    # serves exactly what is listed here and nothing a directory happens
    # to acquire later.
    "mark-claude.png",
    "mark-codex.png",
    "mark-accelerator.png",
    "mark-compute.png",
    "wordmark.png",
    "motion.js",
    "motion.css",
    "shell.js",
    "view-pulse.js",
    "view-pulse.css",
    "view-flowpath.js",
    "view-flowpath.css",
    "view-ceiling.js",
    "view-ceiling.css",
    "view-topology.js",
    "view-topology.css",
    "shell.css",
}


def _utc_now() -> str:
    fixed_epoch = config.source_date_epoch()
    moment = (
        datetime.fromtimestamp(fixed_epoch, tz=timezone.utc)
        if fixed_epoch is not None
        else datetime.now(timezone.utc)
    )
    return moment.isoformat().replace("+00:00", "Z")


def _reject_json_constant(_value: str) -> None:
    raise ValueError("nonstandard JSON constant")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def build_documents(db_path: str | None) -> tuple[dict[str, Any], ...]:
    """The six served documents, all read from one copy of the database.

    Swapping them under one lock is necessary and not sufficient. Each source
    builder materializes its own private copy, so five source calls would read
    the database at five separate instants. A row written between the snapshot
    build and the timeline build lands in the timeline and not in the row list.
    The lock publishes that disagreement atomically rather than preventing it. One
    `stable_copy` around all five source builders is what makes the set agree:
    the builders then see identical bytes, whatever the live database does. The
    pulse document counts the same events the timeline carries, so a reader who
    straddled two copies would see a count that disagrees with the list beside
    it. Operations is then derived from those already-coherent public documents,
    without opening SQLite independently.
    """
    source = db_path if db_path is not None else config.coord_db_path()
    # The telemetry root is resolved from the database the operator named, not
    # from the private copy: the copy lives in a temporary directory that has
    # no sidecars at all, and resolving there would empty the Jobs surface on
    # the default path. Resolving it from the ambient state tree instead --
    # which is what this did -- assembled one screen out of two unrelated
    # directories, so a database served from elsewhere showed job rows
    # belonging to whatever tree the process was standing in.
    sidecars = config.job_progress_dir_for_database(source)
    with stable_copy(source) as frozen:
        path = str(frozen)
        snapshot = build_snapshot(path, job_progress_dir=sidecars)
        graph = build_graph(path, job_progress_dir=sidecars)
        context = build_context(path)
        timeline = build_timeline(path)
        pulse = build_pulse(path)
        return (
            snapshot,
            graph,
            context,
            timeline,
            pulse,
            build_operations(snapshot, graph, context, timeline),
        )


def _project_to_envelope(
    snapshot: dict[str, Any] | None,
    graph: dict[str, Any] | None,
    context: dict[str, Any] | None,
    timeline: dict[str, Any] | None,
    operations: dict[str, Any] | None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any],
]:
    """Trim the bundle to the population its graph envelope actually emits.

    The envelope bounds what a reader draws and publishes its own omission
    receipt; the bundle used to ship the whole board anyway. On a real board of
    8,635 rows that was a 24MB response, and the board server is single-worker
    -- six seconds spent serialising it is six seconds not serving the page
    that asked for it, so the surface never finished loading.

    Rows and nodes outside the envelope are dropped; the counts that describe
    the whole board are not. `snapshot.summary` keeps every total, the envelope
    keeps population/emitted/omitted, and the projection receipt below names
    exactly what was trimmed. A reader can still state what the board holds --
    it just no longer receives every row to say it.
    """
    envelope = (operations or {}).get("graph_envelope") or {}
    nodes = envelope.get("nodes")
    unprojected = (snapshot, graph, context, timeline)
    if not isinstance(nodes, list) or not nodes:
        return (
            *unprojected,
            {
                "applied": False,
                "reason": "no graph envelope published; the bundle is unprojected",
            },
        )
    keep: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        raw = str(node["id"])
        keep.add(raw)
        if ":" in raw:
            keep.add(raw.split(":", 1)[1])
    if not keep:
        return (
            *unprojected,
            {
                "applied": False,
                "reason": "graph envelope published no node ids; the bundle is unprojected",
            },
        )

    def trim_list(
        document: dict[str, Any] | None, field: str
    ) -> tuple[dict[str, Any] | None, int, int]:
        if not isinstance(document, dict):
            return document, 0, 0
        items = document.get(field)
        if not isinstance(items, list):
            return document, 0, 0
        kept = [i for i in items if isinstance(i, dict) and str(i.get("id")) in keep]
        return {**document, field: kept}, len(items), len(items) - len(kept)

    trimmed_snapshot, snap_total, snap_dropped = trim_list(snapshot, "rows")
    trimmed_context, ctx_total, ctx_dropped = trim_list(context, "items")
    trimmed_timeline, tl_total, tl_dropped = trim_list(timeline, "items")

    graph_total_nodes = graph_dropped_nodes = graph_total_edges = graph_dropped_edges = 0
    trimmed_graph = graph
    if isinstance(graph, dict):
        g_nodes = graph.get("nodes")
        g_edges = graph.get("edges")
        new_graph = dict(graph)
        if isinstance(g_nodes, list):
            kept_nodes = [n for n in g_nodes if isinstance(n, dict) and str(n.get("id")) in keep]
            graph_total_nodes, graph_dropped_nodes = len(g_nodes), len(g_nodes) - len(kept_nodes)
            new_graph["nodes"] = kept_nodes
        if isinstance(g_edges, list):
            # An edge survives only when BOTH ends do, which is the same rule
            # the envelope applies; a half-drawn edge would point at nothing.
            kept_edges = [
                e
                for e in g_edges
                if isinstance(e, dict)
                and str(e.get("source")) in keep
                and str(e.get("target")) in keep
            ]
            graph_total_edges, graph_dropped_edges = len(g_edges), len(g_edges) - len(kept_edges)
            new_graph["edges"] = kept_edges
        trimmed_graph = new_graph

    # A projection that keeps nothing is an id-vocabulary mismatch, not an
    # empty board. Publishing it would hand the reader an empty set that looks
    # like a real answer, so the unprojected bundle goes out and the mismatch
    # is named. This fired on the first attempt: envelope ids carry a type
    # prefix the per-row documents do not use.
    kept_any = (
        (snap_total - snap_dropped)
        or (ctx_total - ctx_dropped)
        or (graph_total_nodes - graph_dropped_nodes)
    )
    if (snap_total or ctx_total or graph_total_nodes) and not kept_any:
        return (
            *unprojected,
            {
                "applied": False,
                "reason": (
                    "projection matched no row: the envelope's ids and the bundle's "
                    "documents do not share a vocabulary, so nothing was trimmed"
                ),
                "envelope_nodes": len(keep),
            },
        )
    return (
        trimmed_snapshot,
        trimmed_graph,
        trimmed_context,
        trimmed_timeline,
        {
            "applied": True,
            "rule": "documents are limited to the ids the graph envelope emits; totals are preserved in snapshot.summary and the envelope's own counts",
            "envelope_nodes": len(keep),
            "snapshot_rows": {"published": snap_total - snap_dropped, "omitted": snap_dropped},
            "graph_nodes": {
                "published": graph_total_nodes - graph_dropped_nodes,
                "omitted": graph_dropped_nodes,
            },
            "graph_edges": {
                "published": graph_total_edges - graph_dropped_edges,
                "omitted": graph_dropped_edges,
            },
            "context": {"published": ctx_total - ctx_dropped, "omitted": ctx_dropped},
            "timeline": {"published": tl_total - tl_dropped, "omitted": tl_dropped},
        },
    )


def _request_parameters(raw_query: str, allowed: set[str]) -> dict[str, str]:
    """Parse a tiny GET-only contract without accepting ambiguous values."""
    try:
        parsed = parse_qs(
            raw_query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=max(8, len(allowed) * 2),
        )
    except ValueError as exc:
        raise QueryContractError(
            "invalid_parameters",
            "request query parameters exceed the endpoint contract",
            path="$.parameters",
        ) from exc
    unknown = sorted(set(parsed) - allowed)
    if unknown:
        raise QueryContractError(
            "unknown_parameter",
            "request contains an unsupported query parameter",
            path="$.parameters",
        )
    duplicate = sorted(key for key, values in parsed.items() if len(values) != 1)
    if duplicate:
        raise QueryContractError(
            "ambiguous_parameter",
            "a query parameter appears more than once",
            path="$.parameters",
        )
    return {key: values[0] for key, values in parsed.items()}


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Chromium opens the shell static assets in parallel. The stdlib
    # default backlog of five can reset valid loopback requests and leave
    # the dashboard stuck on Connecting before app.js has loaded.
    request_queue_size = 64

    def __init__(
        self,
        address,
        handler,
        *,
        allowed_hosts: set[str],
        db_path: str | None,
        refresh_interval: float,
        usage_dashboard_proxy: UsageDashboardProxy | None = None,
        system_telemetry_proxy: SystemTelemetryProxy | None = None,
        usage_account_forwarder: UsageAccountActionForwarder | None = None,
        provider_management_forwarder: ProviderManagementForwarder | None = None,
    ):
        super().__init__(address, handler)
        self.allowed_hosts = allowed_hosts
        self.db_path = db_path
        self.refresh_interval = max(0.0, float(refresh_interval))
        # Independent read-through transport only: provider usage never enters
        # coord.db or the lifecycle snapshot/cache generation below.
        self._usage_dashboard_proxy = usage_dashboard_proxy or UsageDashboardProxy()
        self._system_telemetry_proxy = system_telemetry_proxy or SystemTelemetryProxy()
        self._usage_account_forwarder = usage_account_forwarder or UsageAccountActionForwarder(
            dashboard_url=getattr(self._usage_dashboard_proxy, "url", None) or ""
        )
        self._provider_management_forwarder = provider_management_forwarder or ProviderManagementForwarder(
            dashboard_url=getattr(self._usage_dashboard_proxy, "url", None) or ""
        )
        self._snapshot_lock = threading.Lock()
        (
            self._snapshot,
            self._graph,
            self._context,
            self._timeline,
            self._pulse,
            self._operations,
        ) = build_documents(db_path)
        started_at = _utc_now()
        self._last_refresh_attempt = started_at
        self._last_successful_refresh = started_at
        self._last_refresh_failure_class = ""
        self._consecutive_refresh_failures = 0
        self._cache_generation = 1
        self._next_refresh = time.monotonic() + self.refresh_interval
        self._refresh_run_lock = threading.Lock()
        self._refresh_stop = threading.Event()
        self._serve_forever_ident: int | None = None
        self._refresh_thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._snapshot

    def graph(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._graph

    def context(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._context

    def timeline(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._timeline

    def pulse(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._pulse

    def operations(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._operations

    def semantic_query(
        self,
        *,
        encoded_query: str | None = None,
        encoded_display: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate one semantic filter against one atomic cache generation."""
        with self._snapshot_lock:
            return build_semantic_query_response(
                self._snapshot,
                self._graph,
                self._context,
                self._operations,
                encoded_query=encoded_query,
                encoded_display=encoded_display,
                cache_generation=self._cache_generation,
            )

    def action_registry(self, target_id: str = "") -> dict[str, Any] | None:
        """Resolve row affordances without exposing a lifecycle writer."""
        with self._snapshot_lock:
            rows = [row for row in self._snapshot.get("rows", []) if isinstance(row, dict)]
            row_by_id = {str(row.get("id")): row for row in rows if row.get("id") is not None}
            context_by_id = {
                str(item.get("id")): item
                for item in self._context.get("items", [])
                if isinstance(item, dict) and item.get("id") is not None
            }
            if target_id and target_id not in row_by_id:
                return None
            row = row_by_id.get(target_id, {})
            structural = dict(context_by_id.get(target_id, {}))
            if row:
                dependencies = structural.get("depends_on")
                statuses = {
                    identity: str(item.get("status") or "").lower()
                    for identity, item in row_by_id.items()
                }
                structural["dependencies_satisfied"] = isinstance(dependencies, list) and all(
                    statuses.get(str(item), "") in _DONE for item in dependencies
                )
                target = {
                    "work_id": str(row.get("id") or ""),
                    "target_kind": (
                        "job" if str(row.get("id") or "").startswith("job:") else "work"
                    ),
                    "intent_state": str(row.get("status") or ""),
                    "assignee": str(row.get("owner") or ""),
                    "artifact_present": bool(structural.get("artifact_recorded")),
                }
            else:
                target = {}
            return build_action_registry_response(
                target,
                structural_context=structural,
                source_face="loopback_board",
                cache_generation=self._cache_generation,
                generated_at=str(self._snapshot.get("generated_at") or ""),
            )

    def system_telemetry(self, *, demand: bool = False) -> dict[str, Any]:
        """Return the canonical configured host snapshot through a loopback-only proxy."""

        return self._system_telemetry_proxy.get(demand=demand)

    def usage_dashboard(self) -> dict[str, Any]:
        """Return the configured usage document without local derivation."""

        return self._usage_dashboard_proxy.get()

    def usage_account_status(self) -> tuple[int, dict[str, Any]]:
        """Read the configured provider login status."""

        return self._usage_account_forwarder.status()

    def usage_account_action(self, action: str | dict[str, str]) -> tuple[int, dict[str, Any]]:
        """Forward one fixed user-triggered provider action to the configured provider service."""

        return self._usage_account_forwarder.forward(action)

    def provider_management_status(self) -> tuple[int, dict[str, Any]]:
        return self._provider_management_forwarder.status()

    def provider_management_action(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._provider_management_forwarder.forward(document)

    def _read_status_locked(self, *, generated_at: str | None = None) -> dict[str, Any]:
        """Return the cache receipt while ``_snapshot_lock`` is held."""
        return {
            "schema_version": "ReadStatusV1",
            "generated_at": generated_at or _utc_now(),
            "source": "board-cache",
            "read_only": True,
            "degraded": self._consecutive_refresh_failures > 0,
            "cache_generation": self._cache_generation,
            "source_generated_at": self._snapshot.get("generated_at", ""),
            "last_refresh_attempt": self._last_refresh_attempt,
            "last_successful_refresh": self._last_successful_refresh,
            "consecutive_refresh_failures": self._consecutive_refresh_failures,
            "last_failure_class": self._last_refresh_failure_class,
            "refresh_interval_seconds": self.refresh_interval,
        }

    def read_status(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._read_status_locked()

    def operations_bundle(self) -> dict[str, Any]:
        """Return every Atlas input and its receipt from one cache generation.

        Individual endpoints remain useful for small readers. The Atlas needs
        five document inputs, however, and independent requests can straddle a
        refresh even though each document is internally valid. Capturing the
        references and the cache receipt under one lock closes that boundary.
        Published documents are replaced, never mutated, so JSON encoding may
        safely continue after the lock is released.
        """
        with self._snapshot_lock:
            generated_at = _utc_now()
            read_status = self._read_status_locked(generated_at=generated_at)
            (
                snapshot,
                graph,
                context,
                timeline,
                projection,
            ) = _project_to_envelope(
                self._snapshot, self._graph, self._context, self._timeline, self._operations
            )
            return {
                "schema_version": "OpsAtlasBundleV1",
                "generated_at": generated_at,
                "cache_generation": self._cache_generation,
                "snapshot": snapshot,
                "graph": graph,
                "context": context,
                "timeline": timeline,
                "operations": self._operations,
                "read_status": read_status,
                "bundle_projection": projection,
            }

    def operations_bundle_v2(self) -> dict[str, Any]:
        """Return topology and communication traffic from one cache generation.

        V1 remains byte-shape compatible for existing clients. V2 adds the
        prose-free Pulse document so Board, Mesh, Map, and Atlas never imply
        that independently refreshed traffic belongs to a different topology
        generation.
        """
        with self._snapshot_lock:
            generated_at = _utc_now()
            read_status = self._read_status_locked(generated_at=generated_at)
            snapshot, graph, context, timeline, projection = _project_to_envelope(
                self._snapshot, self._graph, self._context, self._timeline, self._operations
            )
            return {
                "schema_version": "OpsAtlasBundleV2",
                "generated_at": generated_at,
                "cache_generation": self._cache_generation,
                "snapshot": snapshot,
                "graph": graph,
                "context": context,
                "timeline": timeline,
                "pulse": self._pulse,
                "operations": self._operations,
                "read_status": read_status,
                "bundle_projection": projection,
            }

    def _refresh_once(self) -> None:
        if not self.refresh_interval or time.monotonic() < self._next_refresh:
            return
        if not self._refresh_run_lock.acquire(blocking=False):
            return
        try:
            self._refresh_once_locked()
        finally:
            self._refresh_run_lock.release()

    def _refresh_once_locked(self) -> None:
        self._next_refresh = time.monotonic() + self.refresh_interval
        attempted_at = _utc_now()
        build_started = time.monotonic()
        try:
            (
                refreshed,
                refreshed_graph,
                refreshed_context,
                refreshed_timeline,
                refreshed_pulse,
                refreshed_operations,
            ) = build_documents(self.db_path)
        except Exception as exc:
            with self._snapshot_lock:
                self._last_refresh_attempt = attempted_at
                self._last_refresh_failure_class = type(exc).__name__
                self._consecutive_refresh_failures += 1
                failures = self._consecutive_refresh_failures
            print(
                json.dumps(
                    {
                        "event": "board_refresh_failed",
                        "attempted_at": attempted_at,
                        "failure_class": type(exc).__name__,
                        "failure": str(exc),
                        "consecutive_failures": failures,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            return
        # All six documents are built from one frozen copy and swapped together.
        # Those are separate guarantees and both are needed. Built together, they
        # agree: no relationship points at a row that is not in the row list,
        # and no timeline carries history for a row the drawer cannot open.
        # Swapped together, a reader never straddles a refresh and sees half of
        # the old set beside half of the new one.
        # A fixed interval assumes the build fits inside it. On a demo of a few
        # dozen rows it does; on a real board of several thousand the rebuild
        # takes seconds, so a two-second schedule means the server is always
        # rebuilding, always holding the lock, and every page request queues
        # behind it -- the surface never finishes loading and the cause looks
        # like a network fault rather than a scheduling one.
        #
        # The next refresh is therefore paced off what this build actually
        # cost: never sooner than the configured interval, and never sooner
        # than four times the build, so the server spends at most a fifth of
        # its time refreshing however large the board becomes.
        build_seconds = time.monotonic() - build_started
        self._next_refresh = time.monotonic() + max(self.refresh_interval, build_seconds * 4)
        with self._snapshot_lock:
            recovered_failures = self._consecutive_refresh_failures
            self._snapshot = refreshed
            self._graph = refreshed_graph
            self._context = refreshed_context
            self._timeline = refreshed_timeline
            self._pulse = refreshed_pulse
            self._operations = refreshed_operations
            self._last_refresh_attempt = attempted_at
            self._last_successful_refresh = _utc_now()
            self._last_refresh_failure_class = ""
            self._consecutive_refresh_failures = 0
            self._cache_generation += 1
            generation = self._cache_generation
        if recovered_failures:
            print(
                json.dumps(
                    {
                        "event": "board_refresh_recovered",
                        "attempted_at": attempted_at,
                        "build_seconds": round(build_seconds, 3),
                        "previous_failures": recovered_failures,
                        "cache_generation": generation,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )

    def service_actions(self) -> None:
        """Refresh synchronously for explicit callers, never on the accept loop."""
        if self._serve_forever_ident == threading.get_ident():
            return
        self._refresh_once()

    def _refresh_loop(self) -> None:
        while not self._refresh_stop.is_set():
            remaining = max(0.0, self._next_refresh - time.monotonic())
            if self._refresh_stop.wait(min(0.5, remaining) if remaining else 0.0):
                return
            self._refresh_once()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Keep expensive materialization off ThreadingHTTPServer's accept loop.

        BaseServer invokes service_actions on the same thread that accepts new
        sockets. A large COORD graph takes many seconds to rebuild, so using
        that hook made a healthy listener time out every refresh cycle.
        """
        self._serve_forever_ident = threading.get_ident()
        self._refresh_stop.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name=f"coord-board-refresh-{self.server_port}",
            daemon=True,
        )
        self._refresh_thread.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._refresh_stop.set()
            refresh_thread = self._refresh_thread
            if refresh_thread is not None and refresh_thread is not threading.current_thread():
                refresh_thread.join(timeout=2.0)
            self._refresh_thread = None
            self._serve_forever_ident = None

    def shutdown(self) -> None:
        self._refresh_stop.set()
        super().shutdown()

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# The native client probes.
#
# The packaged Swift clients call three endpoints against this port -- their
# `HarnessEndpoint.base` defaults to loopback:7870, this server -- and until now
# all three answered 404. Each was decided separately, by reading its decoder in
# apps/, against one rule: serve it only where the decoder can express "the
# board does not publish this" as absence. A decoder that folds a missing field
# into a number turns silence into a quantity, and a read-only projection is
# mostly silence.
#
# /api/menubar -- SERVED, below. `MenubarState` in
#     apps/menubar/Sources/Data/Models.swift pins the shape, and every field of
#     it and of `WorkModel`, `Summary` and `Row` is Optional. The planes this
#     board does not carry -- governor, local lanes, sidecar errors, row actions
#     -- decode to nil, which is what they are.
#
# /api/state/compact -- NOT served; the probe should be removed or repointed at
#     a control plane that has the data. `CockpitHTTPFallbackSource.decode`
#     builds a `CockpitState`, whose `CockpitSummary` holds non-optional Ints
#     filled in with `?? 0` (CockpitHTTPFallbackSource.swift:177,
#     CockpitModels.swift:66). So there is no document -- not even `{}` -- that
#     leaves `done_today` unsaid: every one of them makes the cockpit render
#     "0 done today" over a board that has finished work and simply does not
#     count it per day. The row half is the same shape of problem: the compact
#     decoder draws `CockpitColumn.webDefaults`, control column included, and
#     fills every absent action with a default, so a read-only board would be
#     presented as a control surface whose controls all happen to be missing.
#     404 leaves the client on its own read model, which is the true answer.
#
# /api/capability_inventory -- NOT served; remove the probe. The inventory
#     describes the harness's capability planes: authority, token-cost policy,
#     resident processes, operator actions. This board publishes no capability
#     facts at all, so `capabilities: []` would assert the harness has none and
#     any row in it would be invented here rather than measured. Nothing else
#     depends on it: `CockpitCapabilityInventorySource.load` returns nil on any
#     non-2xx and the diagnostics panel omits the section
#     (CockpitDiagnosticsView.swift:80), so this 404 is already silent and
#     costs one request per window, not one every refresh.
# ---------------------------------------------------------------------------


def _menubar_row(row: dict[str, Any]) -> dict[str, Any]:
    """One snapshot row under the names `MenubarState.Row` decodes.

    A rename, not a translation: every value here is already public at
    /api/v1/snapshot. The snapshot's `group`, `bucket` and `priority` are left
    out rather than mapped onto the nearest-looking Swift field, because `lane`,
    `row_kind` and `priority` there name things this board does not measure, and
    a plausible value in the wrong field is worse than an empty one.
    """
    out: dict[str, Any] = {
        "id": row["id"],
        # `Row.title` is `display ?? name ?? "Task"` (Models.swift:414).
        "display": row["title"],
        "status": row["status"],
        "stale": row["stale"],
    }
    for key in ("owner", "module", "current_step"):
        value = row.get(key)
        if value:
            out[key] = value
    fraction = row.get("progress_fraction")
    if fraction is not None:
        # `Row.effectivePct` clamps to 0...100 (Models.swift:441), so the Swift
        # field is a percentage where the snapshot carries a fraction.
        out["pct"] = float(fraction) * 100.0
    eta = row.get("eta_seconds")
    if eta is not None:
        out["eta_s"] = float(eta)
    return out


def _menubar_document(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project the already-served snapshot into what the menubar decodes.

    Derived from the cached snapshot rather than from the database, so it is
    coherent with /api/v1/snapshot by construction and adds no read.

    The status sets are imported from the snapshot module, not restated here:
    they are the same sets that produce `summary`, so the bucket a row lands in
    can never disagree with the count beside it.
    """
    running: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for row in snapshot["rows"]:
        status = (str(row.get("status") or "").strip().lower()) or "planned"
        if status in _RUNNING:
            running.append(_menubar_row(row))
        elif status in _ATTENTION:
            attention.append(_menubar_row(row))
        elif status in _DONE:
            # `WorkModel` has no done bucket, so finished rows are carried only
            # as the `done` count. Putting them in `next_rows` to make them
            # visible would file completed work as work still to do.
            continue
        else:
            upcoming.append(_menubar_row(row))
    summary = snapshot["summary"]
    generated_at = str(snapshot["generated_at"]).replace("Z", "+00:00")
    return {
        "source": snapshot["source"],
        "stale": snapshot["stale"],
        # The same instant as `generated_at`, in the epoch seconds the Swift
        # field is typed for. `schema_version` is deliberately absent: this
        # document has no version of its own to declare, and inventing one sets
        # `schemaAhead` on a guess (HarnessClient.swift:415).
        "ts": datetime.fromisoformat(generated_at).timestamp(),
        "work_model": {
            # Exactly the five counts `_summary` produces; `Summary` also has
            # done_today, queued, blocked and the rest, all Optional, and all
            # left absent because the board does not measure them.
            "summary": {
                key: summary[key]
                for key in ("running", "attention", "next", "done", "total")
                if key in summary
            },
            "running_rows": running,
            "attention_rows": attention,
            "next_rows": upcoming,
        },
    }


class BoardHandler(BaseHTTPRequestHandler):
    server_version = "coordharness-board/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("COORD_BOARD_QUIET") != "1":
            super().log_message(fmt, *args)

    def _security_ok(self) -> bool:
        host = self.headers.get("Host")
        if not host_allowed(host, self.server.allowed_hosts):
            self._send_text(HTTPStatus.FORBIDDEN, "forbidden host")
            return False
        if not origin_allowed(self.headers.get("Origin"), host):
            self._send_text(HTTPStatus.FORBIDDEN, "forbidden origin")
            return False
        return True

    def _headers(self, status: int, content_type: str, length: int, *, security_headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in (security_headers or SECURITY_HEADERS).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, payload: bytes, content_type: str, *, security_headers=None) -> None:
        self._headers(status, content_type, len(payload), security_headers=security_headers)
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_text(self, status: int, message: str) -> None:
        self._send(status, message.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    def _serve_static(self, name: str, *, security_headers=None) -> None:
        # Allowlisted names may now carry one directory segment (the brand
        # font), so membership is the check and traversal is rejected
        # explicitly rather than by forbidding separators outright.
        if name not in _STATIC_ALLOWLIST or ".." in PurePosixPath(name).parts:
            self._send_text(HTTPStatus.NOT_FOUND, "not found")
            return
        resource = _STATIC.joinpath(name)
        try:
            raw = resource.read_bytes()
        except (FileNotFoundError, OSError):
            self._send_text(HTTPStatus.NOT_FOUND, "not found")
            return
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in {"application/javascript"}:
            kind += "; charset=utf-8"
        self._send(HTTPStatus.OK, raw, kind, security_headers=security_headers)

    def do_GET(self) -> None:
        if not self._security_ok():
            return
        request = urlsplit(self.path)
        path = request.path
        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "service": "coord-board", "read_only": True},
            )
            return
        if path == "/api/v1/snapshot":
            self._send_json(HTTPStatus.OK, self.server.snapshot())
            return
        if path == "/api/v1/graph":
            self._send_json(HTTPStatus.OK, self.server.graph())
            return
        if path == "/api/v1/context":
            self._send_json(HTTPStatus.OK, self.server.context())
            return
        if path == "/api/v1/timeline":
            self._send_json(HTTPStatus.OK, self.server.timeline())
            return
        # Structure the other four documents cannot carry: handoff direction (TimelineV1's
        # event tuple is sealed at three keys), the kind vocabulary an
        # unconstrained TEXT column actually holds, and the shape of the record
        # across UTC days. Read-only like every route here, and prose-free by
        # the same means: the columns that carry prose are never selected.
        if path == "/api/v1/pulse":
            self._send_json(HTTPStatus.OK, self.server.pulse())
            return
        if path == "/api/v1/operations":
            self._send_json(HTTPStatus.OK, self.server.operations())
            return
        if path == "/api/v1/operations-bundle":
            self._send_json(HTTPStatus.OK, self.server.operations_bundle())
            return
        if path == "/api/v2/operations-bundle":
            self._send_json(HTTPStatus.OK, self.server.operations_bundle_v2())
            return
        if path == "/api/v1/query":
            try:
                parameters = _request_parameters(request.query, {"q", "ui"})
                document = self.server.semantic_query(
                    encoded_query=parameters.get("q"),
                    encoded_display=parameters.get("ui"),
                )
            except QueryContractError as exc:
                self._send_json(exc.status, query_error_document(exc))
                return
            self._send_json(HTTPStatus.OK, document)
            return
        if path == "/api/v1/actions":
            try:
                parameters = _request_parameters(request.query, {"target"})
            except QueryContractError as exc:
                self._send_json(exc.status, query_error_document(exc))
                return
            document = self.server.action_registry(parameters.get("target", ""))
            if document is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "schema_version": "BoardRequestErrorV1",
                        "error": {
                            "code": "target_not_found",
                            "message": (
                                "the selected Board row is not present in this cache generation"
                            ),
                        },
                    },
                )
                return
            self._send_json(HTTPStatus.OK, document)
            return
        if path == "/api/v1/read-status":
            self._send_json(HTTPStatus.OK, self.server.read_status())
            return
        if path == "/api/v1/system-telemetry":
            try:
                parameters = _request_parameters(request.query, {"demand"})
            except QueryContractError as exc:
                self._send_json(exc.status, query_error_document(exc))
                return
            demand = parameters.get("demand") == "1"
            self._send_json(HTTPStatus.OK, self.server.system_telemetry(demand=demand))
            return
        if path == "/api/v1/usage-dashboard":
            # Fail-soft by contract: upstream, validation, and timeout failures
            # are disclosed in the returned refresh/errors fields rather than
            # turning the rest of the board into a failed page load.
            self._send_json(HTTPStatus.OK, self.server.usage_dashboard())
            return
        if path == "/api/v1/usage-actions/status":
            status, document = self.server.usage_account_status()
            self._send_json(status, document)
            return
        if path == "/api/v1/provider-management":
            status, document = self.server.provider_management_status()
            self._send_json(status, document)
            return
        if path == "/api/v1/schema":
            self._send_json(HTTPStatus.OK, load_schema())
            return
        # One atomic read of the cached snapshot, projected per request. See the
        # native-probe note above for why this is the only one of the three
        # client probes that is answered.
        if path == "/api/menubar":
            self._send_json(HTTPStatus.OK, _menubar_document(self.server.snapshot()))
            return
        if path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        # The coordination map. The native cockpit embeds this route directly,
        # which is why it is a page rather than a tab on the board: the window
        # loads it with its own chrome already drawn.
        if path in {"/cockpit", "/map"}:
            parameters = parse_qs(request.query)
            continuous_embed = (
                parameters.get("embedded") == ["1"]
                and parameters.get("continuous") == ["1"]
            )
            headers = None
            if continuous_embed:
                headers = dict(SECURITY_HEADERS)
                headers.pop("X-Frame-Options", None)
                headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
                    "frame-ancestors 'none'",
                    "frame-ancestors 'self'",
                )
            self._serve_static("cockpit.html", security_headers=headers)
            return
        if path == "/ops":
            self._serve_static("ops-atlas.html")
            return
        if path == "/mesh":
            self._serve_static("swarm-mesh.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_OPTIONS(self) -> None:
        if not self._security_ok():
            return
        path = urlsplit(self.path).path
        self.send_response(HTTPStatus.NO_CONTENT)
        allow = "GET, HEAD, OPTIONS, POST" if path in {"/api/v1/usage-actions", "/api/v1/provider-management"} else "GET, HEAD, OPTIONS"
        self.send_header("Allow", allow)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    def do_POST(self) -> None:
        if not self._security_ok():
            return
        request = urlsplit(self.path)
        if request.path not in {"/api/v1/usage-actions", "/api/v1/provider-management"}:
            self._readonly()
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "").strip()
        if (
            not is_loopback_bind(str(self.server.server_address[0]))
            or not is_loopback_bind(host)
            or not origin
            or not origin_allowed(origin, host)
        ):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "local_origin_required"})
            return
        if request.query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "query_not_allowed"})
            return
        if self.headers.get("Content-Type", "").strip().lower() != "application/json":
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "json_required"}
            )
            return
        if self.headers.get("X-Coord-Usage-Action", "").strip() != "v1":
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "action_header_required"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        maximum = 20_480 if request.path == "/api/v1/provider-management" else 4_096
        if not 0 < length <= maximum:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > maximum else HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "body_size_invalid"},
            )
            return
        try:
            body = json.loads(
                self.rfile.read(length),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_json_object,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        action = body.get("action") if isinstance(body, dict) else None
        if request.path == "/api/v1/provider-management":
            if not isinstance(body, dict) or action not in _PROVIDER_ACTION_SHAPES or set(body) != _PROVIDER_ACTION_SHAPES[action]:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_provider_document"})
                return
            status, document = self.server.provider_management_action(body)
            self._send_json(status, document)
            return
        expected = {
            "profile_add": {"action", "provider", "label"},
            "profile_select": {"action", "provider", "profile_id"},
            "profile_remove": {"action", "provider", "profile_id"},
        }.get(action, {"action"})
        if (
            not isinstance(body, dict)
            or set(body) != expected
            or not all(isinstance(value, str) for value in body.values())
        ):
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_action_document"}
            )
            return
        forwarded = body if str(action).startswith("profile_") else str(action)
        status, document = self.server.usage_account_action(forwarded)
        self._send_json(status, document)

    def _readonly(self) -> None:
        if not self._security_ok():
            return
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    do_PUT = _readonly
    do_PATCH = _readonly
    do_DELETE = _readonly


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    db_path: str | None = None,
    allowed_hosts: set[str] | None = None,
    allow_remote: bool = False,
    refresh_interval: float = 0.0,
    usage_dashboard_proxy: UsageDashboardProxy | None = None,
    system_telemetry_proxy: SystemTelemetryProxy | None = None,
    usage_account_forwarder: UsageAccountActionForwarder | None = None,
    provider_management_forwarder: ProviderManagementForwarder | None = None,
) -> BoardServer:
    loopback = is_loopback_bind(host)
    if not loopback and not allow_remote:
        raise ValueError("non-loopback bind requires allow_remote=True and explicit allowed_hosts")
    if not loopback and not allowed_hosts:
        raise ValueError("remote board bind requires at least one explicit allowed host")
    hosts = set(ALLOWED_HOSTS)
    hosts.update(allowed_hosts or ())
    if loopback:
        hosts.add(host)
    return BoardServer(
        (host, port),
        BoardHandler,
        allowed_hosts=hosts,
        db_path=db_path,
        refresh_interval=refresh_interval,
        usage_dashboard_proxy=usage_dashboard_proxy,
        system_telemetry_proxy=system_telemetry_proxy,
        usage_account_forwarder=usage_account_forwarder,
        provider_management_forwarder=provider_management_forwarder,
    )


def _missing_database_message(db_path: str | None) -> str:
    """What to print when there is no coord.db to read.

    A first run has no database, which is the expected state and not a fault:
    the board is a reader and nothing has written yet. It reached the terminal
    as a nine-frame traceback ending in FileNotFoundError, which reads as a
    broken install rather than an empty one. The path is resolved and named
    here because the default is derived from COORD_PROJECT_ROOT and a reader
    who does not know that cannot tell which file the board went looking for.
    """
    resolved = db_path if db_path is not None else config.coord_db_path()
    return "\n".join((
        f"coord-board: no coordination database at {resolved}",
        "  the board reads an existing database; it never creates one.",
        "  seed a demo board:      python -m coordharness.demo",
        "  or name an existing one: coord-board --db /path/to/coord.db",
    ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coord-board")
    parser.add_argument("--host", default=os.environ.get("COORD_BOARD_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("COORD_BOARD_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=float(os.environ.get("COORD_BOARD_REFRESH_SECONDS", "2")),
        help="background snapshot refresh interval; zero disables live refresh",
    )
    args = parser.parse_args(argv)
    if not is_loopback_bind(args.host) and not args.allow_remote:
        parser.error("non-loopback bind requires --allow-remote and explicit --allowed-host")
    if not is_loopback_bind(args.host) and not args.allowed_host:
        parser.error("remote board bind requires at least one --allowed-host")
    try:
        server = make_server(
            args.host,
            args.port,
            db_path=args.db,
            allowed_hosts=set(args.allowed_host) | set(ALLOWED_HOSTS),
            allow_remote=args.allow_remote,
            refresh_interval=args.refresh_seconds,
        )
    except FileNotFoundError:
        # Only this one condition is translated. A locked, corrupt, or
        # oversized database is not something a caller can be told to fix in
        # three lines, so those keep their traceback.
        print(_missing_database_message(args.db), file=sys.stderr)
        return 2
    wildcard_ipv4 = ".".join(("0", "0", "0", "0"))
    shown_host = args.host if args.host not in {wildcard_ipv4, "::"} else "127.0.0.1"
    print(f"coord-board read-only at http://{shown_host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
