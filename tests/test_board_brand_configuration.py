"""The board is embeddable, so the name on it has to be the embedder's.

An operator who points this read-only board at their own database and frames it
inside their own tool sees their coordination data under this project's mark.
The workaround for that is CSS that hides the brand after the server has
already sent it, which is a viewer patching over a server decision. So the
server makes the decision: `COORD_BOARD_BRAND_NAME` and
`COORD_BOARD_BRAND_TAGLINE` are read once at startup and painted onto the three
places each page carries a brand.

Two failure modes are worth more than the feature, and both have tests here.

The first is that a branding feature is a change to every page, including the
pages nobody configured. Byte-identity is therefore asserted against the
committed files themselves rather than against a remembered string, and
`apply_brand` is asserted to hand back the same object, so the default path is
provably not a substitution that happens to be a no-op.

The second is that the configured value is operator-supplied text on its way
into served HTML. A product whose pitch is that it is safe to point at your own
database does not get to ship an injection in its own header, so the escaping
test drives a real payload through the real route and checks the bytes that
arrive rather than the value that was set.
"""

from __future__ import annotations

import http.client
from pathlib import Path
import re
import threading
from typing import Iterator

import pytest

from coordharness import demo
from coordharness.board import server as board_server
from coordharness.board.server import apply_brand, make_server

STATIC = Path(board_server.__file__).parent / "static"

# Route to the file it serves. Both halves matter: the route proves the handler
# applies branding where a browser actually arrives, and the file is the
# committed evidence the default path is compared against.
PAGES = {
    "/": "index.html",
    "/cockpit": "cockpit.html",
    "/mesh": "swarm-mesh.html",
    "/ops": "ops-atlas.html",
}

MARK_RE = re.compile(rb'<span class="shell-mark">([^<]*)</span>')
SUB_RE = re.compile(rb'<span class="shell-sub">([^<]*)</span>')
TITLE_RE = re.compile(rb"<title>([^<]*)</title>")

# A name that is markup, quotes and an attribute break in one value. It is
# short enough to pass length validation, so nothing but escaping stands
# between it and the served page.
HOSTILE_NAME = "\"><script>alert('xss')</script>"


