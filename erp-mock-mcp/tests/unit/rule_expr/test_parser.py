"""erp_mock.domain.rule_expr: parsing and printing rule expressions (requirement 10)."""

from decimal import Decimal

import pytest

from erp_mock.domain.rule_expr.errors import ExprError
from erp_mock.domain.rule_expr.nodes import (
    Binary,
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
    Str,
    Sum,
    SumOf,
)
from erp_mock.domain.rule_expr.parser import MAX_DEPTH, parse_expr
from erp_mock.domain.rule_expr.printer import print_expr


def header(*names: str) -> Path:
    return Path("header", names)


def line(*names: str) -> Path:
    return Path("line", names)


def bare(*names: str) -> Path:
    return Path(None, names)


def num(text: str) -> Num:
    return Num(Decimal(text))


def strings(*values: str) -> ListLit:
    return ListLit(tuple(Str(value) for value in values))


A, B, C, D = bare("a"), bare("b"), bare("c"), bare("d")


def syntax_error(text: str) -> ExprError:
    """Parse ``text``, which must fail with a syntax error, and return the error."""
    with pytest.raises(ExprError) as info:
        parse_expr(text)
    assert info.value.phase == "syntax"
    return info.value


# --- grammar (requirement 10.1) ----------------------------------------------------------

# Canonical texts, so each pair also checks print_expr.
FORMS: list[tuple[str, Expr]] = [
    (
        "header.delivery_date >= header.biz_date",
        Binary(">=", header("delivery_date"), header("biz_date")),
    ),
    ("line.material.kind == 'raw'", Binary("==", line("material", "kind"), Str("raw"))),
    ("context.today > header.due_date", Binary(">", CtxToday(), header("due_date"))),
    ("context.period != header.period", Binary("!=", CtxPeriod(), header("period"))),
    ("context.user.role == 'buyer'", Binary("==", Path("context.user", ("role",)), Str("buyer"))),
    (
        "header.status in ['approved', 'closed']",
        In(header("status"), strings("approved", "closed")),
    ),
    (
        "line.tax_rate in [0, 0.13, 100]",
        In(line("tax_rate"), ListLit((num("0"), num("0.13"), num("100")))),
    ),
    ("sum(lines.amount) > 0", Binary(">", Sum("amount"), num("0"))),
    ("count(lines) <= 200", Binary("<=", Count(), num("200"))),
    (
        "exists(supplier, code == header.supplier.code and status == 'enabled')",
        Exists(
            "supplier",
            Binary(
                "and",
                Binary("==", bare("code"), header("supplier", "code")),
                Binary("==", bare("status"), Str("enabled")),
            ),
        ),
    ),
    (
        "sum_of(stock_balance.qty, material_id == line.material_id) >= line.qty_base",
        Binary(
            ">=",
            SumOf("stock_balance", "qty", Binary("==", bare("material_id"), line("material_id"))),
            line("qty_base"),
        ),
    ),
    ("'it''s' != ''", Binary("!=", Str("it's"), Str(""))),
    ("not header.closed", Not(header("closed"))),
    # Reserved words are names after ".", as exists and sum_of targets and as fields.
    (
        "header.count + line.in > sum(lines.not)",
        Binary(">", Binary("+", header("count"), line("in")), Sum("not")),
    ),
    ("exists(line, x.and == 1)", Exists("line", Binary("==", bare("x", "and"), num("1")))),
]


@pytest.mark.req("10.1", "10.3")
@pytest.mark.parametrize(("text", "tree"), FORMS)
def test_parses_every_form(text: str, tree: Expr) -> None:
    assert parse_expr(text) == tree
    assert print_expr(tree) == text


