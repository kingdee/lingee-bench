"""What is refused before a request reaches a router (design.md 7.4).

Both ports of the serve command are chains of pure ASGI applications, and the links in
front of the routers are here. Requirement 23.11 fixes their order, and the assembly of
design.md 7.4 is that order written down::

    mcp_app = OriginGuard(McpTokenGuard(BodyLimit(McpRouter(...), env.max_body_bytes),
                                        env.mcp_token),
                          env.allowed_origins)
    admin_app = AdminTokenGuard(BodyLimit(AdminRouter(...), env.max_body_bytes),
                                state.admin_token)

=====  ======  =================================================  ======  ==============
port   step    what                                               status  requirement
=====  ======  =================================================  ======  ==============
MCP    1       the Origin (:class:`OriginGuard`)                   403    23.1
MCP    2       the endpoint token (:class:`McpTokenGuard`)         401    23.3
MCP    3       the size of the body (:class:`BodyLimit`)           413    23.7
MCP    4 to 7  the address, the session, the concurrency limit      -     14.3, 14.7,
               (:class:`~erp_mock.gateway.router.McpRouter`)              14.8, 15.10
Admin  1       the Admin token (:class:`AdminTokenGuard`)          401    19.2
Admin  2       the size of the body (:class:`BodyLimit`)           413    23.7
Admin  3       the route table (the Admin API of task 23.2)         -     19.15
=====  ======  =================================================  ======  ==============

A guard answers the request itself and does not call the application behind it, so only
the first check that fails is ever answered (requirement 23.11): a request that carries a
forbidden Origin *and* a body of a megabyte gets the 403, and one that carries no Admin
token gets the 401 whether or not its path exists (requirement 19.2). Nothing a guard
refuses reaches a session, a sandbox or a trace file (requirements 23.1, 23.3, 23.7 and
17.9), which is structural rather than remembered: the request simply stops here.

Why these are plain ASGI classes rather than Starlette middlewares. ``BaseHTTPMiddleware``
turns the response of the application into a stream of its own, which the SDK's transport
cannot be put behind (design.md 7.4), and the guards need no ``Request`` object either:
two of them read one header, one counts bytes. Each one does a single thing and holds a
single value, so they compose in any order and are testable one at a time.

What a refused request is told. One shape for both ports, the ``{"error": <short code>,
"message": <Chinese text>}`` object of design.md 7.7, and not the JSON-RPC envelope
:class:`~erp_mock.gateway.router.McpRouter` writes:

- a request refused here is not known to be an MCP message. No path has been matched, no
  session has been looked up and - for the 403 and the 401 - no body has been read, so
  there is no request id to answer and no reason to claim the client sent JSON-RPC. The
  envelope the router writes exists to match the status codes the SDK's transport writes
  *inside* a session; these three refusals are outside every session;
- :class:`BodyLimit` serves both ports, and one shape keeps it from having to be told
  which port it is on;
- requirement 25.6 asks the Admin API tests for a 401 and a 413 with a JSON body, which is
  this shape.

The three short codes and the three messages are module constants, so the status code, the
code and the text of one refusal are one decision. None of them carries a token, a header
value or an exception text (design.md error handling): the 403 names neither the Origin it
refused nor the allowed origins, the two 401s name the scheme they want and nothing else,
and the 413 names the configured limit, which is public configuration (requirement 23.9).
The guards keep their expected ``Authorization`` value in a slot of their own and are not
dataclasses, so no ``repr`` of them can put a token into a log line
(requirement 23.6). The one log line a refusal writes carries the method, the path and the
short code, never a header (design.md error handling).

Non-HTTP scopes pass straight through. A WebSocket upgrade and - were the lifespan
protocol ever turned on - a lifespan message are handed to the application behind the
guard, which leaves the decision with the innermost one:
:class:`~erp_mock.gateway.router.McpRouter` closes a WebSocket and ignores everything
else, and the Admin API does what it does. Requirements 23.1, 23.3, 23.7 and 19.2 all
speak of HTTP requests, so a guard that answered a WebSocket would be inventing a rule.

Headers are read as the raw bytes of ``scope["headers"]`` rather than through
``starlette.datastructures.Headers``: requirements 19.2 and 23.3 ask for a comparison that
is character for character and case sensitive, and :func:`hmac.compare_digest` wants bytes
anyway - it refuses a ``str`` that is not ASCII, which a header value may well be. ASGI
delivers header names lower-cased, which is what :func:`_first_header` matches against.
"""