def _committed(page: str) -> bytes:
    return (STATIC / page).read_bytes()


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[int]:
    """A running board over a seeded demo database, on an ephemeral port."""
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _get(port: int, route: str) -> tuple[bytes, dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", route, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (route, response.status)
        return body, dict(response.getheaders())
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1. Nothing configured changes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("route", "page"), sorted(PAGES.items()))
def test_an_unconfigured_board_serves_the_committed_bytes(
    board: int, route: str, page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_BOARD_BRAND_NAME", raising=False)
    monkeypatch.delenv("COORD_BOARD_BRAND_TAGLINE", raising=False)
    body, headers = _get(board, route)
    assert body == _committed(page)
    assert int(headers["Content-Length"]) == len(body)


def test_the_default_path_is_not_a_substitution_at_all() -> None:
    # Identity, not equality. An unconfigured board must not be running the
    # rewrite with arguments that happen to cancel out, because that is the
    # version of this feature that breaks a page nobody asked it to touch.
    for page in PAGES.values():
        raw = _committed(page)
        assert apply_brand(raw, "COORD", None) is raw


@pytest.mark.parametrize("blank", ("", "   ", "\t\n "))
def test_a_blank_configured_name_falls_back_rather_than_serving_no_brand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # A shell exports an empty variable readily. Treating that as "the brand is
    # the empty string" would serve a nameless shell, which is worse than the
    # default it replaced.
    monkeypatch.setenv("COORD_BOARD_BRAND_NAME", blank)
    monkeypatch.setenv("COORD_BOARD_BRAND_TAGLINE", blank)
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for route, page in PAGES.items():
            body, _ = _get(server.server_port, route)
            assert body == _committed(page)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# ---------------------------------------------------------------------------
# 2. Something configured changes the three places that carry the brand.
# ---------------------------------------------------------------------------


@pytest.fixture()
def branded_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setenv("COORD_BOARD_BRAND_NAME", "Northwind")
    monkeypatch.setenv("COORD_BOARD_BRAND_TAGLINE", "operations desk")
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize(("route", "page"), sorted(PAGES.items()))
def test_the_configured_brand_reaches_every_page(
    branded_board: int, route: str, page: str
) -> None:
    body, headers = _get(branded_board, route)
    assert MARK_RE.findall(body) == [b"Northwind"]
    assert SUB_RE.findall(body) == [b"operations desk"]

    title = TITLE_RE.search(body)
    assert title is not None
    assert b"COORD" not in title.group(1)
    assert b"Northwind" in title.group(1)

    # The page still says which page it is. Four tabs reading "Northwind" would
    # be a worse header than the one this replaced, so the title keeps whatever
    # distinguishes it and swaps only the product name.
    committed_title = TITLE_RE.search(_committed(page))
    assert committed_title is not None
    expected = committed_title.group(1).replace(b"COORD", b"Northwind")
    assert title.group(1) == expected

    assert int(headers["Content-Length"]) == len(body)


@pytest.mark.parametrize(("route", "page"), sorted(PAGES.items()))
def test_branding_rewrites_the_brand_and_nothing_else(
    branded_board: int, route: str, page: str
) -> None:
    # The scope guard. `coordination`, `coord.db` and `COORDINATION TRAFFIC`
    # name what the product is about, not what it is called; an operator
    # renaming the product has not renamed the domain, and a rewrite that
    # touched them would quietly corrupt copy and an element id.
    body, _ = _get(branded_board, route)
    committed = _committed(page)
    restored = TITLE_RE.sub(
        lambda match: b"<title>" + match.group(1).replace(b"Northwind", b"COORD") + b"</title>",
        body,
    )
    restored = MARK_RE.sub(b'<span class="shell-mark">COORD</span>', restored)
    original_sub = SUB_RE.search(committed)
    assert original_sub is not None
    restored = SUB_RE.sub(
        b'<span class="shell-sub">' + original_sub.group(1) + b"</span>", restored
    )
    assert restored == committed


def test_the_name_only_has_to_be_configured_to_be_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tagline is a separate knob. Setting only the name must leave each
    # page's own sub-label alone rather than blanking it.
    monkeypatch.setenv("COORD_BOARD_BRAND_NAME", "Northwind")
    monkeypatch.delenv("COORD_BOARD_BRAND_TAGLINE", raising=False)
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        index, _ = _get(server.server_port, "/")
        mesh, _ = _get(server.server_port, "/mesh")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert SUB_RE.findall(index) == [b"read-only runtime"]
    assert SUB_RE.findall(mesh) == [b"Intelligence"]
    assert MARK_RE.findall(index) == [b"Northwind"]


# ---------------------------------------------------------------------------
# 3. The configured value is untrusted text reaching served HTML.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("route", "page"), sorted(PAGES.items()))
def test_a_name_that_is_markup_is_escaped_and_cannot_break_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, page: str
) -> None:
    monkeypatch.setenv("COORD_BOARD_BRAND_NAME", HOSTILE_NAME)
    monkeypatch.setenv("COORD_BOARD_BRAND_TAGLINE", HOSTILE_NAME)
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    server = make_server(port=0, db_path=str(database), refresh_interval=3600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body, _ = _get(server.server_port, route)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    escaped = b"&quot;&gt;&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
    assert MARK_RE.findall(body) == [escaped]
    assert SUB_RE.findall(body) == [escaped]

    # The payload is present as text and absent as markup.
    assert HOSTILE_NAME.encode("utf-8") not in body
    assert b"<script>alert" not in body

    # These pages legitimately carry `<script src=...></script>` tags, so the
    # proof is differential rather than absolute: the served page must have
    # exactly the tags the committed one has and not one more. An injected `">`
    # would have closed the span's attribute and opened an element, and a
    # counted absolute would have passed on a page that never had scripts.
    committed = _committed(page)
    for tag in (b"<script", b"</script>", b"<span", b"</span>"):
        assert body.count(tag) == committed.count(tag), tag
    assert body.count(b"<title>") == 1 == body.count(b"</title>")


# ---------------------------------------------------------------------------
# 4. A value the shell cannot paint is refused with a sentence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    (
        ("COORD_BOARD_BRAND_NAME", "N" * 65, "at most 64 characters"),
        ("COORD_BOARD_BRAND_NAME", "North\nwind", "control characters"),
        ("COORD_BOARD_BRAND_TAGLINE", "T" * 65, "at most 64 characters"),
        ("COORD_BOARD_BRAND_TAGLINE", "desk\x1b[31m", "control characters"),
    ),
)
def test_an_unusable_brand_is_refused_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(variable, value)
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    with pytest.raises(ValueError, match=message) as raised:
        make_server(port=0, db_path=str(database), refresh_interval=3600)
    assert variable in str(raised.value)


def test_a_refused_brand_prints_a_sentence_rather_than_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "coord.db"
    demo.seed(database, quiet=True)
    monkeypatch.setenv("COORD_BOARD_BRAND_NAME", "N" * 200)

    # `main` is driven on a thread with a deadline rather than called directly.
    # A build that accepted the name would reach `serve_forever` and never
    # return, and a test that blocked there would report as a stuck suite
    # instead of a failure -- so returning at all is the first assertion.
    outcome: list[int] = []
    runner = threading.Thread(
        target=lambda: outcome.append(
            board_server.main(["--port", "0", "--db", str(database)])
        ),
        daemon=True,
    )
    runner.start()
    runner.join(timeout=15)
    captured = capsys.readouterr()
    assert not runner.is_alive(), "coord-board kept serving with an unusable brand name"
    assert outcome == [2]
    assert "Traceback" not in captured.err
    assert "COORD_BOARD_BRAND_NAME" in captured.err