@pytest.mark.req("10.1")
@pytest.mark.parametrize(
    ("text", "tree"),
    [
        ("a or b and c", Binary("or", A, Binary("and", B, C))),
        ("a and b or c", Binary("or", Binary("and", A, B), C)),
        ("not a and b", Binary("and", Not(A), B)),
        ("not a == b", Not(Binary("==", A, B))),
        ("not not a", Not(Not(A))),
        ("a + b * c", Binary("+", A, Binary("*", B, C))),
        ("a - b - c", Binary("-", Binary("-", A, B), C)),
        ("a / b * c", Binary("*", Binary("/", A, B), C)),
        ("a + b < c * d", Binary("<", Binary("+", A, B), Binary("*", C, D))),
        ("(a or b) and c", Binary("and", Binary("or", A, B), C)),
        ("a + b in [1] or c", Binary("or", In(Binary("+", A, B), ListLit((num("1"),))), C)),
    ],
)
def test_precedence_and_associativity(text: str, tree: Expr) -> None:
    assert parse_expr(text) == tree


@pytest.mark.req("10.1")
def test_positions_are_code_point_indexes() -> None:
    # A Binary is at its operator, a Path at its first character, a call at its name.
    expr = parse_expr("'甲乙' == header.a + 10")
    assert isinstance(expr, Binary) and isinstance(expr.right, Binary)
    assert (expr.left.pos, expr.pos, expr.right.left.pos, expr.right.pos) == (1, 6, 9, 18)
    assert expr.right.right.pos == 20

    expr = parse_expr("not exists(e, x in ['a']) or sum_of(e.f, y) < count(lines)")
    assert isinstance(expr, Binary) and isinstance(expr.left, Not)
    exists = expr.left.operand
    assert isinstance(exists, Exists) and isinstance(exists.cond, In)
    assert (expr.left.pos, exists.pos, exists.target_pos) == (1, 5, 12)
    assert (exists.cond.left.pos, exists.cond.pos, exists.cond.right.pos) == (15, 17, 20)
    assert exists.cond.right.items[0].pos == 21
    assert isinstance(expr.right, Binary)
    sum_of, count = expr.right.left, expr.right.right
    assert isinstance(sum_of, SumOf) and isinstance(count, Count)
    assert (sum_of.pos, sum_of.target_pos, sum_of.cond.pos) == (30, 37, 42)
    assert (expr.right.pos, count.pos, count.lines_pos) == (45, 47, 53)

    expr = parse_expr("context.today < context.user.d + sum(lines.q)")
    assert isinstance(expr, Binary) and isinstance(expr.right, Binary)
    total = expr.right.right
    assert isinstance(total, Sum)
    assert (expr.left.pos, expr.right.left.pos, total.pos, total.lines_pos) == (1, 17, 34, 38)


@pytest.mark.req("10.1")
def test_equality_and_hash_ignore_positions() -> None:
    first, second = parse_expr("a==1"), parse_expr("  ( a )  ==  1")
    assert first == second
    assert hash(first) == hash(second)
    assert isinstance(first, Binary) and isinstance(second, Binary)
    assert (first.pos, second.pos) == (2, 10)


# --- canonical text (requirement 10.3) ---------------------------------------------------


@pytest.mark.req("10.3")
@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("(line.qty>0)", "line.qty > 0"),
        ("line.qty > 0", "line.qty > 0"),
        ("((a))+ (b*c)", "a + b * c"),
        (" not(a)and\n\tb\r\n", "not a and b"),
        ("sum ( lines . qty )>=0", "sum(lines.qty) >= 0"),
        ("x in['a' ,'b']", "x in ['a', 'b']"),
        ("exists( e ,(x==1) )", "exists(e, x == 1)"),
        ("(a - (b)) - ((c))", "a - b - c"),
        ("not (a == b)", "not a == b"),
    ],
)
def test_whitespace_and_redundant_parentheses_do_not_matter(text: str, canonical: str) -> None:
    assert print_expr(parse_expr(text)) == canonical
    assert parse_expr(text) == parse_expr(canonical)