import hmac
import logging
from collections.abc import Iterable, Mapping
from http import HTTPStatus
from typing import Final
from urllib.parse import urlsplit

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from erp_mock.common.jsonio import dumps_compact, encode_utf8

__all__ = [
    "ADMIN_TOKEN_MESSAGE",
    "BEARER_PREFIX",
    "BODY_TOO_LARGE_MESSAGE",
    "FORBIDDEN_ORIGIN",
    "FORBIDDEN_ORIGIN_MESSAGE",
    "LOOPBACK_HOSTS",
    "MCP_TOKEN_MESSAGE",
    "ORIGIN_SCHEMES",
    "PAYLOAD_TOO_LARGE",
    "UNAUTHORIZED",
    "AdminTokenGuard",
    "BodyLimit",
    "McpTokenGuard",
    "OriginGuard",
    "error_response",
]

# Where a refusal is noted: the service log on stderr, at debug level. A refused request
# is answered, so nothing here needs a deployer's attention; what the line is for is the
# deployer who wants to know why a client is being turned away.
_LOG: Final = logging.getLogger(__name__)
_REFUSED: Final = "%s %s 被拒绝：%s（HTTP %d）"

# The headers the guards read, lower-cased as ASGI delivers header names.
_ORIGIN_HEADER: Final = b"origin"
_AUTHORIZATION_HEADER: Final = b"authorization"
_CONTENT_LENGTH_HEADER: Final = b"content-length"

# The one authentication scheme both ports use (requirements 19.2 and 23.3): the whole
# header value must be this prefix followed by the token, character for character.
BEARER_PREFIX: Final = "Bearer "

# RFC 9110 asks every 401 to say which scheme it wants; the token value is not named.
_WWW_AUTHENTICATE: Final[Mapping[str, str]] = {"WWW-Authenticate": "Bearer"}

# The two schemes an Origin may carry to be judged by its host (requirement 23.1).
ORIGIN_SCHEMES: Final = frozenset({"http", "https"})

# The three hosts requirement 23.1 lets through on any port. ``[::1]`` is how the
# requirement spells the IPv6 loopback in a URL; ``urlsplit().hostname`` hands it over
# without the brackets, which is what this set holds. Only these three: 127.0.0.2 and the
# rest of 127.0.0.0/8 are loopback addresses for ``--host`` (requirement 23.2) but not
# Origins requirement 23.1 lets through.
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

# The short codes of the three refusals (design.md 7.7).
FORBIDDEN_ORIGIN: Final = "forbidden_origin"
UNAUTHORIZED: Final = "unauthorized"
PAYLOAD_TOO_LARGE: Final = "payload_too_large"

# What each refusal says. The 403 does not repeat the Origin it refused and names no
# allowed origin: the client already knows what it sent, and an error message of this
# service carries neither a header value nor a configured secret-adjacent value
# (design.md error handling).
FORBIDDEN_ORIGIN_MESSAGE: Final = "请求的 Origin 不是允许的来源"
MCP_TOKEN_MESSAGE: Final = "MCP 端点要求 Authorization 头携带 Bearer 令牌"
ADMIN_TOKEN_MESSAGE: Final = "Admin 接口要求 Authorization 头携带 Bearer 令牌"
BODY_TOO_LARGE_MESSAGE: Final = "请求体超过上限 {max_bytes} 字节"

# The media type of every body written here.
_JSON_MEDIA_TYPE: Final = "application/json"

# The ASGI message type of a body chunk, and the one the buffered body is replayed as.
_HTTP_REQUEST: Final = "http.request"
_HTTP: Final = "http"


