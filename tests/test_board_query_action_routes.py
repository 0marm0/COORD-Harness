from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
from typing import Any, Iterator
from urllib.parse import quote, urlencode

from jsonschema import Draft202012Validator
import pytest

from coordharness import demo
from coordharness.board.semantic_query import encode_display_state, encode_query
from coordharness.board.server import make_server


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "src" / "coordharness" / "board"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


QUERY_VALIDATOR = _validator("semantic_query_response_v1.schema.json")
ACTION_VALIDATOR = _validator("action_registry_v1.schema.json")


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[Any]:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    port: int, path: str, *, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _json_get(board: Any, path: str) -> tuple[dict[str, str], dict[str, Any]]:
    status, headers, body = _request(board.server_port, path)
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    return headers, json.loads(body)


def _without_display(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"display_state", "display_token"}
    }


def test_query_route_matches_the_complete_cache_and_display_is_population_neutral(
    board: Any,
) -> None:
    _headers, default = _json_get(board, "/api/v1/query")
    QUERY_VALIDATOR.validate(default)

    snapshot_ids = sorted(str(row["id"]) for row in board.snapshot()["rows"])
    generation = board.read_status()["cache_generation"]
    assert default["matched_ids"] == snapshot_ids
    assert default["cache_generation"] == generation
    assert default["population"] == {
        "rows": len(snapshot_ids),
        "matched": len(snapshot_ids),
        "detailed": len(snapshot_ids),
        "omitted_detail": 0,
    }
    assert default["omission_receipt"]["matched_ids_complete"] is True

    query_token = encode_query({"expr": {"status": {"in": ["blocked"]}}})
    first_display = encode_display_state(
        {
            "view": "mesh",
            "sort": "priority",
            "selected_id": "ML-202",
            "expanded_ids": ["INIT-MODEL"],
        }
    )
    second_display = encode_display_state(
        {
            "view": "compact",
            "sort": "id",
            "selected_id": "SRCH-403",
            "expanded_ids": ["INIT-SEARCH"],
        }
    )
    first_path = "/api/v1/query?" + urlencode({"q": query_token, "ui": first_display})
    second_path = "/api/v1/query?" + urlencode({"q": query_token, "ui": second_display})
    _first_headers, first = _json_get(board, first_path)
    _second_headers, second = _json_get(board, second_path)
    QUERY_VALIDATOR.validate(first)
    QUERY_VALIDATOR.validate(second)

    expected_blocked = sorted(
        str(row["id"])
        for row in board.snapshot()["rows"]
        if str(row.get("status") or "").lower() == "blocked"
    )
    assert expected_blocked
    assert first["matched_ids"] == second["matched_ids"] == expected_blocked
    assert first["query_token"] == second["query_token"] == query_token
    assert first["display_token"] == first_display
    assert second["display_token"] == second_display
    assert first["display_state"] != second["display_state"]
    assert _without_display(first) == _without_display(second)


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        ("/api/v1/query?q=not-a-semantic-token-SECRET-INVALID", "SECRET-INVALID"),
        ("/api/v1/query?q={tampered}", ""),
        ("/api/v1/query?q={valid}&q=SECRET-DUPLICATE", "SECRET-DUPLICATE"),
        ("/api/v1/query?future=SECRET-UNKNOWN", "SECRET-UNKNOWN"),
        ("/api/v1/query?SECRET-PARAMETER-NAME=value", "SECRET-PARAMETER-NAME"),
        ("/api/v1/actions?target=UI-101&target=SECRET-ACTION-DUP", "SECRET-ACTION-DUP"),
        ("/api/v1/actions?future=SECRET-ACTION-UNKNOWN", "SECRET-ACTION-UNKNOWN"),
    ],
)
def test_route_parameters_fail_closed_without_echoing_values(
    board: Any, path: str, secret: str
) -> None:
    valid = encode_query({"expr": {"status": {"in": ["running"]}}})
    prefix, payload, digest = valid.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    tampered = f"{prefix}.{payload[:-1]}{replacement}.{digest}"
    request_path = path.format(valid=quote(valid, safe=""), tampered=quote(tampered, safe=""))
    if not secret:
        secret = tampered

    status, headers, body = _request(board.server_port, request_path)
    assert status == 400
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    document = json.loads(body)
    assert document["schema_version"] == "SemanticQueryErrorV1"
    assert set(document) == {"schema_version", "error"}
    assert set(document["error"]) == {"code", "message", "path"}
    assert all(document["error"].values())
    assert secret not in body.decode("utf-8")


def test_action_route_is_schema_valid_preview_only_and_has_no_mutation_authority(
    board: Any,
) -> None:
    target_id = next(
        str(row["id"])
        for row in board.snapshot()["rows"]
        if not str(row["id"]).startswith("job:")
    )
    path = "/api/v1/actions?" + urlencode({"target": target_id})
    _headers, document = _json_get(board, path)
    ACTION_VALIDATOR.validate(document)

    assert document["target"]["id"] == target_id
    assert document["source"] == {
        "cache_generation": board.read_status()["cache_generation"],
        "generated_at": board.snapshot()["generated_at"],
        "read_only": True,
        "source_face": "loopback_board",
    }
    available = {action["id"] for action in document["actions"] if action["available"]}
    assert available == {"inspect", "copy_id"}

    mutations = [action for action in document["actions"] if action["mutation"]]
    assert mutations
    assert document["counts"]["available_mutations"] == 0
    assert document["counts"]["reachable_mutations"] == 0
    assert all(not action["available"] and not action["reachable"] for action in mutations)
    assert all(isinstance(action["reason"], str) and action["reason"] for action in mutations)
    assert all(
        check["reason"]
        for action in mutations
        for check in action["checks"]
        if not check["passed"]
    )

    missing = "NO-SUCH-TARGET-SECRET"
    status, headers, body = _request(
        board.server_port,
        "/api/v1/actions?" + urlencode({"target": missing}),
    )
    assert status == 404
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {
        "schema_version": "BoardRequestErrorV1",
        "error": {
            "code": "target_not_found",
            "message": "the selected Board row is not present in this cache generation",
        },
    }
    assert missing not in body.decode("utf-8")

    native_status, native_headers, native_body = _request(
        board.server_port, "/api/native/action"
    )
    assert native_status == 404
    assert native_headers["Content-Type"] == "text/plain; charset=utf-8"
    assert native_body == b"not found"


def test_query_and_action_routes_have_truthful_head_and_reject_every_write_method(
    board: Any,
) -> None:
    target_id = str(board.snapshot()["rows"][0]["id"])
    routes = (
        "/api/v1/query",
        "/api/v1/actions?" + urlencode({"target": target_id}),
    )
    expected_schemas = {"SemanticQueryResponseV1", "ActionRegistryV1"}

    for path in routes:
        get_status, get_headers, get_body = _request(board.server_port, path)
        assert get_status == 200
        assert get_headers["Content-Type"] == "application/json; charset=utf-8"
        assert json.loads(get_body)["schema_version"] in expected_schemas

        head_status, head_headers, head_body = _request(
            board.server_port, path, method="HEAD"
        )
        assert head_status == 200
        assert head_headers["Content-Type"] == get_headers["Content-Type"]
        assert head_headers["Content-Length"] == get_headers["Content-Length"]
        assert int(head_headers["Content-Length"]) == len(get_body)
        assert head_body == b""

        for method in WRITE_METHODS:
            status, headers, body = _request(board.server_port, path, method=method)
            assert status == 405, f"{method} {path} -> {status}"
            assert headers["Allow"] == "GET, HEAD, OPTIONS"
            assert body == b""
