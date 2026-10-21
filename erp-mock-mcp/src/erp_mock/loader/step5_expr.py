"""Step 5, part four: the rule expressions of the rules, writebacks and queries (req 10.2).

Three items of a business pack hold a rule expression: the ``check`` of an enforced rule,
the ``value`` of a writeback and the ``filter`` of a declarative query. Each of them is
written against one object - the bill the rule applies to, the bill a push produces, the
records a query reads - and this part settles that the expression fits that object: it
parses the text and runs the names, operator type and result type checks of the
Rule_Engine on it (``erp_mock.domain.rule_expr``), so the engine never meets a check it
cannot evaluate.

What each expression is evaluated on, which is the :class:`ExprScope` built for it:

==================  =========================  =================  =====================
expression          ``header.``                ``line.``          result
==================  =========================  =================  =====================
rule ``check``      the header of ``bill``     its lines          boolean
writeback ``value`` the header of the target    its lines, only   number
                    bill of the conversion     for a
                                               ``source_line.``
                                               target
query ``filter``    the entity record, the     the line, only     boolean
                    bill header or the         for a
                    header of the line's       ``<bill>_line``
                    bill                       source
==================  =========================  =================  =====================

The rest of a scope is the same everywhere: ``context.user.<field>`` is a field of the
user context entity (requirement 1.14), ``sum(lines.<field>)`` and ``count(lines)`` are
the lines of the bill the evaluated record belongs to - a query over bill headers
included, where ``line.`` does not resolve - and the target of an ``exists`` or a
``sum_of`` is any entity or bill of the instance, a bill taking its header fields
(requirement 10.13). A ``value`` is a number because the writeback adds it to the link
field of the source, a qty or money field (requirement 8.8); a ``check`` and a ``filter``
are boolean (requirement 10.11).

One error per expression: the first of the four checks that finds anything reports its
error with the smallest position, which is what the parser and the checker already do
(requirement 10.2). The error names the item - the rule number, the writeback id or the
query tool name - the position as a 1-based code point index, and which check found it::

    [step 5] fx_trade: rules/depots_differ.yaml: rule.check: 规则 R-FXT-001 的 check 第 8 位有名称错误：...

What is skipped: an expression whose object did not resolve, since the name a rule, a
writeback or a query wrote was reported by ``loader.step5_model`` and nothing can be
looked up in a bill that is not there; and an expression that reads ``context.user``
while the instance does not have exactly one user context entity, which
``loader.step5_namespace`` reported (requirement 1.14). An expression that expands a ref
field whose target did not resolve is reported here as well: the path cannot be expanded
either way, and it is the path, not the field declaration, that this error is about.

The syntax trees are what the step leaves behind. They belong to no layer below this one:
the configuration objects of :mod:`erp_mock.config.model` may not hold them, since config
does not import domain (requirement 24.8), so :class:`ParsedDomain` pairs the domain model
with the tree of every expression of it, keyed by the rule number, the writeback id and
the query tool name. The engine evaluates those trees instead of parsing a text again, and
a tree is kept as soon as it parses, so a step that reported an error still shows what was
read.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.model import BillModel, DomainModel, FieldLevel, FieldModel
from erp_mock.domain.rule_expr.checker import (
    FIELD_EXPR_TYPES,
    ExprType,
    FieldInfo,
    check_expr,
    expr_type,
)
from erp_mock.domain.rule_expr.errors import ExprError, ExprPhase
from erp_mock.domain.rule_expr.nodes import Binary, Exists, Expr, In, Not, Path, SumOf
from erp_mock.domain.rule_expr.parser import parse_expr
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.step4 import RULE_KEY, WRITEBACK_KEY, RawPack

__all__ = ["ParsedDomain", "check"]

# The item of each kind of document that holds a rule expression, as the document writes
# it; also the last part of the config path an error about it is reported at.
CHECK_ITEM: Final = "check"
VALUE_ITEM: Final = "value"
FILTER_ITEM: Final = "filter"
# The item of an exposure that holds the queries, whose index locates one of them
# (``queries[0].filter``, :mod:`erp_mock.loader.step5_namespace`).
QUERIES_KEY: Final = "queries"

# How an error names the check that found it (requirement 10.2).
_PHASE_NAMES: Final[Mapping[ExprPhase, str]] = {
    "syntax": "语法",
    "names": "名称",
    "operators": "运算类型",
    "result": "结果类型",
}

# How an error names the object the expression belongs to.
_RULE: Final = "规则"
_WRITEBACK: Final = "反写"
_QUERY: Final = "查询工具"

# The prefix of the paths that read the current user's record (requirement 10.1).
_USER_ROOT: Final = "context.user"


@dataclass(frozen=True, slots=True)
class ParsedDomain:
    """The domain model and the syntax tree of every rule expression of it.

    ``rules`` holds the ``check`` of each enforced rule by rule number, ``writebacks`` the
    ``value`` of each writeback by writeback id and ``queries`` the ``filter`` of each
    declarative query by tool name, each in the order of the model. An expression whose
    text does not parse has no tree and is absent; so is one whose object did not resolve,
    which was reported where that name was written. The trees are the configuration of a
    valid instance only when step 5 reported nothing, as the model itself is.
    """

    domain: DomainModel
    rules: FrozenMap[str, Expr] = FrozenMap()
    writebacks: FrozenMap[str, Expr] = FrozenMap()
    queries: FrozenMap[str, Expr] = FrozenMap()


def check(packs: Sequence[RawPack], domain: DomainModel, errors: StepErrors) -> ParsedDomain:
    """Check every rule expression of ``domain``; return the model with its syntax trees.

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated, which is where the position of a query inside an exposure comes from;
    ``domain`` is the model the earlier parts of step 5 built. Everything found is recorded
    in ``errors``, so this part of the step passed only when ``errors`` stayed empty.
    """
    return ParsedDomain(
        domain=domain,
        rules=FrozenMap(_rules(domain, errors)),
        writebacks=FrozenMap(_writebacks(domain, errors)),
        queries=FrozenMap(_queries(packs, domain, errors)),
    )


# --- the expressions of the three kinds of document --------------------------------------


def _rules(domain: DomainModel, errors: StepErrors) -> Iterator[tuple[str, Expr]]:
    """The ``check`` of every enforced rule, evaluated on the bill it applies to (req 10.5).

    A described rule declares no check and is never evaluated (requirement 10.9), and a
    rule whose bill did not resolve is skipped: that name was reported by
    ``loader.step5_model``.
    """
    for rule in domain.rules.values():
        text = rule.check
        bill = None if rule.bill is None else domain.bills.get(rule.bill)
        if rule.enforcement != "enforced" or text is None or bill is None:
            continue
        item = _Item(
            what=f"{_RULE} {rule.id}",
            item=CHECK_ITEM,
            pack=rule.pack,
            file=rule.file,
            path=f"{RULE_KEY}.{CHECK_ITEM}",
            scope=_bill_scope(domain, bill, current_line=True),
            expected=ExprType.BOOL,
        )
        tree = _parsed(text, item, errors)
        if tree is not None:
            yield rule.id, tree


def _writebacks(domain: DomainModel, errors: StepErrors) -> Iterator[tuple[str, Expr]]:
    """The ``value`` of every writeback, evaluated on the bill the push produces (req 8.8).

    A ``source_line.<field>`` target adds the value of each target line to the source line
    it comes from, so ``line.`` is the current target line; a ``source.<field>`` target
    adds one value to the source header, so the value is evaluated on the target header
    alone and ``line.`` does not resolve, the lines being reachable with ``sum(lines.F)``.
    A writeback whose conversion, or whose target bill, did not resolve is skipped.
    """
    for writeback in domain.writebacks.values():
        conversion = domain.conversions.get(writeback.conversion)
        target = None if conversion is None else domain.bills.get(conversion.target)
        if target is None:
            continue
        item = _Item(
            what=f"{_WRITEBACK} {writeback.id}",
            item=VALUE_ITEM,
            pack=writeback.pack,
            file=writeback.file,
            path=f"{WRITEBACK_KEY}.{VALUE_ITEM}",
            scope=_bill_scope(domain, target, current_line=writeback.target.level == "line"),
            expected=ExprType.NUM,
        )
        tree = _parsed(writeback.value, item, errors)
        if tree is not None:
            yield writeback.id, tree


def _queries(
    packs: Sequence[RawPack], domain: DomainModel, errors: StepErrors
) -> Iterator[tuple[str, Expr]]:
    """The ``filter`` of every declarative query, evaluated on one source record (req 10.10).

    A query without a filter selects every record, a primitive query has none, and a query
    whose source did not resolve is skipped: that name was reported by
    ``loader.step5_model``.
    """
    paths = _filter_paths(packs)
    for query in domain.queries.values():
        text = query.filter
        scope = _query_scope(domain, query.source, query.source_level)
        if text is None or scope is None:
            continue
        item = _Item(
            what=f"{_QUERY} {query.tool}",
            item=FILTER_ITEM,
            pack=query.pack,
            file=query.file,
            path=paths.get((query.pack, query.file, query.tool), FILTER_ITEM),
            scope=scope,
            expected=ExprType.BOOL,
        )
        tree = _parsed(text, item, errors)
        if tree is not None:
            yield query.tool, tree


def _filter_paths(packs: Sequence[RawPack]) -> Mapping[tuple[str, str, str], str]:
    """The config path of the filter of each declared query, by (pack, file, tool name).

    A query is located by its position in the ``queries`` of its exposure, the way
    ``loader.step5_namespace`` locates one, which only the document shows; the model keys
    the queries by tool name and keeps the first declaration of a name.
    """
    found: dict[tuple[str, str, str], str] = {}
    for pack in packs:
        exposure = pack.exposure
        if exposure is None:
            continue
        for index, query in enumerate(exposure.document.queries):
            key = (pack.name, exposure.file, query.tool)
            found.setdefault(key, f"{QUERIES_KEY}[{index}].{FILTER_ITEM}")
    return found


# --- one expression ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Item:
    """One expression of the configuration: where it is written and what it must be.

    ``what`` is how an error names the object that declares it (``规则 R-FXT-001``),
    ``item`` the item that holds it (``check``), ``pack``, ``file`` and ``path`` where it
    is reported, ``scope`` the object it is evaluated on and ``expected`` the type class
    the whole expression must have.
    """

    what: str
    item: str
    pack: str
    file: str
    path: str
    scope: "_Scope"
    expected: ExprType


def _parsed(text: str, item: _Item, errors: StepErrors) -> Expr | None:
    """Parse and check one expression; return its tree, or None when the text does not parse.

    The four checks of requirement 10.2 in order: the parser raises the syntax error, the
    checker the names and operator type errors, and the result type is the last. Each of
    them is reported at the item that wrote the expression; the tree is returned whether
    the checks passed or not, so the model shows what was read.
    """
    try:
        tree = parse_expr(text)
    except ExprError as error:
        _report(item, error, errors)
        return None
    if _reads_unknown_user(tree, item.scope):
        # No user record to resolve ``context.user`` against; requirement 1.14 was
        # reported by ``loader.step5_namespace``.
        return tree
    try:
        _check_types(tree, item)
    except ExprError as error:
        _report(item, error, errors)
    return tree


def _check_types(tree: Expr, item: _Item) -> None:
    """Run the names, operator type and result type checks of ``tree`` (requirements 10.2, 10.11).

    A boolean expression is exactly what :func:`check_expr` checks; a writeback value takes
    the same three checks with a number as its result, which the engine adds to the link
    field of the source (requirement 8.8). A result type error is at position 1.
    """
    if item.expected is ExprType.BOOL:
        check_expr(tree, item.scope)
        return
    found = expr_type(tree, item.scope)
    if found is not item.expected:
        raise ExprError("result", 1, f"the expression must be {item.expected}, got {found}")


def _report(item: _Item, error: ExprError, errors: StepErrors) -> None:
    """Record one expression error: the object, the item, the position and the check."""
    reason = (
        f"{item.what} 的 {item.item} 第 {error.position} 位有"
        f"{_PHASE_NAMES[error.phase]}错误：{error.reason}"
    )
    errors.add_at(item.pack, item.file, item.path, reason)


def _reads_unknown_user(tree: Expr, scope: "_Scope") -> bool:
    """Whether ``tree`` reads ``context.user`` while the user context entity is unknown."""
    return scope.domain.user_entity is None and _reads_user(tree)


def _reads_user(expr: Expr) -> bool:
    """Whether ``expr`` holds a ``context.user.<field>`` path, inside a condition included."""
    match expr:
        case Path(root=root):
            return root == _USER_ROOT
        case Not(operand=operand):
            return _reads_user(operand)
        case Binary(left=left, right=right):
            return _reads_user(left) or _reads_user(right)
        case In(left=left):
            # The right operand is a list of literals, which read nothing.
            return _reads_user(left)
        case Exists(cond=cond) | SumOf(cond=cond):
            return _reads_user(cond)
        case _:
            return False


# --- the object an expression is evaluated on --------------------------------------------


@dataclass(frozen=True, slots=True)
class _Scope:
    """What the names of one expression resolve to: the domain model's answer (req 10.2).

    The :class:`~erp_mock.domain.rule_expr.checker.ExprScope` the checker asks, built over
    the field views of the model. ``header`` holds the fields ``header.<field>`` reads: the
    header of a bill, or the record of the entity a query's source names. ``lines`` holds
    the lines of the bill the evaluated record belongs to, None when it belongs to none,
    and ``current_line`` says whether ``line.<field>`` reads one of them: a query over bill
    headers has lines to sum but no current line, and so has the value of a writeback onto
    the source header.
    """

    domain: DomainModel
    header: Mapping[str, FieldModel]
    lines: Mapping[str, FieldModel] | None
    current_line: bool

    @property
    def has_current_line(self) -> bool:
        """Whether ``line.<field>`` paths resolve."""
        return self.current_line

    @property
    def has_bill_lines(self) -> bool:
        """Whether ``sum(lines.<field>)`` and ``count(lines)`` resolve."""
        return self.lines is not None

    def header_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the header, or of the entity record a query reads."""
        return _info(self.header.get(name))

    def line_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the lines, which ``sum`` reads as well."""
        return None if self.lines is None else _info(self.lines.get(name))

    def user_field(self, name: str) -> FieldInfo | None:
        """The field ``name`` of the user context entity (requirement 1.14)."""
        user = self.domain.user_entity
        return None if user is None else _info(user.fields.get(name))

    def has_target(self, target: str) -> bool:
        """Whether ``target`` names an entity or a bill of the instance."""
        return target in self.domain.entities or target in self.domain.bills

    def target_field(self, target: str, name: str) -> FieldInfo | None:
        """The field ``name`` of the records of ``target``, a bill taking its header."""
        return _info(_records(self.domain, target).get(name))


def _bill_scope(domain: DomainModel, bill: BillModel, *, current_line: bool) -> _Scope:
    """The scope of an expression evaluated on ``bill``: its header and its lines."""
    return _Scope(domain=domain, header=bill.header, lines=bill.lines, current_line=current_line)


def _query_scope(
    domain: DomainModel, source: str | None, level: FieldLevel | None
) -> _Scope | None:
    """The scope of the filter of a declarative query (requirement 10.10).

    The source is an entity (``record``), the headers of a bill (``header``) or its lines
    (``line``, written ``<bill>_line``): ``line.`` resolves for the lines alone, while the
    header of their bill resolves for all three records of a bill. None when the source did
    not resolve, which ``loader.step5_model`` reported.
    """
    if source is None or level is None:
        return None
    if level == "record":
        entity = domain.entities.get(source)
        if entity is None:
            return None
        return _Scope(domain=domain, header=entity.fields, lines=None, current_line=False)
    bill = domain.bills.get(source)
    if bill is None:
        return None
    return _bill_scope(domain, bill, current_line=level == "line")


def _records(domain: DomainModel, target: str) -> Mapping[str, FieldModel]:
    """The fields of one record of ``target``: an entity's fields or a bill's header.

    The records of an exists or sum_of target, and of the entity or bill a ref field
    refers to; a bill takes its header fields (requirement 10.13).
    """
    bill = domain.bills.get(target)
    if bill is not None:
        return bill.header
    entity = domain.entities.get(target)
    return FrozenMap() if entity is None else entity.fields


def _info(field: FieldModel | None) -> FieldInfo | None:
    """What a scope tells the checker about ``field``: its type class and ref target.

    The type classes are those of requirement 10.11 (:data:`FIELD_EXPR_TYPES`). Only a ref
    field can be expanded, so only it carries a target; the header's ``source_bill_id``
    names its bill per record and a target that did not resolve is None, and neither can be
    expanded.
    """
    if field is None:
        return None
    return FieldInfo(FIELD_EXPR_TYPES[field.type], field.ref if field.type == "ref" else None)
