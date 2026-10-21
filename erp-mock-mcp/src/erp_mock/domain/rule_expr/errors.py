"""The error raised for a rule expression the loader rejects (requirement 10.2).

The loader checks each enforced rule's check and each declared query's filter in four
phases: syntax (parse_expr), then names, operator types and the result type (the
checker). Each expression reports one error: the one with the smallest position in the
first phase that finds any, and the loader adds the rule ID or query tool name.
"""

from typing import Literal

__all__ = ["ExprError", "ExprPhase"]

# The checks of requirement 10.2, in the order they run.
ExprPhase = Literal["syntax", "names", "operators", "result"]


class ExprError(ValueError):
    """A rule expression that fails one of the checks of requirement 10.2.

    - ``phase``: the check that failed.
    - ``position``: 1-based code point index into the expression text. A syntax error is
      at the first character of the first token that cannot be accepted, or at
      ``len(text) + 1`` when the text ends early; a names error at the first character of
      the field path or entity name that cannot be resolved; an operators error at the
      first character of the operator or function name whose operand types do not fit;
      a result error at 1.
    - ``reason``: a one-line description, in English.
    """

    phase: ExprPhase
    position: int
    reason: str

    def __init__(self, phase: ExprPhase, position: int, reason: str) -> None:
        # All fields go to args, so the exception pickles and copies.
        super().__init__(phase, position, reason)
        self.phase = phase
        self.position = position
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.phase} error at position {self.position}: {self.reason}"