def error_response(
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """One refusal in the shape of design.md 7.7: ``{"error": ..., "message": ...}``.

    ``code`` is the short machine-readable code a caller branches on and ``message`` the
    Chinese text a person reads; the two keys are written in that order, and
    :func:`~erp_mock.common.jsonio.dumps_compact` keeps it, so the same refusal is the
    same bytes every time (requirement 24.1). The Admin API of tasks 23.2 and 23.3 writes
    its errors in this shape as well.
    """
    body: Mapping[str, str] = {"error": code, "message": message}
    return Response(
        content=encode_utf8(dumps_compact(body)),
        status_code=status,
        media_type=_JSON_MEDIA_TYPE,
        headers=headers,
    )


def _first_header(scope: Scope, name: bytes) -> bytes | None:
    """The first value of the header ``name`` of ``scope``, or None where it has none.

    ``name`` is lower case, which is how ASGI delivers header names, and the value is
    returned as the bytes that arrived: the token comparisons of requirements 19.2 and
    23.3 are byte comparisons, and an Origin is parsed from its own bytes
    (module docstring). A request that repeats a header is answered from its first value,
    as ``starlette.datastructures.Headers`` would.
    """
    for key, value in scope["headers"]:
        if key == name:
            return bytes(value)
    return None


def _refuse(scope: Scope, response: Response, code: str) -> Response:
    """Note the refusal ``code`` of the request ``scope`` in the log and return it.

    The line carries the method, the path and the short code and no header at all
    (design.md error handling), so neither a token nor an Origin can reach the log
    (requirement 23.6).
    """
    _LOG.debug(
        _REFUSED,
        scope.get("method", ""),
        scope.get("path", ""),
        code,
        response.status_code,
    )
    return response


# --- the Origin of an MCP request (requirement 23.1) -------------------------------------


class OriginGuard:
    """Step 1 of the MCP port: which Origins may reach it (requirement 23.1).

    - ``app``: the application a request that passes is handed to, which is
      :class:`McpTokenGuard` in the service (design.md 7.4);
    - ``allowed_origins``: the origins of ``ERP_MOCK_ALLOWED_ORIGINS``, in any order and
      empty by default (requirement 23.9); they are matched literally, so each one is kept
      as the bytes of its own text.

    The rules of requirement 23.1, in the order they are tried; the first that applies
    decides, and a request that matches none is answered with 403:

    ==  ====================================================  =========  ===============
    #   the Origin header                                     outcome    example
    ==  ====================================================  =========  ===============
    1   absent                                                allowed    a CLI client
    2   scheme http or https, host ``localhost``,             allowed    ``http://127.0.0.1:5173``
        ``127.0.0.1`` or ``[::1]``, any port or none
    3   the same bytes as one of ``allowed_origins``          allowed    ``https://app.example``
    4   anything else, ``null`` and anything unparsable       403        ``http://evil.example``
        included
    ==  ====================================================  =========  ===============

    Rule 2 and rule 3 are independent, and they compare differently on purpose. Rule 2
    reads the value as a URL, where the scheme and the host are case insensitive, so
    ``HTTP://LOCALHOST:8000`` is the loopback; rule 3 is requirement 23.1's "character for
    character", so an allowed origin written in another case, a prefix of one
    (``https://app.example`` against ``https://app.example.com``) and an extension of one
    (the reverse) are all refused. A loopback Origin therefore needs no configuration,
    and a configured origin needs no guessing about what counts as the same origin.

    What counts as parsable (rule 4): a scheme, a host and at most a port, which is the
    serialization of an origin. The empty value, ``null``, a bare host, a value that is
    not ASCII and a port that is not a number are all refused; so is a value that carries
    userinfo, a query or a fragment, or a path other than ``/`` - ``http://localhost/`` is
    the common spelling of the loopback origin and names the same origin, while
    ``http://localhost/x`` is a URL and not an origin.
    """

    __slots__ = ("_allowed", "_app")

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str] = ()) -> None:
        self._app = app
        self._allowed = frozenset(origin.encode("utf-8") for origin in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hand the request to ``app``, or answer 403 (requirement 23.1)."""
        if scope["type"] != _HTTP:
            await self._app(scope, receive, send)
            return
        origin = _first_header(scope, _ORIGIN_HEADER)
        if origin is None or self._allows(origin):
            await self._app(scope, receive, send)
            return
        refusal = _refuse(
            scope,
            error_response(HTTPStatus.FORBIDDEN, FORBIDDEN_ORIGIN, FORBIDDEN_ORIGIN_MESSAGE),
            FORBIDDEN_ORIGIN,
        )
        await refusal(scope, receive, send)

    def _allows(self, origin: bytes) -> bool:
        """Whether ``origin`` passes rule 2 or rule 3 of the table above."""
        return _is_loopback_origin(origin) or origin in self._allowed


def _is_loopback_origin(origin: bytes) -> bool:
    """Whether ``origin`` is an http or https origin of a loopback host (rule 2).

    False for everything that is not one, a value that cannot be read as an origin
    included, so the caller needs no separate "unparsable" case: such a value falls
    through to rule 3, where it does not match either unless it was configured literally.
    """
    try:
        text = origin.decode("ascii")
    except UnicodeDecodeError:
        return False
    parts = urlsplit(text)
    if parts.scheme not in ORIGIN_SCHEMES:
        return False
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return False
    try:
        # Reading .port is what rejects a port that is not a number.
        _ = parts.port
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        # Userinfo: in http://localhost:8000@evil.example the host is evil.example, and a
        # value that carries userinfo is no origin whatever its host.
        return False
    return parts.hostname in LOOPBACK_HOSTS


# --- the tokens of the two ports (requirements 23.3 and 19.2) ----------------------------


class McpTokenGuard:
    """Step 2 of the MCP port: the endpoint token, where one is configured (req 23.3).

    - ``app``: the application a request that passes is handed to, which is
      :class:`BodyLimit` in the service;
    - ``token``: the value of ``ERP_MOCK_MCP_TOKEN``, None - or the empty string - where
      the variable was unset or empty, which requirement 23.3 reads as "no endpoint token
      configured".

    With no token configured the guard is transparent: the MCP port is reachable without
    an ``Authorization`` header, which is what the loopback default of requirement 22.1
    needs, and requirement 23.2 is what keeps that default off a non-loopback ``--host``
    (the CLI refuses to start, task 24.3). With a token configured every request of the
    port must carry ``Authorization: Bearer <token>``, the whole value identical to that
    string, or it is answered with 401 and reaches no session and no sandbox.

    The comparison is :func:`hmac.compare_digest` over the bytes of the header and the
    bytes of the expected value, never ``==``: a token is a secret, and a comparison that
    stops at the first differing byte tells a client how much of its guess was right. The
    scheme is compared as part of the value, so ``bearer <token>`` is refused as well -
    requirement 23.3's "character for character, case sensitive", which is stricter than
    RFC 9110, where a scheme name is case insensitive.
    """

    __slots__ = ("_app", "_expected")

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self._app = app
        self._expected = None if not token else _bearer(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hand the request to ``app``, or answer 401 (requirement 23.3)."""
        expected = self._expected
        if expected is None or scope["type"] != _HTTP:
            await self._app(scope, receive, send)
            return
        if _bearer_matches(_first_header(scope, _AUTHORIZATION_HEADER), expected):
            await self._app(scope, receive, send)
            return
        await _unauthorized(scope, MCP_TOKEN_MESSAGE)(scope, receive, send)


class AdminTokenGuard:
    """Step 1 of the Admin port: the Admin token, always (requirement 19.2).

    - ``app``: the application a request that passes is handed to, which is
      :class:`BodyLimit` in the service;
    - ``token``: the Admin token of this process, either ``ERP_MOCK_ADMIN_TOKEN`` or the
      one the CLI generated and wrote to the token file (requirements 23.4 and 23.5).

    The difference from :class:`McpTokenGuard` is the whole reason there are two classes:
    this one has no transparent case. Requirement 19.2 asks for the token on every request
    of the port, paths that do not exist included, so a client without it cannot even learn
    which Admin paths there are - the 401 comes before the route table of task 23.2, which
    is where requirement 19.15's 404 lives. The comparison is the same
    :func:`hmac.compare_digest` over the whole ``Authorization`` value.
    """

    __slots__ = ("_app", "_expected")

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = _bearer(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hand the request to ``app``, or answer 401 (requirement 19.2)."""
        if scope["type"] != _HTTP:
            await self._app(scope, receive, send)
            return
        if _bearer_matches(_first_header(scope, _AUTHORIZATION_HEADER), self._expected):
            await self._app(scope, receive, send)
            return
        await _unauthorized(scope, ADMIN_TOKEN_MESSAGE)(scope, receive, send)


def _bearer(token: str) -> bytes:
    """The ``Authorization`` value a request must carry to present ``token``."""
    return f"{BEARER_PREFIX}{token}".encode()


def _bearer_matches(presented: bytes | None, expected: bytes) -> bool:
    """Whether the ``Authorization`` value ``presented`` is ``expected``.

    False for a request that carries no such header at all, which requirement 23.3 answers
    exactly like one whose value does not match.
    """
    return presented is not None and hmac.compare_digest(presented, expected)


def _unauthorized(scope: Scope, message: str) -> Response:
    """The 401 of requirements 19.2 and 23.3: ``message`` and the scheme, no token."""
    return _refuse(
        scope,
        error_response(HTTPStatus.UNAUTHORIZED, UNAUTHORIZED, message, headers=_WWW_AUTHENTICATE),
        UNAUTHORIZED,
    )


# --- the size of a body, on both ports (requirement 23.7) --------------------------------


class BodyLimit:
    """The body size step of both ports, and the buffer behind it (requirement 23.7).

    - ``app``: the application a request that passes is handed to, which is the
      :class:`~erp_mock.gateway.router.McpRouter` on the MCP port and the Admin API on the
      Admin port;
    - ``max_bytes``: ``ERP_MOCK_MAX_BODY_BYTES``, 262144 by default (requirement 23.9). A
      body of exactly this many bytes passes; requirement 23.7 refuses the one that
      *exceeds* the limit.

    Two ways a body is found to be too large, and requirement 23.7 asks for the cheap one
    first:

    1. a ``Content-Length`` that is larger than ``max_bytes``: answered with 413 at once,
      without reading a single byte of the body, so a client that announces a gigabyte is
      not read for a gigabyte. A ``Content-Length`` that is not a number is ignored and
      the body counted instead - an ASGI server does not deliver such a request, and
      guessing what it meant is not this guard's business;
    2. no usable ``Content-Length``, or one within the limit: the body is read chunk by
      chunk and counted as it is read, and the 413 is written as soon as the running total
      passes ``max_bytes``, with the rest of the body left unread. A chunked upload is
      therefore refused while it is still arriving.

    Neither case reaches the application behind the guard, so no session, no sandbox and no
    trace file is touched by a request that is too large (requirement 23.7).

    The buffer and how it is replayed. A body that passes is read to its end here, which
    an ASGI application cannot undo, so the application behind the guard is given a
    ``receive`` of this module's making instead of the server's. Its protocol:

    - every call returns one ``{"type": "http.request", "body": <the whole body>,
      "more_body": False}`` message, so a reader sees the body complete in one message and
      stops at the first one;
    - every call returns it again. The buffer is replayable any number of times, which is
      what a chain that reads the body more than once needs:
      :class:`~erp_mock.gateway.router.McpRouter` reads it to see whether a request without
      a session id is an ``initialize`` (requirement 14.8) and then builds its own
      replaying ``receive`` for the transport, while an in-session POST is handed the
      ``receive`` of this module and reads it through a ``starlette.requests.Request`` of
      the transport's own;
    - no call ever returns ``http.disconnect``. The whole request has arrived by the time
      the application runs, so ``Request.is_disconnected()`` reports a client that is still
      there, and a reader can never be handed a ``ClientDisconnect`` for a body that is
      already in memory.

    A client that hangs up in the middle of its body ends the reading where it stopped, as
    ``McpRouter``'s own reader does: the partial bytes are what the application sees, which
    is no ``initialize`` and no valid Admin body, so the request is refused for what it is.
    """

    __slots__ = ("_app", "_max_bytes")

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Hand the request and its buffered body to ``app``, or answer 413 (req 23.7)."""
        if scope["type"] != _HTTP:
            await self._app(scope, receive, send)
            return
        if self._declared_too_large(scope):
            await self._too_large(scope)(scope, receive, send)
            return
        body = await self._read(receive)
        if body is None:
            await self._too_large(scope)(scope, receive, send)
            return
        await self._app(scope, _buffered(body), send)

    def _declared_too_large(self, scope: Scope) -> bool:
        """Whether ``Content-Length`` announces more than ``max_bytes`` (case 1)."""
        declared = _first_header(scope, _CONTENT_LENGTH_HEADER)
        if declared is None:
            return False
        try:
            length = int(declared)
        except ValueError:
            return False
        return length > self._max_bytes

    async def _read(self, receive: Receive) -> bytes | None:
        """The whole body, or None as soon as it passes ``max_bytes`` (case 2)."""
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != _HTTP_REQUEST:
                break
            chunk = bytes(message.get("body", b""))
            total += len(chunk)
            if total > self._max_bytes:
                return None
            chunks.append(chunk)
            more = bool(message.get("more_body", False))
        return b"".join(chunks)

    def _too_large(self, scope: Scope) -> Response:
        """The 413 of requirement 23.7, naming the limit that was passed."""
        return _refuse(
            scope,
            error_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                PAYLOAD_TOO_LARGE,
                BODY_TOO_LARGE_MESSAGE.format(max_bytes=self._max_bytes),
            ),
            PAYLOAD_TOO_LARGE,
        )


def _buffered(body: bytes) -> Receive:
    """An ASGI ``receive`` that delivers ``body`` whole, on this and on every later call.

    The replay protocol of :class:`BodyLimit`, written where the one reader of it is.
    """

    async def receive() -> Message:
        return {"type": _HTTP_REQUEST, "body": body, "more_body": False}

    return receive