@pytest.mark.req("10.3")
@pytest.mark.parametrize(
    ("tree", "text"),
    [
        (Binary("-", A, Binary("-", B, C)), "a - (b - c)"),
        (Binary("*", Binary("+", A, B), C), "(a + b) * c"),
        (Binary("/", A, Binary("*", B, C)), "a / (b * c)"),
        (Binary("and", A, Binary("and", B, C)), "a and (b and c)"),
        (Binary("or", Binary("and", A, B), Binary("and", C, D)), "a and b or c and d"),
        (Binary("and", Binary("or", A, B), C), "(a or b) and c"),
        (Not(Binary("and", A, B)), "not (a and b)"),
        (Binary("or", Not(A), Not(B)), "not a or not b"),
        (Binary("==", Not(A), B), "(not a) == b"),
        (Binary("==", Binary("<", A, B), C), "(a < b) == c"),
        (Binary("<", A, Binary("<", B, C)), "a < (b < c)"),
        (Binary("+", Binary("==", A, B), C), "(a == b) + c"),
        (In(Binary("==", A, B), ListLit((num("1"),))), "(a == b) in [1]"),
        (Binary("==", In(A, strings("x")), B), "(a in ['x']) == b"),
        (Exists("e", Binary("or", A, B)), "exists(e, a or b)"),
    ],
)
def test_printer_adds_only_needed_parentheses(tree: Expr, text: str) -> None:
    assert print_expr(tree) == text
    assert parse_expr(text) == tree


@pytest.mark.req("10.3")
@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("1.50", "1.5"),
        ("100", "100"),
        ("1E+2", "100"),
        ("007", "7"),
        ("0.000", "0"),
        ("-0", "0"),
        ("0.130", "0.13"),
        ("10.0", "10"),
        ("1E-7", "0.0000001"),
        ("123456789012345678901234567890.5", "123456789012345678901234567890.5"),
    ],
)
def test_numbers_print_without_needless_zeros(value: str, text: str) -> None:
    assert print_expr(Num(Decimal(value))) == text
    assert parse_expr(text) == Num(Decimal(value))  # equal by value


@pytest.mark.req("10.3")
@pytest.mark.parametrize(
    ("value", "text"),
    [("it's", "'it''s'"), ("", "''"), ("''", "''''''"), ("a\nb 甲", "'a\nb 甲'")],
)
def test_strings_double_their_quotes(value: str, text: str) -> None:
    assert print_expr(Str(value)) == text
    assert parse_expr(text) == Str(value)


# --- syntax errors (requirement 10.1, positions as requirement 10.2 defines) -------------


@pytest.mark.req("10.1")
def test_comparisons_do_not_chain() -> None:
    error = syntax_error("a < b < c")
    assert error.position == 7  # the second "<"
    assert error.reason == "comparisons cannot be chained"
    assert syntax_error("a == b in ['x']").position == 8
    assert syntax_error("a in [1] in [2]").position == 10


@pytest.mark.req("10.1")
def test_unterminated_string_is_at_the_end_of_the_text() -> None:
    text = "header.x == 'abc"
    error = syntax_error(text)
    assert error.position == len(text) + 1
    assert str(error) == f"syntax error at position {len(text) + 1}: unterminated string literal"
    assert syntax_error("'abc").position == 5
    assert syntax_error("x in ['a', 'it''s").position == 18
    # Where no literal can stand, the literal itself is the unacceptable token.
    assert syntax_error("header.x 'abc").position == 10


@pytest.mark.req("10.1")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("line.qty > 0 AND line.price > 0", 14),
        ("a == 1 Or b", 8),
        ("NOT a", 1),
        ("x In [1]", 3),
        ("Header.x == 1", 1),
        ("header.Qty == 1", 8),
        ("SUM(lines.qty) > 0", 1),
        ("count(LINES)", 7),
    ],
)
def test_keywords_and_names_are_lower_case(text: str, position: int) -> None:
    error = syntax_error(text)
    assert error.position == position
    assert error.reason.endswith("are lower case)")


