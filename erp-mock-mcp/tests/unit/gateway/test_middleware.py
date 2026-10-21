"""Which requests the two ports refuse before a router sees them, and in which order.

:mod:`erp_mock.gateway.middleware` is four pure ASGI applications, so every test here
speaks HTTP to a chain of them through :class:`httpx.ASGITransport` - no socket, no
uvicorn - and reads the status code, the headers and the body a client would see. That is
the whole observable behaviour of a guard: requirement 23.1 is a 403, requirements 19.2
and 23.3 are a 401, requirement 23.7 is a 413, and requirement 23.11 is which of them a
request with two faults gets.

What sits behind the chain is :class:`Downstream`, not a router: the guards must not know
what they protect, and a recording application says two things a real router could not say
as plainly - whether a refused request reached the thing behind the guard at all
(requirements 23.1, 23.3 and 23.7 each say it must not), and which bytes of the body
arrived there (the buffer of :class:`~erp_mock.gateway.middleware.BodyLimit`, read once or
twice through a ``starlette.requests.Request`` the way the SDK's transport and the Admin
API read it). :class:`Chunks` is the other half of that: a request body that counts how
many of its chunks were pulled, which is how "refused without reading the body" and
"refused while the body was still arriving" are told apart.

The two chains are assembled by :func:`mcp_chain` and :func:`admin_chain` exactly as
design.md 7.4 assembles them, so the order the tests observe is the order the service
runs. No test reads the system clock (requirement 25.10): nothing here is timed.
"""

import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from erp_mock.gateway.middleware import (
    ADMIN_TOKEN_MESSAGE,
    BEARER_PREFIX,
    FORBIDDEN_ORIGIN,
    FORBIDDEN_ORIGIN_MESSAGE,
    MCP_TOKEN_MESSAGE,
    PAYLOAD_TOO_LARGE,
    UNAUTHORIZED,
    AdminTokenGuard,
    BodyLimit,
    McpTokenGuard,
    OriginGuard,
)

# Where the httpx client sends its requests; the ASGI transport opens no socket, so the
# host and the port are only what ``scope["server"]`` and the Host header carry.
BASE_URL: Final = "http://127.0.0.1:8000"

# The one address of the MCP port the tests use, and one of the Admin port: no guard looks
# at a path, which is what :func:`test_the_admin_port_asks_for_the_token_before_the_path`
# is about.
MCP_PATH: Final = "/mcp"
ADMIN_PATH: Final = "/admin/health"

# The two tokens of the tests. Both are nonsense values of the right shape; the point of
# every assertion about them is that neither reaches a response body or a log line
# (requirement 23.6).
MCP_TOKEN: Final = "mcp-tOKEN-9f3c7a1e"
ADMIN_TOKEN: Final = "admin-tOKEN-4b8d02ce"

# The one configured allowed origin of the tests (ERP_MOCK_ALLOWED_ORIGINS,
# requirement 23.9) and two values that are not it: a prefix and an extension of it, which
# requirement 23.1's "character for character" refuses.
ALLOWED_ORIGIN: Final = "https://app.example"
SHORTER_ORIGIN: Final = "https://app.exampl"
LONGER_ORIGIN: Final = "https://app.example.com"

# The body limit every chain of this file runs with: small enough to write a body over it
# in a test, and not a round number, so an off-by-one in the comparison shows.
MAX_BYTES: Final = 64

# What a request that passes every guard is answered with.
DOWNSTREAM_BODY: Final = b"downstream"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- what sits behind the guards ---------------------------------------------------------


@dataclass(slots=True)
class Downstream:
    """A pure ASGI application that records what reached it and reads the body.

    - ``reads``: how often the body is read, each time through a fresh
      ``starlette.requests.Request``. Two is the chain of the service: the
      :class:`~erp_mock.gateway.router.McpRouter` reads the body of a request without a
      session id to see whether it is an ``initialize`` and the transport reads it again
      (requirement 14.8), so the buffer behind :class:`BodyLimit` has to stand being read
      more than once;
    - ``status``: what a request that arrives is answered with. 404 is the Admin API
      answering a path it does not have (requirement 19.15), which
      :func:`test_the_admin_port_asks_for_the_token_before_the_path` puts behind the token
      guard.

    ``served`` is one entry per HTTP request that got here, ``bodies`` one per read and
    ``scopes`` every scope of any type, which is what the non-HTTP pass-through is read
    off.
    """

    reads: int = 1
    status: HTTPStatus = HTTPStatus.OK
    served: list[tuple[str, str]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)
    scopes: list[Scope] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.scopes.append(scope)
        if scope["type"] != "http":
            return
        self.served.append((str(scope["method"]), str(scope["path"])))
        for _ in range(self.reads):
            self.bodies.append(await Request(scope, receive).body())
        response = Response(DOWNSTREAM_BODY, status_code=self.status, media_type="text/plain")
        await response(scope, receive, send)

    @property
    def body(self) -> bytes:
        """The body of the one request that got here, which must have arrived once."""
        assert len(self.bodies) == 1, self.bodies
        return self.bodies[0]


