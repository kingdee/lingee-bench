"""Error codes and the exception that ends a tool call with an error result.

domain and tools raise :class:`ErpError`; the tools layer is the only place that turns
it into the error object of a tool result (requirement 13.6), whose keys are, in order::

    {"code": ..., "message": ..., "rule_id": ..., "details": [...]}

ErpError is a plain Exception subclass with read-only fields, not a frozen dataclass: a
frozen dataclass also rejects the attributes that Python's exception machinery assigns.
``contextlib.contextmanager`` sets ``__traceback__`` on an exception that leaves its
``with`` block and ``add_note`` sets ``__notes__``, so a frozen dataclass error raised
inside a transaction context manager would turn into a TypeError (Python 3.12).
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final

from erp_mock.common.jsonio import JsonValue

__all__ = ["ErpError", "ErrorCode"]


class ErrorCode(StrEnum):
    """Error code of a tool error result; each value equals the member name.

    A StrEnum member is a str, so it serializes as its value in JSON.
    """

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    CONFLICT = "CONFLICT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_FIELDS: Final = frozenset({"code", "message", "rule_id", "details"})


class ErpError(Exception):
    """An error that ends a tool call; the tools layer reports it as an error result.

    Fields, read-only after construction:

    - ``code``: the error code.
    - ``message``: the message in simplified Chinese (requirement 13.7).
    - ``rule_id``: the rule that decided the error (R-GEN-001 to R-GEN-007 or a pack
      rule), None for errors no rule decides (requirement 13.8).
    - ``details``: detail items in report order. An item about an input field starts
      with ``field``, then ``reason`` and extra keys (design data model 6). Values are
      JSON values already in output form, e.g. decimals formatted as strings.

    The constructor stores ``details`` as a tuple. Exception attributes such as
    ``__traceback__`` and ``__notes__`` stay assignable. ``args`` holds the four fields,
    so copies and pickles rebuild an error with the same fields.
    """

    __slots__ = ("code", "message", "rule_id", "details")

    code: ErrorCode
    message: str
    rule_id: str | None
    details: tuple[Mapping[str, JsonValue], ...]

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        rule_id: str | None = None,
        details: Iterable[Mapping[str, JsonValue]] = (),
    ) -> None:
        items = tuple(details)
        super().__init__(code, message, rule_id, items)
        # __setattr__ rejects the fields, so set them with the generic implementation.
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "details", items)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _FIELDS:
            raise AttributeError(f"cannot assign to read-only field {name!r} of ErpError")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _FIELDS:
            raise AttributeError(f"cannot delete read-only field {name!r} of ErpError")
        super().__delattr__(name)

    def __str__(self) -> str:
        """``"<code>: <message>"``, with the rule ID after the code when there is one."""
        head = self.code if self.rule_id is None else f"{self.code} {self.rule_id}"
        return f"{head}: {self.message}"
