"""Rule_Expression_Parser: from the text of a check or filter to a syntax tree (req 10.1).

Grammar; any tokens may be separated by whitespace, which does not change the result:

    expr    := or
    or      := and ("or" and)*
    and     := not ("and" not)*
    not     := "not" not | cmp
    cmp     := add [("==" | "!=" | "<" | "<=" | ">" | ">=") add | "in" list]
    add     := mul (("+" | "-") mul)*
    mul     := atom (("*" | "/") atom)*
    atom    := NUMBER | STRING | path | call | "(" expr ")"
    path    := ("header" | "line") "." NAME ("." NAME)*
             | "context" "." ("today" | "period" | "user" "." NAME ("." NAME)*)
             | NAME ("." NAME)*                    # unprefixed: NAME is not reserved
    call    := "sum" "(" "lines" "." NAME ")" | "count" "(" "lines" ")"
             | "exists" "(" NAME "," expr ")" | "sum_of" "(" NAME "." NAME "," expr ")"
    list    := "[" literal ("," literal)* "]"
    literal := NUMBER | STRING

Binary logical and arithmetic operators are left-associative. A comparison operand is an
``add``, so comparisons do not chain (``a < b < c`` is an error) and ``not`` applies to
a whole comparison (``not a == b`` is ``not (a == b)``). There is no unary minus
(``-1`` is an error; write ``0 - 1``) and no boolean literal. The parser accepts an
unprefixed path anywhere; that one stands only in an exists or sum_of condition is
checked with the other names (requirement 10.2).

Tokens are read left to right, each as long as possible:

- word: ``[a-z][a-z0-9_]*``, ASCII lowercase letters, digits and ``_``, the name rule of
  the configuration. Keywords, function names and path prefixes are words too; nothing is
  case-folded, so ``AND``, ``Header`` and ``SUM`` are errors at their first upper-case
  letter. A word is a keyword only as a whole word: ``order_no`` and ``notes`` are names.
- number: ``[0-9]+`` optionally followed by ``.`` and ``[0-9]+``, unsigned; ``1.`` is the
  number ``1`` followed by ``.``.
- string: ``'`` up to the next ``'`` that is not doubled; ``''`` inside stands for one
  quote. Any other character, a line break included, stands for itself.
- operator: ``==`` ``!=`` ``<=`` ``>=`` ``<`` ``>`` ``+`` ``-`` ``*`` ``/`` ``(`` ``)``
  ``[`` ``]`` ``,`` ``.``.
- whitespace between tokens: space, tab, LF and CR only.

Any other character (an upper-case letter, a lone ``=`` or ``!``, ``"``, a non-ASCII
character outside a string, other white space such as U+3000) cannot start a token.

Words, keywords and names (nodes.RESERVED_WORDS):

- The keywords ``and``, ``or``, ``not``, ``in``, the function names ``sum``, ``count``,
  ``exists``, ``sum_of`` and the path prefixes ``header``, ``line``, ``lines``,
  ``context`` are reserved: where an operand starts, such a word has its grammar meaning
  and never starts an unprefixed path. ``line`` and ``lines`` are different words:
  ``line.F`` is a field of the current line, while ``lines`` appears only in
  ``sum(lines.F)`` and ``count(lines)``, so ``lines.qty`` is an error at ``lines`` and
  ``sum(line.qty)`` an error at ``line``.
- Where only a name can stand (after ``.``, the target of exists and sum_of, the field
  of sum and sum_of) every word is a name: ``header.count`` is the header field count.
- After ``context.`` only ``today``, ``period`` and ``user`` are accepted, and
  ``context.user`` must be followed by ``.`` and a name.

Syntax errors raise ExprError with phase "syntax", positioned as requirement 10.2
defines: at the first character of the first token that the grammar does not accept
where it appears, or at ``len(text) + 1`` when the text ends early. Where a string
literal can stand, one without a closing quote makes the text end early (``len(text) +
1``); elsewhere it is the unacceptable token. A comparison followed by another
comparison operator or ``in`` is reported at that operator.

Expressions are at most MAX_DEPTH levels deep: parentheses, the parentheses of exists
and sum_of and ``not`` nest at most MAX_DEPTH levels, and a node of the syntax tree has
at most MAX_DEPTH ancestors. A deeper expression is a syntax error at the parenthesis,
function name, ``not`` or operator that exceeds the limit. This keeps the parser, the
printer and the other recursive consumers of the tree within Python's recursion limit.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

from erp_mock.domain.rule_expr.errors import ExprError
from erp_mock.domain.rule_expr.nodes import (
    RESERVED_WORDS,
    Binary,
    BinaryOp,
    Count,
    CtxPeriod,
    CtxToday,
    Exists,
    Expr,
    In,
    ListLit,
    Not,
    Num,
    Path,
    PathRoot,
    Str,
    Sum,
    SumOf,
)

__all__ = ["MAX_DEPTH", "parse_expr"]

MAX_DEPTH: Final = 64

# --- tokens ------------------------------------------------------------------------------

_TokenKind = Literal["word", "number", "string", "op", "bad", "end"]

_WHITESPACE: Final = frozenset(" \t\n\r")
_DIGITS: Final = frozenset("0123456789")
_LETTERS: Final = frozenset("abcdefghijklmnopqrstuvwxyz")
_WORD_CHARS: Final = _LETTERS | _DIGITS | {"_"}
_UPPER_CASE: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
# Two-character operators first, so that "<=" is not read as "<" and "=".
_OPERATORS: Final = ("==", "!=", "<=", ">=", *"<>+-*/()[],.")


@dataclass(frozen=True, slots=True)
class _Token:
    """A token: ``start`` is the 0-based code point index of its first character.

    ``text`` is the word, number or operator as written, the value of a string literal
    (quotes removed, doubled quotes undone), the character of a bad token and "" at the
    end. ``closed`` is False for a string literal that the text ends inside.
    """

    kind: _TokenKind
    text: str
    start: int
    closed: bool = True

    @property
    def position(self) -> int:
        """1-based code point index; ``len(text) + 1`` for the end token."""
        return self.start + 1

    def is_op(self, op: str) -> bool:
        return self.kind == "op" and self.text == op

    def is_word(self, word: str) -> bool:
        return self.kind == "word" and self.text == word


def _tokenize(text: str) -> list[_Token]:
    """Split ``text`` into tokens, ending with an end token.

    Tokenizing stops at a bad character: the parser cannot accept it, so what follows does
    not matter. A string literal without a closing quote runs to the end of the text.
    """
    tokens: list[_Token] = []
    length = len(text)
    index = 0
    while True:
        while index < length and text[index] in _WHITESPACE:
            index += 1
        if index == length:
            tokens.append(_Token("end", "", length))
            return tokens
        char = text[index]
        if char in _LETTERS:
            end = index + 1
            while end < length and text[end] in _WORD_CHARS:
                end += 1
            token = _Token("word", text[index:end], index)
        elif char in _DIGITS:
            end = _number_end(text, index)
            token = _Token("number", text[index:end], index)
        elif char == "'":
            token, end = _read_string(text, index)
        else:
            op = next((each for each in _OPERATORS if text.startswith(each, index)), None)
            if op is None:
                tokens.append(_Token("bad", char, index))
                tokens.append(_Token("end", "", length))
                return tokens
            token, end = _Token("op", op, index), index + len(op)
        tokens.append(token)
        index = end


def _number_end(text: str, start: int) -> int:
    """End of the number starting at ``start``: digits, then ``.`` and digits if present."""
    end = start
    while end < len(text) and text[end] in _DIGITS:
        end += 1
    if end + 1 < len(text) and text[end] == "." and text[end + 1] in _DIGITS:
        end += 1
        while end < len(text) and text[end] in _DIGITS:
            end += 1
    return end


def _read_string(text: str, start: int) -> tuple[_Token, int]:
    """Read the string literal whose opening quote is at ``start``; return it and its end."""
    parts: list[str] = []
    index = start + 1
    while True:
        quote = text.find("'", index)
        if quote < 0:
            parts.append(text[index:])
            return _Token("string", "".join(parts), start, closed=False), len(text)
        parts.append(text[index:quote])
        if not text.startswith("'", quote + 1):
            return _Token("string", "".join(parts), start), quote + 1
        parts.append("'")
        index = quote + 2


def _describe(token: _Token) -> str:
    match token.kind:
        case "word" | "op":
            return f"'{token.text}'"
        case "number":
            return f"number {token.text}"
        case "string":
            return "string literal"
        case "bad":
            return f"character {token.text!r}"
        case "end":
            return "end of text"


# --- parser ------------------------------------------------------------------------------


def _binary_ops(*ops: BinaryOp) -> Mapping[str, BinaryOp]:
    """Map the token text of each operator in ``ops`` to the operator."""
    return MappingProxyType({op: op for op in ops})


_COMPARISON_OPS: Final = _binary_ops("==", "!=", "<", "<=", ">", ">=")
_ADDITIVE_OPS: Final = _binary_ops("+", "-")
_MULTIPLICATIVE_OPS: Final = _binary_ops("*", "/")


def _is_comparison(token: _Token) -> bool:
    return (token.kind == "op" and token.text in _COMPARISON_OPS) or token.is_word("in")


def parse_expr(text: str) -> Expr:
    """Parse ``text`` as a rule expression and return its syntax tree.

    Node positions are 1-based code point indices into ``text``. Raises ExprError with
    phase "syntax" when ``text`` does not follow the grammar of the module docstring or
    is more than MAX_DEPTH levels deep; it raises no other exception for any str.
    """
    expr = _Parser(text).parse()
    _check_height(expr)
    return expr


class _Parser:
    """Recursive descent over the tokens of one text; one method per grammar rule."""

    __slots__ = ("_depth", "_index", "_text", "_tokens")

    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = _tokenize(text)
        self._index = 0
        self._depth = 0  # open parentheses, exists and sum_of calls and not operands

    def parse(self) -> Expr:
        expr = self._or()
        token = self._peek()
        if token.kind != "end":
            raise self._unexpected(token)
        return expr

    # --- token access --------------------------------------------------------------------

    def _peek(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _accept_word(self, word: str) -> _Token | None:
        if self._peek().is_word(word):
            return self._advance()
        return None

    def _accept_op(self, op: str) -> _Token | None:
        if self._peek().is_op(op):
            return self._advance()
        return None

    def _accept_binary(self, ops: Mapping[str, BinaryOp]) -> tuple[BinaryOp, _Token] | None:
        token = self._peek()
        if token.kind == "op" and token.text in ops:
            return ops[token.text], self._advance()
        return None

    def _expect_op(self, op: str, expected: str | None = None) -> _Token:
        token = self._accept_op(op)
        if token is None:
            raise self._unexpected(self._peek(), expected or f"'{op}'")
        return token

    def _expect_word(self, word: str) -> _Token:
        token = self._accept_word(word)
        if token is None:
            raise self._unexpected(self._peek(), f"'{word}'")
        return token

    def _expect_name(self) -> _Token:
        """Consume a word of any kind, read as a name."""
        if self._peek().kind != "word":
            raise self._unexpected(self._peek(), "a name")
        return self._advance()

    def _unexpected(self, token: _Token, expected: str | None = None) -> ExprError:
        reason = f"unexpected {_describe(token)}"
        if expected is not None:
            reason += f", expected {expected}"
        if token.kind == "bad" and token.text in _UPPER_CASE:
            reason += " (keywords, function names, path prefixes and names are lower case)"
        return ExprError("syntax", token.position, reason)

    def _enter(self, opener: _Token) -> None:
        """Open a nesting level at ``opener``; the caller closes it with ``_depth -= 1``."""
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise ExprError("syntax", opener.position, _too_deep())

    # --- grammar rules -------------------------------------------------------------------

    def _or(self) -> Expr:
        left = self._and()
        while (token := self._accept_word("or")) is not None:
            left = Binary("or", left, self._and(), pos=token.position)
        return left

    def _and(self) -> Expr:
        left = self._not()
        while (token := self._accept_word("and")) is not None:
            left = Binary("and", left, self._not(), pos=token.position)
        return left

    def _not(self) -> Expr:
        token = self._accept_word("not")
        if token is None:
            return self._comparison()
        self._enter(token)
        operand = self._not()
        self._depth -= 1
        return Not(operand, pos=token.position)

    def _comparison(self) -> Expr:
        left = self._additive()
        expr: Expr
        if (found := self._accept_binary(_COMPARISON_OPS)) is not None:
            op, token = found
            expr = Binary(op, left, self._additive(), pos=token.position)
        elif (in_token := self._accept_word("in")) is not None:
            expr = In(left, self._list(), pos=in_token.position)
        else:
            return left
        following = self._peek()
        if _is_comparison(following):
            raise ExprError("syntax", following.position, "comparisons cannot be chained")
        return expr

    def _additive(self) -> Expr:
        left = self._multiplicative()
        while (found := self._accept_binary(_ADDITIVE_OPS)) is not None:
            op, token = found
            left = Binary(op, left, self._multiplicative(), pos=token.position)
        return left

    def _multiplicative(self) -> Expr:
        left = self._atom()
        while (found := self._accept_binary(_MULTIPLICATIVE_OPS)) is not None:
            op, token = found
            left = Binary(op, left, self._atom(), pos=token.position)
        return left

    def _atom(self) -> Expr:
        token = self._peek()
        match token.kind:
            case "number":
                return self._number()
            case "string":
                return self._string()
            case "word":
                return self._word()
            case "op" if token.text == "(":
                self._advance()
                expr = self._nested(token)
                self._expect_op(")")
                return expr
        raise self._unexpected(token)

    def _nested(self, opener: _Token) -> Expr:
        """Parse an ``expr`` one nesting level deeper, inside parentheses opened at ``opener``."""
        self._enter(opener)
        expr = self._or()
        self._depth -= 1
        return expr

    def _number(self) -> Num:
        token = self._advance()
        # Decimal reads the digits exactly, whatever their number.
        return Num(Decimal(token.text), pos=token.position)

    def _string(self) -> Str:
        token = self._advance()
        if not token.closed:
            # A literal can stand here, but the text ends before its closing quote.
            raise ExprError("syntax", len(self._text) + 1, "unterminated string literal")
        return Str(token.text, pos=token.position)

    def _literal(self) -> Num | Str:
        match self._peek().kind:
            case "number":
                return self._number()
            case "string":
                return self._string()
        raise self._unexpected(self._peek(), "a number or string literal")

    def _list(self) -> ListLit:
        bracket = self._expect_op("[")
        items = [self._literal()]
        while self._accept_op(",") is not None:
            items.append(self._literal())
        self._expect_op("]", "',' or ']'")
        return ListLit(tuple(items), pos=bracket.position)

    def _names(self) -> tuple[str, ...]:
        """``NAME ("." NAME)*``."""
        return self._names_after(self._expect_name().text)

    def _names_after(self, first: str) -> tuple[str, ...]:
        """``("." NAME)*`` after the name ``first``; return ``first`` and the names read."""
        names = [first]
        while self._accept_op(".") is not None:
            names.append(self._expect_name().text)
        return tuple(names)

    def _word(self) -> Expr:
        """An operand that starts with a word: a path or a function call."""
        token = self._peek()
        word = token.text
        if word in ("header", "line"):
            self._advance()
            self._expect_op(".")
            root: PathRoot = "header" if word == "header" else "line"
            return Path(root, self._names(), pos=token.position)
        if word == "context":
            self._advance()
            return self._context(token)
        if word in ("sum", "count", "exists", "sum_of"):
            self._advance()
            return self._call(token)
        if word in RESERVED_WORDS:
            # and, or, in; not after a comparison or arithmetic operator; lines outside
            # sum and count.
            raise self._unexpected(token)
        self._advance()
        return Path(None, self._names_after(word), pos=token.position)

    def _context(self, context: _Token) -> Expr:
        self._expect_op(".")
        if self._accept_word("today") is not None:
            return CtxToday(pos=context.position)
        if self._accept_word("period") is not None:
            return CtxPeriod(pos=context.position)
        if self._accept_word("user") is not None:
            self._expect_op(".")
            return Path("context.user", self._names(), pos=context.position)
        raise self._unexpected(self._peek(), "today, period or user")

    def _call(self, function: _Token) -> Expr:
        self._expect_op("(")
        if function.text == "sum":
            lines = self._expect_word("lines")
            self._expect_op(".")
            field = self._expect_name()
            self._expect_op(")")
            return Sum(field.text, pos=function.position, lines_pos=lines.position)
        if function.text == "count":
            lines = self._expect_word("lines")
            self._expect_op(")")
            return Count(pos=function.position, lines_pos=lines.position)
        target = self._expect_name()
        if function.text == "exists":
            self._expect_op(",")
            cond = self._nested(function)
            self._expect_op(")")
            return Exists(target.text, cond, pos=function.position, target_pos=target.position)
        self._expect_op(".")
        field = self._expect_name()
        self._expect_op(",")
        cond = self._nested(function)
        self._expect_op(")")
        return SumOf(
            target.text, field.text, cond, pos=function.position, target_pos=target.position
        )


# --- depth -------------------------------------------------------------------------------


def _too_deep() -> str:
    return f"expression nested more than {MAX_DEPTH} levels deep"


def _children(expr: Expr) -> tuple[Expr, ...]:
    match expr:
        case Not(operand=operand):
            return (operand,)
        case Binary(left=left, right=right):
            return (left, right)
        case In(left=left):
            return (left,)
        case Exists(cond=cond) | SumOf(cond=cond):
            return (cond,)
        case _:
            return ()


def _check_height(expr: Expr) -> None:
    """Raise ExprError if a node of ``expr`` has more than MAX_DEPTH ancestors.

    The error is at the node with exactly MAX_DEPTH + 1 levels below it (one exists on
    the path to every node too deep) that comes first in the text. Iterative, so it
    handles a tree of any height, such as a long chain of ``+`` built by the loop of
    ``_additive``.
    """
    heights: dict[int, int] = {}  # id(node) -> levels below it; the tree keeps nodes alive
    too_high: list[int] = []
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    while pending:
        node, children_done = pending.pop()
        children = _children(node)
        if not children_done:
            pending.append((node, True))
            pending.extend((child, False) for child in children)
            continue
        height = max((heights[id(child)] + 1 for child in children), default=0)
        heights[id(node)] = height
        if height == MAX_DEPTH + 1:
            too_high.append(node.pos)
    if too_high:
        raise ExprError("syntax", min(too_high), _too_deep())