@pytest.mark.req("10.1")
@pytest.mark.parametrize(
    ("text", "position"),
    [
        # The text ends early: its length plus 1.
        ("", 1),
        ("   ", 4),
        ("header", 7),
        ("header.", 8),
        ("header.qty >", 13),
        ("a and", 6),
        ("not", 4),
        ("(a == 1", 8),
        ("exists(e, x == 1", 17),
        ("context.user", 13),
        # The first token that cannot be accepted where it stands.
        ("-1 < 0", 1),
        ("header.qty > -1", 14),
        ("a == not b", 6),
        ("a b", 3),
        ("a == 1)", 7),
        ("[1] == x", 1),
        ("x in []", 7),
        ("x in [1,]", 9),
        ("x in [y]", 7),
        ("x in 'a'", 6),
        ("x not in [1]", 3),
        ("lines.qty > 0", 1),
        ("sum(line.qty) > 0", 5),
        ("sum(lines) > 0", 10),
        ("sum(lines.material.kind)", 19),
        ("count(lines.qty)", 12),
        ("exists(e)", 9),
        ("sum_of(e, x == 1)", 9),
        ("context.now", 9),
        ("context.user == 1", 14),
        ("context.today.x", 14),
        ("header..x", 8),
        ("header.1", 8),
        ("1.", 2),
        ("1.5.2", 4),
        (".5", 1),
        ("1e3", 2),
        ("'a' 'b'", 5),
        # Characters that start no token.
        ("a = 1", 3),
        ("a ! b", 3),
        ("a <> b", 4),
        ('x == "a"', 6),
        ("_x == 1", 1),
        ("a\u3000== b", 2),  # ideographic space
        ("a ＝＝ b", 3),  # full-width equals sign
    ],
)
def test_syntax_error_positions(text: str, position: int) -> None:
    assert syntax_error(text).position == position


@pytest.mark.req("10.1")
def test_nesting_limit() -> None:
    depth = MAX_DEPTH
    assert parse_expr("(" * depth + "x" + ")" * depth) == bare("x")
    assert syntax_error("(" * (depth + 1) + "x" + ")" * (depth + 1)).position == depth + 1
    assert isinstance(parse_expr("not " * depth + "x"), Not)
    assert syntax_error("not " * (depth + 1) + "x").position == 4 * depth + 1
    # A chain of + nests through the loop of the parser, not through parentheses.
    assert isinstance(parse_expr("x" + " + x" * depth), Binary)
    error = syntax_error("x" + " + x" * (depth + 1))
    assert error.position == 3 + 4 * depth  # the last "+"
    assert error.reason == f"expression nested more than {depth} levels deep"


# --- trees (requirement 10.1) ------------------------------------------------------------


@pytest.mark.req("10.1")
@pytest.mark.parametrize(
    ("cls", "args", "error"),
    [
        (Num, (Decimal("-1"),), ValueError),
        (Num, (Decimal("NaN"),), ValueError),
        (Num, (Decimal("Infinity"),), ValueError),
        (Num, (1,), TypeError),
        (Str, (1,), TypeError),
        (Path, (None, ("and",)), ValueError),
        (Path, (None, ("lines",)), ValueError),
        (Path, ("lines", ("qty",)), ValueError),
        (Path, ("header", ()), ValueError),
        (Path, ("header", ("Qty",)), ValueError),
        (Path, ("header", ["qty"]), TypeError),
        (Binary, ("=", A, B), ValueError),
        (Binary, ("+", A, "b"), TypeError),
        (Not, (strings("x"),), TypeError),
        (ListLit, ((),), ValueError),
        (ListLit, ((A,),), TypeError),
        (In, (A, (Str("x"),)), TypeError),
        (Sum, ("Qty",), ValueError),
        (Exists, ("e", strings("x")), TypeError),
        (SumOf, ("e", "1f", A), ValueError),
    ],
)
def test_trees_without_a_text_cannot_be_built(
    cls: type, args: tuple[object, ...], error: type[Exception]
) -> None:
    with pytest.raises(error):
        cls(*args)
