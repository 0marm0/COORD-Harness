"""Pure, signed semantic queries over one coherent public Board generation.

The token is not an authorization primitive.  Its digest detects corruption and
non-canonical encodings; the AST remains plain JSON so operators can inspect,
log, reproduce, and test every predicate.  Evaluation consumes only the public
snapshot rows, raw source-bound graph, and structural ContextV1 document.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import hmac
import json
import re
from typing import Any


QUERY_SCHEMA = "SemanticQueryV1"
DISPLAY_SCHEMA = "DisplayStateV1"
RESPONSE_SCHEMA = "SemanticQueryResponseV1"
ERROR_SCHEMA = "SemanticQueryErrorV1"

MAX_TOKEN_BYTES = 24_000
MAX_DOCUMENT_BYTES = 16_000
MAX_AST_DEPTH = 16
MAX_AST_NODES = 256
MAX_SET_VALUES = 128
MAX_TEXT_BYTES = 512

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PUBLIC_ROW_FIELDS = (
    "id",
    "title",
    "status",
    "bucket",
    "owner",
    "module",
    "group",
    "priority",
    "progress_fraction",
    "eta_seconds",
    "stale",
    "current_step",
)
_STRUCTURAL_CONTEXT_FIELDS = (
    "parent",
    "children",
    "depends_on",
    "dependents",
    "siblings",
    "artifact_recorded",
    "blocked_reason_class",
    "claim_present",
    "lease_remaining_s",
)
_CONTEXT_FIELDS = set(_STRUCTURAL_CONTEXT_FIELDS)
_CONTEXT_OPERATORS = {"eq", "ne", "in", "contains", "present", "gt", "gte", "lt", "lte"}
_DIRECTIONS = {"incoming", "outgoing", "either"}
_LIFECYCLES = {"active", "attention", "terminal", "open", "planned"}
_FRESHNESS = {"fresh", "stale", "lease_current", "lease_expired", "unclaimed"}
_TERMINAL = {
    "archived",
    "canceled",
    "cancelled",
    "closed",
    "complete",
    "completed",
    "done",
    "skipped",
    "superseded",
    "success",
}
_ACTIVE = {"claimed", "running"}
_ATTENTION = {"attention", "artifact_present", "blocked", "failed", "needs_verification"}


class QueryContractError(ValueError):
    """Stable, public-safe failure raised for query or source contract errors."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "$",
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.status = status


def _error(code: str, message: str, path: str = "$", *, status: int = 400) -> None:
    raise QueryContractError(code, message, path=path, status=status)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str, path: str) -> bytes:
    if not value or not _TOKEN_RE.fullmatch(value):
        _error("invalid_token", "token component is not unpadded base64url", path)
    try:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise QueryContractError(
            "invalid_token", "token component is not valid base64url", path=path
        ) from exc


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _error("invalid_document", "expected an object", path)
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    path: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        _error("invalid_document", f"missing required field {sorted(missing)[0]!r}", path)
    if unknown:
        _error("invalid_document", f"unknown field {sorted(unknown)[0]!r}", path)


