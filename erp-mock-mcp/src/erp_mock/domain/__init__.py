"""Business-agnostic engine: primitives, rule expressions and invariant checks.

Type-checked in mypy strict mode. This package must not import tools, gateway,
sqlite3, sqlalchemy, the MCP SDK, starlette or uvicorn, and must not read the
system clock.
"""