@dataclass(slots=True)
class Chunks:
    """A request body of several chunks that counts how many of them were pulled.

    httpx sends an iterable body with ``Transfer-Encoding: chunked`` and the ASGI
    transport hands one ``http.request`` message per chunk to the application, pulling the
    next one only when the application asks for it. ``pulled`` is therefore how much of
    the body the guard read: 0 for a ``Content-Length`` that was refused without reading
    anything, and fewer than all of them for a body that was refused while it was still
    arriving (requirement 23.7).
    """

    chunks: tuple[bytes, ...]
    pulled: int = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._pull()

    async def _pull(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    @property
    def size(self) -> int:
        """How many bytes the whole body is."""
        return sum(len(chunk) for chunk in self.chunks)


def over_limit_chunks(count: int = 10, chunk: int = 32) -> Chunks:
    """A chunked body of ``count`` chunks of ``chunk`` bytes, well over :data:`MAX_BYTES`.

    The limit is passed inside the third chunk, so a guard that counts as it reads has
    chunks left over and one that read the whole body first does not.
    """
    return Chunks(chunks=tuple(b"x" * chunk for _ in range(count)))


# --- the two chains of design.md 7.4 -----------------------------------------------------


def mcp_chain(
    app: ASGIApp,
    *,
    allowed_origins: Iterable[str] = (ALLOWED_ORIGIN,),
    token: str | None = MCP_TOKEN,
    max_bytes: int = MAX_BYTES,
) -> ASGIApp:
    """The MCP port in front of ``app``: Origin, then token, then body (req 23.11)."""
    return OriginGuard(McpTokenGuard(BodyLimit(app, max_bytes), token), allowed_origins)


def admin_chain(app: ASGIApp, *, token: str = ADMIN_TOKEN, max_bytes: int = MAX_BYTES) -> ASGIApp:
    """The Admin port in front of ``app``: token, then body (requirement 23.11)."""
    return AdminTokenGuard(BodyLimit(app, max_bytes), token)


@asynccontextmanager
async def client_for(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that speaks to ``app`` through the in-process ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    """The ``Authorization`` header that presents ``token``."""
    return {"authorization": f"{BEARER_PREFIX}{token}"}


# --- what a refused request looks like ---------------------------------------------------


def assert_error(response: httpx.Response, status: HTTPStatus, code: str) -> str:
    """Assert ``response`` is the refusal of ``status`` and ``code``, and return its text.

    The shape is design.md 7.7's: a JSON object of ``error`` and ``message`` in that order
    and nothing else, with ``Content-Type: application/json``.
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert list(body) == ["error", "message"]
    assert body["error"] == code
    message = body["message"]
    assert isinstance(message, str)
    assert message
    return message


def assert_passed(response: httpx.Response, downstream: Downstream, path: str) -> None:
    """Assert the request reached ``downstream`` at ``path`` and its answer came back."""
    assert response.status_code == HTTPStatus.OK, response.text
    assert response.content == DOWNSTREAM_BODY
    assert [served for _, served in downstream.served] == [path]


# --- the Origin of an MCP request (requirement 23.1) -------------------------------------


@pytest.mark.anyio
@pytest.mark.req("23.1")
async def test_a_request_without_an_origin_reaches_the_application() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN))
    assert_passed(response, downstream, MCP_PATH)


ALLOWED_ORIGINS: Final = (
    # The three loopback hosts of requirement 23.1, on both schemes and on any port.
    "http://localhost",
    "http://localhost:5173",
    "https://localhost",
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "https://127.0.0.1:443",
    "http://[::1]",
    "http://[::1]:3000",
    # A URL spells its scheme and its host case-insensitively, so this is the loopback.
    "HTTP://LOCALHOST:8000",
    # The common spelling of the loopback origin with an empty path.
    "http://localhost/",
    # Rule 3: the configured allowed origin, character for character.
    ALLOWED_ORIGIN,
)


@pytest.mark.anyio
@pytest.mark.req("23.1")
@pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
async def test_an_allowed_origin_reaches_the_application(origin: str) -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers={**bearer(MCP_TOKEN), "origin": origin})
    assert_passed(response, downstream, MCP_PATH)


REFUSED_ORIGINS: Final = (
    # The two values requirement 23.1 names: an opaque origin and a host that is not
    # loopback.
    "null",
    "http://evil.example",
    # Values that cannot be read as an origin at all.
    "",
    " ",
    "localhost",
    "//localhost",
    "http:/localhost",
    "http://127.0.0.1:abc",
    # A scheme that is neither http nor https.
    "file://localhost",
    "ws://localhost",
    "chrome-extension://abcdefghijklmnop",
    # A loopback host behind something that makes the value a URL rather than an origin.
    "http://localhost/evil",
    "http://localhost?x=1",
    "http://localhost#fragment",
    "http://localhost:8000@evil.example",
    # Hosts that only look like the loopback.
    "http://127.0.0.2",
    "http://127.0.0.1.evil.example",
    "http://localhost.evil.example",
    "http://notlocalhost",
    # The configured allowed origin, not character for character: another case, a prefix
    # of it, an extension of it, and the same host on another scheme or port.
    "HTTPS://APP.EXAMPLE",
    "https://App.Example",
    SHORTER_ORIGIN,
    LONGER_ORIGIN,
    "http://app.example",
    "https://app.example:443",
)


@pytest.mark.anyio
@pytest.mark.req("23.1")
@pytest.mark.parametrize("origin", REFUSED_ORIGINS)
async def test_a_refused_origin_is_answered_with_403(origin: str) -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH, headers={**bearer(MCP_TOKEN), "origin": origin}, content=b"{}"
        )
    message = assert_error(response, HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN)
    # Requirement 23.1: the request is not executed; nothing behind the guard saw it.
    assert downstream.served == []
    assert downstream.bodies == []
    # One message for every refused Origin: the value that was refused is not echoed.
    assert message == FORBIDDEN_ORIGIN_MESSAGE


@pytest.mark.anyio
@pytest.mark.req("23.1")
async def test_without_allowed_origins_only_the_loopback_passes() -> None:
    downstream = Downstream()
    chain = mcp_chain(downstream, allowed_origins=())
    async with client_for(chain) as client:
        refused = await client.post(
            MCP_PATH, headers={**bearer(MCP_TOKEN), "origin": ALLOWED_ORIGIN}
        )
        allowed = await client.post(
            MCP_PATH, headers={**bearer(MCP_TOKEN), "origin": "http://localhost:5173"}
        )
    assert_error(refused, HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN)
    assert_passed(allowed, downstream, MCP_PATH)


# --- the MCP endpoint token (requirement 23.3) -------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("23.3")
@pytest.mark.parametrize("token", [None, ""])
async def test_without_a_configured_token_no_authorization_is_needed(token: str | None) -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream, token=token)) as client:
        response = await client.post(MCP_PATH)
    assert_passed(response, downstream, MCP_PATH)


@pytest.mark.anyio
@pytest.mark.req("23.3")
async def test_the_configured_token_reaches_the_application() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN))
    assert_passed(response, downstream, MCP_PATH)


def wrong_authorizations(token: str) -> tuple[str, ...]:
    """The ``Authorization`` values requirements 19.2 and 23.3 answer with 401."""
    return (
        # No value at all is covered separately; these are values that are not the one.
        "",
        token,
        f"Bearer {token} ",
        f"Bearer  {token}",
        f"Bearer {token}x",
        f"Bearer {token[:-1]}",
        f"Bearer {token.upper()}",
        "Bearer",
        "Bearer ",
        "Bearer other-token",
        # The scheme is part of the value requirement 23.3 compares character for
        # character, so another spelling of it does not match either.
        f"bearer {token}",
        f"BEARER {token}",
        f"Basic {token}",
        f"Token {token}",
    )


@pytest.mark.anyio
@pytest.mark.req("23.3")
@pytest.mark.parametrize("value", wrong_authorizations(MCP_TOKEN))
async def test_a_wrong_mcp_authorization_is_answered_with_401(value: str) -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers={"authorization": value}, content=b"{}")
    assert_error(response, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert downstream.served == []
    assert downstream.bodies == []


@pytest.mark.anyio
@pytest.mark.req("23.3")
async def test_a_missing_mcp_authorization_is_answered_with_401() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH)
    message = assert_error(response, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert message == MCP_TOKEN_MESSAGE
    assert response.headers["www-authenticate"] == "Bearer"
    assert downstream.served == []


# --- the Admin token (requirement 19.2) --------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.2")
async def test_the_admin_token_reaches_the_application() -> None:
    downstream = Downstream()
    async with client_for(admin_chain(downstream)) as client:
        response = await client.get(ADMIN_PATH, headers=bearer(ADMIN_TOKEN))
    assert_passed(response, downstream, ADMIN_PATH)


@pytest.mark.anyio
@pytest.mark.req("19.2")
@pytest.mark.parametrize("value", wrong_authorizations(ADMIN_TOKEN))
async def test_a_wrong_admin_authorization_is_answered_with_401(value: str) -> None:
    downstream = Downstream()
    async with client_for(admin_chain(downstream)) as client:
        response = await client.get(ADMIN_PATH, headers={"authorization": value})
    assert_error(response, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("19.2")
async def test_a_missing_admin_authorization_is_answered_with_401() -> None:
    downstream = Downstream()
    async with client_for(admin_chain(downstream)) as client:
        response = await client.get(ADMIN_PATH)
    message = assert_error(response, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert message == ADMIN_TOKEN_MESSAGE
    assert response.headers["www-authenticate"] == "Bearer"
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("19.2")
async def test_the_admin_port_asks_for_the_token_before_the_path() -> None:
    """Requirement 19.2: a path that does not exist is a 401, not the 404 of 19.15."""
    downstream = Downstream(status=HTTPStatus.NOT_FOUND)
    async with client_for(admin_chain(downstream)) as client:
        without = await client.post("/admin/no-such-thing")
        assert_error(without, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
        assert downstream.served == []
        with_token = await client.post("/admin/no-such-thing", headers=bearer(ADMIN_TOKEN))
    assert with_token.status_code == HTTPStatus.NOT_FOUND
    assert downstream.served == [("POST", "/admin/no-such-thing")]


# --- the size of a body (requirement 23.7) -----------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_body_of_exactly_the_limit_reaches_the_application() -> None:
    downstream = Downstream()
    payload = b"x" * MAX_BYTES
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN), content=payload)
    assert_passed(response, downstream, MCP_PATH)
    assert downstream.body == payload


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_body_one_byte_over_the_limit_is_answered_with_413() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH, headers=bearer(MCP_TOKEN), content=b"x" * (MAX_BYTES + 1)
        )
    message = assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert str(MAX_BYTES) in message
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body() -> None:
    downstream = Downstream()
    body = over_limit_chunks()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={**bearer(MCP_TOKEN), "content-length": str(body.size)},
            content=body,
        )
    assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert body.pulled == 0
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_chunked_body_is_refused_while_it_is_still_arriving() -> None:
    downstream = Downstream()
    body = over_limit_chunks()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN), content=body)
    assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    # Counted as it was read: the limit was passed inside the third chunk and the rest of
    # the body was never pulled.
    assert body.pulled == 3
    assert body.pulled < len(body.chunks)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_content_length_that_is_not_a_number_falls_back_to_counting() -> None:
    downstream = Downstream()
    body = over_limit_chunks()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={**bearer(MCP_TOKEN), "content-length": "not-a-number"},
            content=body,
        )
    assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert body.pulled == 3
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_chunked_body_within_the_limit_arrives_whole() -> None:
    downstream = Downstream()
    body = Chunks(chunks=(b"abc", b"", b"de", b"f" * 20))
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN), content=body)
    assert_passed(response, downstream, MCP_PATH)
    assert downstream.body == b"abcdef" + b"f" * 19
    assert body.pulled == len(body.chunks)


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_the_buffered_body_can_be_read_twice() -> None:
    downstream = Downstream(reads=2)
    payload = b'{"jsonrpc":"2.0"}'
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(MCP_PATH, headers=bearer(MCP_TOKEN), content=payload)
    assert response.status_code == HTTPStatus.OK, response.text
    assert downstream.bodies == [payload, payload]


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_a_request_without_a_body_reaches_the_application() -> None:
    downstream = Downstream()
    async with client_for(admin_chain(downstream)) as client:
        response = await client.delete("/admin/sandboxes/sb_0001", headers=bearer(ADMIN_TOKEN))
    assert response.status_code == HTTPStatus.OK, response.text
    assert downstream.body == b""


@pytest.mark.anyio
@pytest.mark.req("23.7")
async def test_the_admin_port_refuses_a_body_over_the_limit() -> None:
    downstream = Downstream()
    async with client_for(admin_chain(downstream)) as client:
        response = await client.post(
            "/admin/sandboxes", headers=bearer(ADMIN_TOKEN), content=b"x" * (MAX_BYTES + 1)
        )
    assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert downstream.served == []


# --- only the first check that fails is answered (requirement 23.11) ---------------------


@pytest.mark.anyio
@pytest.mark.req("23.11")
async def test_the_mcp_port_answers_the_origin_before_the_token() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={"origin": "http://evil.example", "authorization": "Bearer wrong"},
            content=b"x" * (MAX_BYTES + 1),
        )
    assert_error(response, HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.11")
async def test_the_mcp_port_answers_the_token_before_the_body() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={"origin": "http://localhost:5173", "authorization": "Bearer wrong"},
            content=b"x" * (MAX_BYTES + 1),
        )
    assert_error(response, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.11")
async def test_the_mcp_port_answers_the_body_after_the_origin_and_the_token() -> None:
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={**bearer(MCP_TOKEN), "origin": ALLOWED_ORIGIN},
            content=b"x" * (MAX_BYTES + 1),
        )
    assert_error(response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.11")
async def test_the_admin_port_answers_the_token_before_the_body() -> None:
    downstream = Downstream()
    oversized = b"x" * (MAX_BYTES + 1)
    async with client_for(admin_chain(downstream)) as client:
        without = await client.post("/admin/sandboxes", content=oversized)
        with_token = await client.post(
            "/admin/sandboxes", headers=bearer(ADMIN_TOKEN), content=oversized
        )
    assert_error(without, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert_error(with_token, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, PAYLOAD_TOO_LARGE)
    assert downstream.served == []


@pytest.mark.anyio
@pytest.mark.req("23.1", "23.3", "23.7", "23.11")
async def test_a_request_that_passes_every_check_reaches_the_application() -> None:
    downstream = Downstream()
    payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    async with client_for(mcp_chain(downstream)) as client:
        response = await client.post(
            MCP_PATH,
            headers={**bearer(MCP_TOKEN), "origin": "http://localhost:5173"},
            content=payload,
        )
    assert_passed(response, downstream, MCP_PATH)
    assert downstream.body == payload


# --- what a guard never says (requirements 19.2, 23.3 and 23.6) --------------------------


@pytest.mark.anyio
@pytest.mark.req("19.2", "23.3")
async def test_no_token_reaches_a_response_body_or_a_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="erp_mock.gateway.middleware")
    downstream = Downstream()
    async with client_for(mcp_chain(downstream)) as mcp:
        mcp_401 = await mcp.post(MCP_PATH, headers={"authorization": "Bearer guess"})
        mcp_403 = await mcp.post(MCP_PATH, headers={"origin": "http://evil.example"})
    async with client_for(admin_chain(downstream)) as admin:
        admin_401 = await admin.get(ADMIN_PATH)
    assert_error(mcp_401, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    assert_error(mcp_403, HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN)
    assert_error(admin_401, HTTPStatus.UNAUTHORIZED, UNAUTHORIZED)
    written = "\n".join([mcp_401.text, mcp_403.text, admin_401.text, caplog.text])
    for secret in (MCP_TOKEN, ADMIN_TOKEN):
        assert secret not in written
    # The refusals were noted, so the absence above is not the absence of any log at all.
    assert [record.levelno for record in caplog.records] == [logging.DEBUG] * 3
    assert caplog.text.count(UNAUTHORIZED) == 2
    assert caplog.text.count(FORBIDDEN_ORIGIN) == 1
    # No log line carries a header: the Origin that was refused is not in it either.
    assert "evil.example" not in caplog.text


# --- a scope that is not HTTP ------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.req("19.2", "23.1", "23.3", "23.7")
@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_a_scope_that_is_not_http_passes_through_every_guard(scope_type: str) -> None:
    """The guards check HTTP requests; what else reaches the port is the router's call."""

    async def receive() -> Message:
        raise AssertionError("a guard must not read a scope that is not HTTP")

    async def send(message: Message) -> None:
        raise AssertionError("a guard must not answer a scope that is not HTTP")

    scope: Mapping[str, Any] = {"type": scope_type, "path": MCP_PATH, "headers": []}
    behind_mcp = Downstream()
    behind_admin = Downstream()
    for downstream, app in (
        (behind_mcp, mcp_chain(behind_mcp)),
        (behind_admin, admin_chain(behind_admin)),
    ):
        await app(dict(scope), receive, send)
        assert [str(seen["type"]) for seen in downstream.scopes] == [scope_type]
        assert downstream.served == []