def _text(value: Any, path: str, *, lower: bool = False, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _error("invalid_document", "expected a string", path)
    rendered = value.strip()
    if not rendered and not allow_empty:
        _error("invalid_document", "string must not be empty", path)
    if len(rendered.encode("utf-8")) > MAX_TEXT_BYTES:
        _error("oversize_document", "string exceeds contract limit", path, status=413)
    return rendered.lower() if lower else rendered


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _error("invalid_document", "expected a boolean", path)
    return value


def _integer(value: Any, path: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _error("invalid_document", f"expected an integer from {minimum} to {maximum}", path)
    return value


def _scalar(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, path, allow_empty=True)
    _error("invalid_document", "expected a JSON scalar", path)


def _set_values(value: Any, path: str, *, lower: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        _error("invalid_document", "expected an array", path)
    if len(value) > MAX_SET_VALUES:
        _error("oversize_document", "array exceeds contract limit", path, status=413)
    rendered = {
        _text(item, f"{path}[{index}]", lower=lower)
        for index, item in enumerate(value)
    }
    return sorted(rendered)


def _expr(value: Any, path: str, *, depth: int, budget: list[int]) -> dict[str, Any]:
    if depth > MAX_AST_DEPTH:
        _error("oversize_ast", "query nesting exceeds contract limit", path, status=413)
    budget[0] += 1
    if budget[0] > MAX_AST_NODES:
        _error("oversize_ast", "query predicate count exceeds contract limit", path, status=413)
    node = _mapping(value, path)
    if len(node) != 1:
        _error("invalid_ast", "each expression must contain exactly one operator", path)
    operator = next(iter(node))
    operand = node[operator]

    if operator in {"all", "any"}:
        if not isinstance(operand, (list, tuple)):
            _error("invalid_ast", f"{operator} expects an array", f"{path}.{operator}")
        children = [
            _expr(child, f"{path}.{operator}[{index}]", depth=depth + 1, budget=budget)
            for index, child in enumerate(operand)
        ]
        # These boolean operators are commutative and idempotent.  Sorting and
        # de-duplicating makes equivalent user construction order one token.
        unique = {_canonical_bytes(child): child for child in children}
        children = [unique[key] for key in sorted(unique)]
        return {operator: children}
    if operator == "not":
        child = _expr(operand, f"{path}.not", depth=depth + 1, budget=budget)
        if set(child) == {"not"}:
            return child["not"]
        return {"not": child}
    if operator in {"lifecycle", "actor", "module", "status", "freshness"}:
        spec = _mapping(operand, f"{path}.{operator}")
        _exact_keys(spec, required={"in"}, path=f"{path}.{operator}")
        values = _set_values(spec["in"], f"{path}.{operator}.in", lower=True)
        if not values:
            _error("invalid_ast", "predicate set must not be empty", f"{path}.{operator}.in")
        allowed = _LIFECYCLES if operator == "lifecycle" else _FRESHNESS if operator == "freshness" else None
        if allowed is not None and not set(values) <= allowed:
            unknown = sorted(set(values) - allowed)[0]
            _error("invalid_ast", f"unsupported {operator} value {unknown!r}", f"{path}.{operator}.in")
        return {operator: {"in": values}}
    if operator == "evidence":
        spec = _mapping(operand, f"{path}.evidence")
        _exact_keys(spec, required={"recorded"}, path=f"{path}.evidence")
        return {"evidence": {"recorded": _boolean(spec["recorded"], f"{path}.evidence.recorded")}}
    if operator == "graph_relation":
        spec = _mapping(operand, f"{path}.graph_relation")
        _exact_keys(
            spec,
            required=set(),
            optional={"direction", "kind_in", "related_ids", "relationship_state_in", "min_count"},
            path=f"{path}.graph_relation",
        )
        direction = _text(spec.get("direction", "either"), f"{path}.graph_relation.direction", lower=True)
        if direction not in _DIRECTIONS:
            _error("invalid_ast", f"unsupported direction {direction!r}", f"{path}.graph_relation.direction")
        return {
            "graph_relation": {
                "direction": direction,
                "kind_in": _set_values(spec.get("kind_in", []), f"{path}.graph_relation.kind_in", lower=True),
                "min_count": _integer(spec.get("min_count", 1), f"{path}.graph_relation.min_count", minimum=0),
                "related_ids": _set_values(spec.get("related_ids", []), f"{path}.graph_relation.related_ids"),
                "relationship_state_in": _set_values(
                    spec.get("relationship_state_in", []),
                    f"{path}.graph_relation.relationship_state_in",
                    lower=True,
                ),
            }
        }
    if operator == "job":
        spec = _mapping(operand, f"{path}.job")
        _exact_keys(
            spec,
            required=set(),
            optional={"is_job", "state_in", "progress", "eta"},
            path=f"{path}.job",
        )
        progress = _text(spec.get("progress", "either"), f"{path}.job.progress", lower=True)
        eta = _text(spec.get("eta", "either"), f"{path}.job.eta", lower=True)
        if progress not in {"known", "unknown", "either"}:
            _error("invalid_ast", "unsupported job progress selector", f"{path}.job.progress")
        if eta not in {"known", "unknown", "either"}:
            _error("invalid_ast", "unsupported job eta selector", f"{path}.job.eta")
        return {
            "job": {
                "eta": eta,
                "is_job": _boolean(spec.get("is_job", True), f"{path}.job.is_job"),
                "progress": progress,
                "state_in": _set_values(spec.get("state_in", []), f"{path}.job.state_in", lower=True),
            }
        }
    if operator == "context":
        spec = _mapping(operand, f"{path}.context")
        _exact_keys(spec, required={"field", "operator", "value"}, path=f"{path}.context")
        field = _text(spec["field"], f"{path}.context.field")
        comparison = _text(spec["operator"], f"{path}.context.operator", lower=True)
        if field not in _CONTEXT_FIELDS:
            _error("invalid_ast", f"context field {field!r} is not structural", f"{path}.context.field")
        if comparison not in _CONTEXT_OPERATORS:
            _error("invalid_ast", f"unsupported context operator {comparison!r}", f"{path}.context.operator")
        raw = spec["value"]
        if comparison == "in":
            if not isinstance(raw, (list, tuple)):
                _error("invalid_ast", "context in expects an array", f"{path}.context.value")
            if len(raw) > MAX_SET_VALUES:
                _error("oversize_document", "array exceeds contract limit", f"{path}.context.value", status=413)
            values = [_scalar(item, f"{path}.context.value[{index}]") for index, item in enumerate(raw)]
            unique = {_canonical_bytes(item): item for item in values}
            canonical_value: Any = [unique[key] for key in sorted(unique)]
        else:
            canonical_value = _scalar(raw, f"{path}.context.value")
        return {"context": {"field": field, "operator": comparison, "value": canonical_value}}
    _error("invalid_ast", f"unsupported expression operator {operator!r}", path)


def _canonical_query(value: Any, *, default: bool = False) -> dict[str, Any]:
    if value is None and default:
        value = {"schema_version": QUERY_SCHEMA, "expr": {"all": []}}
    document = _mapping(value, "$")
    _exact_keys(document, required={"expr"}, optional={"schema_version"}, path="$")
    version = document.get("schema_version", QUERY_SCHEMA)
    if version != QUERY_SCHEMA:
        _error("unsupported_schema", f"expected {QUERY_SCHEMA}", "$.schema_version")
    result = {
        "expr": _expr(document["expr"], "$.expr", depth=1, budget=[0]),
        "schema_version": QUERY_SCHEMA,
    }
    if len(_canonical_bytes(result)) > MAX_DOCUMENT_BYTES:
        _error("oversize_document", "canonical query exceeds contract limit", status=413)
    return result


def _canonical_display(value: Any, *, default: bool = False) -> dict[str, Any]:
    if value is None and default:
        value = {}
    document = _mapping(value, "$")
    _exact_keys(
        document,
        required=set(),
        optional={"schema_version", "view", "sort", "selected_id", "expanded_ids"},
        path="$",
    )
    version = document.get("schema_version", DISPLAY_SCHEMA)
    if version != DISPLAY_SCHEMA:
        _error("unsupported_schema", f"expected {DISPLAY_SCHEMA}", "$.schema_version")
    result = {
        "expanded_ids": _set_values(document.get("expanded_ids", []), "$.expanded_ids"),
        "schema_version": DISPLAY_SCHEMA,
        "selected_id": _text(document.get("selected_id", ""), "$.selected_id", allow_empty=True),
        "sort": _text(document.get("sort", "id"), "$.sort", lower=True),
        "view": _text(document.get("view", "list"), "$.view", lower=True),
    }
    if len(_canonical_bytes(result)) > MAX_DOCUMENT_BYTES:
        _error("oversize_document", "canonical display state exceeds contract limit", status=413)
    return result


def _encode(value: Any, *, prefix: str) -> str:
    payload = _canonical_bytes(value)
    token = f"{prefix}.{_b64(payload)}.{_b64(sha256(payload).digest())}"
    if len(token.encode("ascii")) > MAX_TOKEN_BYTES:
        _error("oversize_token", "encoded token exceeds contract limit", status=413)
    return token


def _decode(token: Any, *, prefix: str, canonicalizer: Any) -> dict[str, Any]:
    if not isinstance(token, str):
        _error("invalid_token", "token must be a string")
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        _error("oversize_token", "encoded token exceeds contract limit", status=413)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        _error("invalid_token", f"expected {prefix} token")
    payload = _unb64(parts[1], "$.payload")
    digest = _unb64(parts[2], "$.digest")
    if len(payload) > MAX_DOCUMENT_BYTES:
        _error("oversize_token", "decoded token exceeds contract limit", status=413)
    if len(digest) != sha256().digest_size or not hmac.compare_digest(digest, sha256(payload).digest()):
        _error("tampered_token", "token digest does not match payload")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueryContractError("invalid_token", "token payload is not valid JSON") from exc
    canonical = canonicalizer(decoded)
    if _encode(canonical, prefix=prefix) != token:
        _error("noncanonical_token", "token payload is valid but not canonical")
    return canonical


def encode_query(query: Mapping[str, Any]) -> str:
    """Return the sole canonical, unpadded base64url representation of a query."""
    return _encode(_canonical_query(query), prefix="sq1")


def decode_query(token: str) -> dict[str, Any]:
    """Verify and decode one canonical SemanticQueryV1 token."""
    return _decode(token, prefix="sq1", canonicalizer=_canonical_query)


def encode_display_state(display_state: Mapping[str, Any]) -> str:
    """Encode UI state separately; it can never widen or narrow query truth."""
    return _encode(_canonical_display(display_state), prefix="ds1")


def decode_display_state(token: str) -> dict[str, Any]:
    """Verify and decode one canonical DisplayStateV1 token."""
    return _decode(token, prefix="ds1", canonicalizer=_canonical_display)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _timestamp(value: Any, path: str) -> tuple[str, float]:
    if not isinstance(value, str) or not value.strip():
        _error("invalid_source", "source generated_at must be a timestamp", path)
    rendered = value.strip()
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryContractError(
            "invalid_source", "source generated_at is not ISO-8601", path=path
        ) from exc
    if parsed.tzinfo is None:
        _error("invalid_source", "source generated_at must include a timezone", path)
    return rendered, parsed.timestamp()


def _source_documents(
    snapshot: Mapping[str, Any], graph: Mapping[str, Any], context: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    snapshot = _mapping(snapshot, "$.snapshot")
    graph = _mapping(graph, "$.graph")
    context = _mapping(context, "$.context")
    if graph.get("schema_version") == "GraphEnvelopeV1" or "population" in graph:
        _error("partial_graph", "semantic evaluation requires the raw coherent graph", "$.graph")
    rows_raw = snapshot.get("rows")
    edges_raw = graph.get("edges")
    items_raw = context.get("items")
    if not isinstance(rows_raw, (list, tuple)):
        _error("invalid_source", "snapshot rows must be an array", "$.snapshot.rows")
    if not isinstance(edges_raw, (list, tuple)):
        _error("invalid_source", "graph edges must be an array", "$.graph.edges")
    if not isinstance(items_raw, (list, tuple)):
        _error("invalid_source", "context items must be an array", "$.context.items")

    rows: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    for index, raw in enumerate(rows_raw):
        item = _mapping(raw, f"$.snapshot.rows[{index}]")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity.strip():
            _error("invalid_source", "public row has no identity", f"$.snapshot.rows[{index}].id")
        identity = identity.strip()
        if identity in seen_rows:
            _error("invalid_source", "duplicate public row identity", f"$.snapshot.rows[{index}].id")
        seen_rows.add(identity)
        row = {field: item.get(field) for field in _PUBLIC_ROW_FIELDS}
        row["id"] = identity
        rows.append(row)
    rows.sort(key=lambda row: row["id"])

    contexts: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(items_raw):
        item = _mapping(raw, f"$.context.items[{index}]")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity.strip():
            _error("invalid_source", "context item has no identity", f"$.context.items[{index}].id")
        identity = identity.strip()
        if identity in contexts:
            _error("invalid_source", "duplicate context identity", f"$.context.items[{index}].id")
        contexts[identity] = {
            field: item.get(field)
            for field in _STRUCTURAL_CONTEXT_FIELDS
        }

    edges: list[dict[str, Any]] = []
    for index, raw in enumerate(edges_raw):
        item = _mapping(raw, f"$.graph.edges[{index}]")
        source = item.get("source")
        target = item.get("target")
        if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
            _error("invalid_source", "graph edge has no endpoint identity", f"$.graph.edges[{index}]")
        edges.append(
            {
                "id": str(item.get("id") or ""),
                "kind": str(item.get("kind") or "").strip().lower(),
                "relationship_state": str(item.get("relationship_state") or "").strip().lower(),
                "source": source.strip(),
                "target": target.strip(),
            }
        )
    edges.sort(key=lambda edge: (edge["source"], edge["target"], edge["kind"], edge["id"]))

    snapshot_stamp, snapshot_time = _timestamp(
        snapshot.get("generated_at"), "$.snapshot.generated_at"
    )
    graph_stamp, graph_time = _timestamp(graph.get("generated_at"), "$.graph.generated_at")
    context_stamp, context_time = _timestamp(
        context.get("generated_at"), "$.context.generated_at"
    )
    times = (snapshot_time, graph_time, context_time)
    receipt = {
        "coherent": True,
        "context_generated_at": context_stamp,
        "context_sha256": sha256(_canonical_bytes(context)).hexdigest(),
        "context_schema_version": str(context.get("schema_version") or "unknown"),
        "graph_generated_at": graph_stamp,
        "graph_sha256": sha256(_canonical_bytes(graph)).hexdigest(),
        "graph_schema_version": str(graph.get("schema_version") or "unknown"),
        "max_generation_skew_ms": round((max(times) - min(times)) * 1000.0, 3),
        "snapshot_generated_at": snapshot_stamp,
        "snapshot_sha256": sha256(_canonical_bytes(snapshot)).hexdigest(),
        "snapshot_schema_version": str(snapshot.get("schema_version") or "unknown"),
    }
    return rows, contexts, edges, receipt


def _node_id(row: Mapping[str, Any]) -> str:
    identity = str(row.get("id") or "")
    return identity if identity.startswith(("work:", "job:")) else f"work:{identity}"


def _edge_index(
    edges: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    """Index raw edges once; each row subsequently sees only its incidence."""
    mutable: dict[str, list[Mapping[str, Any]]] = {}
    for edge in edges:
        source = str(edge["source"])
        target = str(edge["target"])
        mutable.setdefault(source, []).append(edge)
        if target != source:
            mutable.setdefault(target, []).append(edge)
    return {identity: tuple(values) for identity, values in mutable.items()}


def _related_matches(identity: str, allowed: Sequence[str]) -> bool:
    if not allowed:
        return True
    variants = {identity, identity.split(":", 1)[1] if ":" in identity else identity}
    return bool(variants & set(allowed))


def _lifecycle(status: str) -> set[str]:
    values = {"terminal"} if status in _TERMINAL else {"open"}
    if status in _ACTIVE:
        values.add("active")
    elif status in _ATTENTION:
        values.add("attention")
    elif status not in _TERMINAL:
        values.add("planned")
    return values


def _compare_context(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "present":
        return (actual is not None and actual != "" and actual != []) is bool(expected)
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "contains":
        return isinstance(actual, (str, list, tuple, set)) and expected in actual
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    return {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
    }.get(operator, False)


def _matches(
    expression: Mapping[str, Any],
    row: Mapping[str, Any],
    item: Mapping[str, Any],
    edges: Sequence[Mapping[str, Any]],
    *,
    snapshot_stale: bool,
) -> bool:
    operator = next(iter(expression))
    operand = expression[operator]
    if operator == "all":
        return all(_matches(child, row, item, edges, snapshot_stale=snapshot_stale) for child in operand)
    if operator == "any":
        return any(_matches(child, row, item, edges, snapshot_stale=snapshot_stale) for child in operand)
    if operator == "not":
        return not _matches(operand, row, item, edges, snapshot_stale=snapshot_stale)
    if operator == "lifecycle":
        return bool(_lifecycle(str(row.get("status") or "").lower()) & set(operand["in"]))
    if operator == "actor":
        return str(row.get("owner") or "").strip().lower() in operand["in"]
    if operator == "module":
        values = {str(row.get(key) or "").strip().lower() for key in ("module", "group")}
        return bool(values & set(operand["in"]))
    if operator == "status":
        return str(row.get("status") or "").strip().lower() in operand["in"]
    if operator == "evidence":
        node = _node_id(row)
        recorded = bool(item.get("artifact_recorded")) or any(
            edge["source"] == node and edge["kind"] in {"evidence", "runtime_evidence"}
            for edge in edges
        )
        return recorded is operand["recorded"]
    if operator == "freshness":
        states = {"stale" if snapshot_stale or bool(row.get("stale")) else "fresh"}
        if not item.get("claim_present"):
            states.add("unclaimed")
        else:
            lease = item.get("lease_remaining_s")
            states.add("lease_expired" if isinstance(lease, (int, float)) and lease < 0 else "lease_current")
        return bool(states & set(operand["in"]))
    if operator == "graph_relation":
        node = _node_id(row)
        found = []
        for edge in edges:
            if operand["direction"] == "outgoing" and edge["source"] != node:
                continue
            if operand["direction"] == "incoming" and edge["target"] != node:
                continue
            if operand["direction"] == "either" and node not in {edge["source"], edge["target"]}:
                continue
            related = edge["target"] if edge["source"] == node else edge["source"]
            if operand["kind_in"] and edge["kind"] not in operand["kind_in"]:
                continue
            if operand["relationship_state_in"] and edge["relationship_state"] not in operand["relationship_state_in"]:
                continue
            if not _related_matches(related, operand["related_ids"]):
                continue
            found.append(edge)
        return len(found) >= operand["min_count"]
    if operator == "job":
        is_job = str(row.get("bucket") or "").lower() == "job" or str(row.get("id") or "").startswith("job:")
        if is_job is not operand["is_job"]:
            return False
        if operand["state_in"] and str(row.get("status") or "").lower() not in operand["state_in"]:
            return False
        for field, selector in (("progress_fraction", operand["progress"]), ("eta_seconds", operand["eta"])):
            if selector != "either" and (row.get(field) is not None) is not (selector == "known"):
                return False
        return True
    if operator == "context":
        return _compare_context(item.get(operand["field"]), operand["operator"], operand["value"])
    raise AssertionError(f"unhandled canonical operator {operator}")


def _detail(row: Mapping[str, Any], item: Mapping[str, Any], edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    node = _node_id(row)
    incoming = sum(1 for edge in edges if edge["target"] == node)
    outgoing = sum(1 for edge in edges if edge["source"] == node)
    return {
        "context": {field: item.get(field) for field in _STRUCTURAL_CONTEXT_FIELDS},
        "id": row["id"],
        "relation_counts": {"incoming": incoming, "outgoing": outgoing},
        "row": {field: row.get(field) for field in _PUBLIC_ROW_FIELDS},
    }


def _operations_receipt(operations: Any) -> dict[str, Any] | None:
    if operations is None:
        return None
    document = _mapping(operations, "$.operations")
    envelope = document.get("graph_envelope")
    if not isinstance(envelope, Mapping):
        return {"available": False}
    return {
        "available": True,
        "caps": dict(envelope.get("caps")) if isinstance(envelope.get("caps"), Mapping) else {},
        "complete": bool(envelope.get("complete")),
        "emitted": dict(envelope.get("emitted")) if isinstance(envelope.get("emitted"), Mapping) else {},
        "omitted": dict(envelope.get("omitted")) if isinstance(envelope.get("omitted"), Mapping) else {},
        "population": dict(envelope.get("population")) if isinstance(envelope.get("population"), Mapping) else {},
    }


def build_semantic_query_response(
    snapshot: Mapping[str, Any],
    graph: Mapping[str, Any],
    context: Mapping[str, Any],
    operations: Mapping[str, Any] | None = None,
    *,
    encoded_query: str | None = None,
    query: Mapping[str, Any] | None = None,
    encoded_display: str | None = None,
    display_state: Mapping[str, Any] | None = None,
    cache_generation: int = 0,
    result_cap: int = 200,
) -> dict[str, Any]:
    """Evaluate a query against a complete coherent generation without mutation.

    Display state is returned for client convenience but deliberately has no
    influence on population, matched identities, result order, or detail caps.
    """
    if encoded_query is not None and query is not None:
        _error("ambiguous_query", "provide encoded_query or query, not both")
    if encoded_display is not None and display_state is not None:
        _error("ambiguous_display", "provide encoded_display or display_state, not both")
    canonical_query = decode_query(encoded_query) if encoded_query is not None else _canonical_query(query, default=True)
    canonical_display = (
        decode_display_state(encoded_display)
        if encoded_display is not None
        else _canonical_display(display_state, default=True)
    )
    generation = _integer(cache_generation, "$.cache_generation", minimum=0)
    cap = _integer(result_cap, "$.result_cap", minimum=1, maximum=10_000)
    rows, contexts, edges, source = _source_documents(snapshot, graph, context)
    edges_by_node = _edge_index(edges)
    snapshot_stale = bool(snapshot.get("stale"))
    matched = [
        row
        for row in rows
        if _matches(
            canonical_query["expr"],
            row,
            contexts.get(row["id"], {}),
            edges_by_node.get(_node_id(row), ()),
            snapshot_stale=snapshot_stale,
        )
    ]
    matched_ids = [row["id"] for row in matched]
    detailed = matched[:cap]
    omitted_count = len(matched) - len(detailed)
    operations_receipt = _operations_receipt(operations)
    omission_receipt: dict[str, Any] = {
        "detail_cap": cap,
        "detailed_count": len(detailed),
        "matched_ids_complete": True,
        "omitted_detail_count": omitted_count,
        "reason": "result_cap" if omitted_count else "none",
    }
    if operations_receipt is not None:
        omission_receipt["operations_graph_envelope"] = operations_receipt
    return {
        "cache_generation": generation,
        "complete": True,
        "display_state": canonical_display,
        "display_token": encode_display_state(canonical_display),
        "generated_at": source["snapshot_generated_at"],
        "matched_ids": matched_ids,
        "omission_receipt": omission_receipt,
        "population": {
            "detailed": len(detailed),
            "matched": len(matched),
            "omitted_detail": omitted_count,
            "rows": len(rows),
        },
        "query": canonical_query,
        "query_token": encode_query(canonical_query),
        "results": [
            _detail(
                row,
                contexts.get(row["id"], {}),
                edges_by_node.get(_node_id(row), ()),
            )
            for row in detailed
        ],
        "schema_version": RESPONSE_SCHEMA,
        "source": source,
    }


def query_error_document(error: QueryContractError | Exception) -> dict[str, Any]:
    """Return a deterministic, token-free public error document."""
    if isinstance(error, QueryContractError):
        code, message, path = error.code, error.message, error.path
    else:
        code, message, path = "query_error", "semantic query could not be evaluated", "$"
    return {
        "error": {"code": code, "message": message, "path": path},
        "schema_version": ERROR_SCHEMA,
    }


__all__ = [
    "DISPLAY_SCHEMA",
    "ERROR_SCHEMA",
    "QUERY_SCHEMA",
    "RESPONSE_SCHEMA",
    "QueryContractError",
    "build_semantic_query_response",
    "decode_display_state",
    "decode_query",
    "encode_display_state",
    "encode_query",
    "query_error_document",
]
