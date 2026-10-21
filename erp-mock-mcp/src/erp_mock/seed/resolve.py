"""The static pass over an initial dataset definition (requirements 20.1, 20.6, 20.16).

A build replays a dataset definition through the engine, record by record
(requirement 20.2). Nothing about that replay is decided while it runs: :func:`resolve`
reads the definition once, against the resolved configuration and the business packs,
checks everything the configuration alone settles and returns the :class:`SeedPlan` the
build then walks (design.md component 6.1)::

    plan = resolve(dataset, domain, packs, file="datasets/purchase-basic.yaml")
    for record in plan.records:          # the written order of ``entities``
        ...                              # create or push, then the operations of its status

What the plan holds is the definition with every name already looked up: the entity or
bill of each record, the :class:`~erp_mock.config.model.FieldModel` behind every written
key, the parsed date expressions, the conversion of a push, the state operations a status
amounts to, and the generators a ``bulk`` item draws. What it leaves to the build is
everything that needs the database being filled: the internal id an alias stands for, the
record a code names, the value a generator draws, and the date an expression gives at the
anchor date.

The checks, each raising :class:`SeedError` at the first problem, in the order the
definition writes its keys:

==============================  ====================================================
written                         checked here
==============================  ====================================================
``current_user``                an internal id of the user context entity, never an
                                alias or a code (requirement 20.16)
``periods.closed_before``       an expression of the period category (20.6)
``entities`` keys               the alias prefix is the record's ``kind``, and an
                                alias is used only after it is declared (20.6)
``kind``                        an entity or a bill of the instance (20.6)
a field key                     the field exists, a ref written without its ``_id``
                                suffix and a field named ``kind`` as
                                ``<kind value>_kind`` (20.16); the field is writable
                                and, on a pushed bill, not mapped by the
                                conversion's ``header_mapping`` (20.6)
a field value                   a ref resolves to an alias of the right kind or is
                                left to the build as a code (20.16); a date field
                                takes an expression of the date category (20.6); a
                                generator's parameters are usable and its precision
                                fits the field (20.6, 4.7)
``source``                      an alias of an earlier bill with exactly one
                                conversion to this record's kind (20.6)
``lines[].source_line``         a line number of the source bill, only on a pushed
                                bill, and every pushed line has one (20.16)
``status``                      a state of the bill's own state machine, and the
                                operations that reach it (20.6, 20.2)
``bulk[].kind``                 an entity whose ``shared`` is false (20.6)
==============================  ====================================================

Two of the cases requirement 20.6 lists are settled before this pass: a repeated alias is
a repeated YAML mapping key, which reading the document reports at its line
(:mod:`erp_mock.loader.yaml_io`), and an unknown generator or a parameter that does not fit
it is rejected by the generator models of :mod:`erp_mock.config.pack`, so loading step 4
reports it as an instance configuration error (requirement 2.13). What is left of the
generators here is the relation between two of their parameters
(:func:`~erp_mock.seed.generators.param_problem`) and the field each one fills.

What this pass does not check, because the engine or the build does:

- whether a written value fits its field - a quantity that is no decimal, a date outside
  the calendar, a value outside an enum: the engine validates every value it is handed,
  and an engine error ends the build (requirement 20.15);
- whether the code a ref names exists and is unique, and whether the state operations of a
  status and the records of ``bulk`` get through: both need the records created so far
  (requirement 20.6, the build's part);
- the Shared_Data of ``include_shared``: step 5 checked that those names are packs of the
  instance (requirement 2.10), and the build creates their records in the order
  requirement 20.17 gives.

The values the plan carries stay as the definition writes them: a decimal is still the
text ``"0.85"``, since it is the engine that turns a value into one of the field's
precision (requirement 4.7). The build hands a :class:`Written` value to the engine as the
tools layer hands over an argument.
"""

import re
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from erp_mock.common.date_expr import (
    Category,
    DateExprError,
    Edge,
    MonthDay,
    MonthPeriod,
    Today,
    WeekDay,
    parse_date_expr,
)
from erp_mock.common.decimals import PLACES
from erp_mock.common.frozen import FrozenMap
from erp_mock.config.instance import ALIAS_PATTERN, BulkDef, DatasetDef, DatasetRecord, FieldValue
from erp_mock.config.model import (
    BillModel,
    ConversionModel,
    DomainModel,
    FieldModel,
    StateMachine,
)
from erp_mock.config.pack import (
    BillStatus,
    DecimalRangeGenerator,
    GeneratorObject,
    ShortNameGenerator,
)
from erp_mock.domain.conversion import SOURCE_LINE_ID
from erp_mock.domain.state_machine import DRAFT, TRANSITIONS, StateAction
from erp_mock.loader.step4 import DATASETS_DIR, RawPack
from erp_mock.seed.generators import NAME_FIELD, param_problem

__all__ = [
    "BULK_KEY",
    "CURRENT_USER_KEY",
    "DATE_TYPE",
    "ENTITIES_KEY",
    "FORWARD_ACTIONS",
    "ID_SUFFIX",
    "KIND_KEY",
    "KIND_SUFFIX",
    "LINES_KEY",
    "PERIODS_PATH",
    "REF_TYPE",
    "SOURCE_KEY",
    "SOURCE_LINE_KEY",
    "STATUS_KEY",
    "AliasId",
    "CodeId",
    "DateAt",
    "Drawn",
    "PlanBulk",
    "PlanField",
    "PlanLine",
    "PlanRecord",
    "PlanValue",
    "SeedError",
    "SeedPlan",
    "Written",
    "dataset_file",
    "lookup_field",
    "resolve",
    "status_actions",
]

# The keys of a dataset definition this module names, as seed-data.md section 2 writes
# them; ``kind``, ``status``, ``source`` and ``lines`` are the reserved keys of a record
# and ``source_line`` the reserved key of one of its lines (requirement 20.16).
ENTITIES_KEY: Final = "entities"
BULK_KEY: Final = "bulk"
CURRENT_USER_KEY: Final = "current_user"
PERIODS_PATH: Final = "periods.closed_before"
KIND_KEY: Final = "kind"
STATUS_KEY: Final = "status"
SOURCE_KEY: Final = "source"
LINES_KEY: Final = "lines"
SOURCE_LINE_KEY: Final = "source_line"

# How a record writes a field named ``kind`` and a ref field (requirement 20.16).
KIND_SUFFIX: Final = "_kind"
ID_SUFFIX: Final = "_id"

# The two field types whose written value is more than itself: a ref is an alias or a code
# and a date is an expression relative to the anchor date (requirement 20.16).
REF_TYPE: Final = "ref"
DATE_TYPE: Final = "date"

# The state operations that move a bill towards the last state of its machine; the status a
# dataset declares is reached by running them from draft (requirement 20.2).
FORWARD_ACTIONS: Final[tuple[StateAction, ...]] = ("submit", "approve", "close")
_FORWARD: Final[Mapping[tuple[StateMachine, BillStatus], StateAction]] = MappingProxyType(
    {
        (machine, step.pre): action
        for (machine, action), step in TRANSITIONS.items()
        if action in FORWARD_ACTIONS
    }
)

# The alias of a record, ``<kind>:<name>``: a ref value of this form is an alias and any
# other text is a code (requirement 20.16).
_ALIAS: Final = re.compile(ALIAS_PATTERN)

# No field of the target is filled from the source: the record is not generated by a push.
_NO_MAPPING: Final[Mapping[str, str]] = FrozenMap()

# The reasons, one per class of error (requirement 20.6).
_KIND_NOT_TEXT: Final = "kind 必须是实体或单据的名称"
_UNKNOWN_KIND: Final = "kind {kind} 不是实例配置中的实体或单据"
_ALIAS_PREFIX: Final = "别名前缀 {prefix} 与 kind {kind} 不同"
_UNKNOWN_ALIAS: Final = "别名 {alias} 不存在"
_LATE_ALIAS: Final = "别名 {alias} 在引用处之后才声明"
_ALIAS_KIND: Final = "别名 {alias} 的 kind 是 {kind}，与引用字段 {field} 引用的 {ref} 不符"
_UNKNOWN_FIELD: Final = "{kind} 没有字段 {field}"
_REF_SPELLING: Final = "引用字段 {field} 去掉 _id 后缀书写，即 {written}"
_KIND_SPELLING: Final = "{kind} 没有字段 {field}；名为 kind 的字段写作 {written}"
_READONLY_FIELD: Final = "字段 {field} 是只读字段，不能写入"
_MAPPED_FIELD: Final = "表头字段 {field} 由下推 {conversion} 的 header_mapping 映射，不能写入"
_NOT_PUSH_INPUT: Final = "下推 {conversion} 的分录只能写 source_line 与 {inputs}，没有 {field}"
_REF_VALUE: Final = "引用字段 {field} 的值必须写成别名或编码"
_NO_REF_TARGET: Final = "引用字段 {field} 引用的实体未解析"
_DATE_VALUE: Final = "日期字段 {field} 的值必须是日期类表达式"
_NOT_DATE: Final = (
    "{value} 不是日期类表达式（如 today、month(-1).day(12)）：位置 {position} 处无法解析"
)
_NOT_PERIOD: Final = "{value} 不是期间类表达式（如 month(0)）：位置 {position} 处无法解析"
_GENERATOR_PARAM: Final = "生成器 {gen} 的参数不合法：{problem}"
_SHORT_NAME_NEEDS_NAME: Final = "生成器 short_name 由同一记录的 name 派生，要求 name 写在它之前"
_GENERATOR_REF: Final = "引用字段 {field} 不能由生成器取值"
_GENERATOR_DATE: Final = "日期字段 {field} 不能由生成器取值"
_GENERATOR_TYPE: Final = "生成器 decimal_range 要求小数字段，{field} 是 {type} 字段"
_DECIMAL_PLACES: Final = (
    "生成器 decimal_range 的边界有 {places} 位小数，超过 {field}（{type}）的 {limit} 位"
)
_DOUBLE_BINDING: Final = "{field} 的生成器在业务包 {first} 与 {second} 中各声明了一次"
_LINES_UNDER: Final = "分录写在 lines 下，不是 {key}"
_ENTITY_LINES: Final = "实体 {kind} 没有分录"
_SOURCE_NOT_BILL: Final = "kind {kind} 不是单据，不能声明 source"
_SOURCE_VALUE: Final = "source 的值必须是来源单据的别名"
_SOURCE_IS_ENTITY: Final = "别名 {alias} 的 kind {kind} 不是单据，不能作为下推的来源"
_NO_CONVERSION: Final = "{source} 到 {target} 之间没有 conversion"
_MANY_CONVERSIONS: Final = "{source} 到 {target} 之间有 {count} 个 conversion：{ids}"
_SOURCE_LINE_ONLY_PUSH: Final = "source_line 只能写在下推生成的单据的分录上"
_SOURCE_LINE_MISSING: Final = "下推生成的分录要写 source_line，即来源单据的行号"
_SOURCE_LINE_VALUE: Final = "source_line 必须是正整数行号"
_SOURCE_LINE_RANGE: Final = "source_line {line_no} 超出来源单据 {alias} 的分录数 {count}"
_STATUS_VALUE: Final = "status 必须是状态名"
_UNKNOWN_STATUS: Final = "status {status} 不属于 {bill} 的 {machine} 状态机（{states}）"
_BULK_NOT_ENTITY: Final = "bulk 的 kind {kind} 不是实例配置中的实体"
_BULK_BILL: Final = "bulk 的 kind {kind} 是单据，bulk 只批量生成实体的记录"
_BULK_SHARED: Final = "bulk 的 kind {kind} 的 shared 为 true，共享基础数据由 include_shared 引入"
_NOT_USER_ID: Final = (
    "current_user {value} 不是用户上下文实体 {entity} 的内部 ID（以 {prefix} 开头）"
)


def dataset_file(dataset: DatasetDef) -> str:
    """The configuration file of ``dataset``, as an error names it.

    ``datasets/<name>.yaml``, relative to the instance directory. Step 4 matches a
    definition by its ``dataset`` field and not by its file name, so a caller that read the
    document itself passes the file it came from (``file`` of
    :class:`~erp_mock.loader.step4.RawDoc`) instead.
    """
    return f"{DATASETS_DIR}/{dataset.dataset}.yaml"


class SeedError(Exception):
    """A dataset the build cannot carry out (requirements 20.6, 20.15, 20.16).

    Fields:

    - ``file``: the configuration file the error is in, relative to the instance directory
      for the dataset definition and, for a generator a business pack binds to a field,
      ``<pack>/seed/generators.yaml``.
    - ``config_path``: the item inside that file, written as a configuration error writes
      it (``entities.vendor:main.lines[0].article``), None where no item corresponds.
    - ``reason``: a one-line description in simplified Chinese.

    ``str(...)`` joins the three with ``": "``, leaving out a config path that is None. A
    plain Exception subclass with ``__slots__``, like the other errors of the engine
    (:class:`~erp_mock.common.errors.ErpError`,
    :class:`~erp_mock.seed.generators.DiscardLimitExceeded`): a frozen dataclass would
    reject the attributes Python's exception machinery assigns.
    """

    __slots__ = ("file", "config_path", "reason")

    def __init__(self, file: str, config_path: str | None, reason: str) -> None:
        super().__init__(file, config_path, reason)
        self.file = file
        self.config_path = config_path
        self.reason = reason

    def __str__(self) -> str:
        parts = (self.file, self.config_path, self.reason)
        return ": ".join(part for part in parts if part)


# --- the resolved values -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Written:
    """The value as the dataset writes it: a string, an integer or a boolean.

    Decimals are text here, as everywhere in a configuration; the engine turns the text
    into a number of the field's precision (requirement 4.7).
    """

    value: str | int | bool


@dataclass(frozen=True, slots=True)
class Drawn:
    """A value the build draws from its random sequence (requirement 20.3).

    ``decl`` is the generator with its parameters, which :func:`resolve` checked against
    the field; :class:`~erp_mock.seed.generators.ValueGenerators` draws the value.
    """

    decl: GeneratorObject


@dataclass(frozen=True, slots=True)
class DateAt:
    """A date the build evaluates at its anchor date (requirements 16.5 and 20.5).

    ``expr`` is the parsed expression of the date category, so
    :func:`~erp_mock.common.date_expr.eval_date_expr` of it gives a date and never a
    period.
    """

    expr: Today | WeekDay | MonthDay | Edge


@dataclass(frozen=True, slots=True)
class AliasId:
    """The internal id of the record ``alias`` names (requirement 20.16).

    That record is declared before this value, so the build has created it and holds its
    id.
    """

    alias: str


@dataclass(frozen=True, slots=True)
class CodeId:
    """The internal id of the record of ``entity`` whose code is ``code`` (req 20.16).

    The build resolves it against the records it has created and fails when there is no
    such record or more than one (requirement 20.6).
    """

    entity: str
    code: str


# One value of the plan, by what the build has to do with it.
type PlanValue = Written | Drawn | DateAt | AliasId | CodeId


# --- the plan ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanField:
    """One written value together with the field it fills.

    ``field`` is the resolved field, so ``field.name`` is the name the engine takes
    (``vendor_id`` for a ``vendor`` key, ``kind`` for a ``material_kind`` key), and
    ``written`` the key as the configuration writes it: the key of the record for a dataset
    record, the ``<entity>.<field>`` path for a generator a pack binds.
    """

    field: FieldModel
    written: str
    value: PlanValue

    @property
    def name(self) -> str:
        """The name of the field, which is the name the engine is given."""
        return self.field.name


@dataclass(frozen=True, slots=True)
class PlanLine:
    """One line of a bill record of ``entities``.

    ``source_line`` is the line number of the source line the line comes from, set exactly
    for the lines of a pushed bill (requirement 20.16); the build turns it into the
    ``source_line_id`` a push takes.
    """

    config_path: str
    fields: tuple[PlanField, ...] = ()
    source_line: int | None = None


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """One record of ``entities``, with every name resolved (requirements 20.2, 20.16).

    - ``alias``: the key of the record, ``<kind>:<name>``, whose internal id goes into the
      alias map (requirement 20.10).
    - ``kind``: the entity or bill it is a record of, and ``bill`` which of the two.
    - ``config_path``: ``entities.<alias>``, the place an error about this record names.
    - ``fields``: the written values of the record, of the header for a bill, in written
      order.
    - ``lines``: the lines of a bill, in written order.
    - ``source`` and ``conversion``: the alias of the source bill and the id of the
      conversion that generates this record, both None for a record created on its own
      (requirement 20.2).
    - ``status`` and ``actions``: the state the record is to reach and the state operations
      that reach it, from draft and in order; no status leaves the bill a draft, and a
      ``closed`` one ends in a close that counts as manual (requirement 20.2).
    """

    alias: str
    kind: str
    config_path: str
    bill: bool = False
    fields: tuple[PlanField, ...] = ()
    lines: tuple[PlanLine, ...] = ()
    source: str | None = None
    conversion: str | None = None
    status: BillStatus | None = None
    actions: tuple[StateAction, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanBulk:
    """One item of ``bulk``: ``count`` records of the entity ``kind`` (requirement 20.4).

    ``fields`` are the fields a business pack binds a generator to in
    ``seed/generators.yaml``, in the order the packs and their bindings are written, which
    is the order the build draws them in; every other field of such a record takes its
    default, and none of them gets an alias.
    """

    kind: str
    count: int
    config_path: str
    fields: tuple[PlanField, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """A dataset definition as the build walks it (design.md component 6.1).

    - ``dataset``, ``version`` and ``random_seed``: what the definition calls itself, and
      the seed of the one random sequence of the build (requirement 20.3).
    - ``file``: the file of the definition, which an error of the build names.
    - ``closed_before``: the parsed ``periods.closed_before``; the build evaluates it at
      the anchor date to get the first open period (requirements 20.1 and 20.10).
    - ``include_shared``: the packs whose Shared_Data the build creates first, in written
      order (requirement 20.17).
    - ``current_user``: the current user of the build context, the dataset's own or the one
      of instance.yaml, None when neither declares one (requirement 20.5).
    - ``records`` and ``bulk``: the records of ``entities`` and the items of ``bulk``, each
      in written order (requirement 20.17).
    """

    dataset: str
    version: str
    file: str
    random_seed: int
    closed_before: MonthPeriod
    include_shared: tuple[str, ...] = ()
    current_user: str | None = None
    records: tuple[PlanRecord, ...] = ()
    bulk: tuple[PlanBulk, ...] = ()


def status_actions(bill: BillModel, status: BillStatus) -> tuple[StateAction, ...]:
    """The state operations that take ``bill`` from draft to ``status`` (req 20.2).

    The forward operations of the bill's own preset state machine, in order: ``submit`` for
    a submitted standard bill, ``submit`` and ``approve`` for an approved one, those two
    and ``close`` for a closed one, and ``approve`` alone for an approved simple bill; a
    draft needs none. Raises ValueError for a state that machine does not have, which
    :func:`resolve` reports as an error of the dataset before it gets here.
    """
    actions: list[StateAction] = []
    current: BillStatus = DRAFT
    while current != status:
        action = _FORWARD.get((bill.state_machine, current))
        if action is None:
            raise ValueError(f"{status} 不是 {bill.state_machine} 状态机的状态")
        actions.append(action)
        current = TRANSITIONS[bill.state_machine, action].post
    return tuple(actions)


def resolve(
    dataset: DatasetDef,
    domain: DomainModel,
    packs: Sequence[RawPack] = (),
    *,
    file: str | None = None,
    instance_user: str | None = None,
) -> SeedPlan:
    """Resolve ``dataset`` against the configuration and return the plan of its build.

    ``domain`` is the resolved configuration of the instance and ``packs`` its business
    packs, in the order instance.yaml references them, which the generators of ``bulk`` are
    read from. ``file`` is the file of the definition, as an error names it
    (:func:`dataset_file` when the caller holds the document alone), and ``instance_user``
    the ``current_user`` of instance.yaml, which the dataset's own overrides.

    Raises :class:`SeedError` at the first problem, which is where the build fails
    (requirement 20.6); the module docstring lists what is checked and what is not.
    """
    resolver = _Resolver(dataset_file(dataset) if file is None else file, domain, packs)
    return resolver.plan(dataset, instance_user)


class _Resolver:
    """One run of the pass: the file errors name, the configuration, and what is resolved.

    ``_declared`` holds every alias of the definition and ``_records`` those resolved so
    far, which is how an alias used before it is declared is told from one that does not
    exist at all (requirement 20.6).
    """

    __slots__ = ("_file", "_domain", "_packs", "_declared", "_records")

    def __init__(self, file: str, domain: DomainModel, packs: Sequence[RawPack]) -> None:
        self._file = file
        self._domain = domain
        self._packs = tuple(packs)
        self._declared: frozenset[str] = frozenset()
        self._records: dict[str, PlanRecord] = {}

    # --- the definition ------------------------------------------------------------------

    def plan(self, dataset: DatasetDef, instance_user: str | None) -> SeedPlan:
        """The plan of ``dataset``; its keys are checked in the order they are written."""
        self._declared = frozenset(dataset.entities)
        current_user = self._current_user(dataset.current_user, instance_user)
        closed_before = self._period(dataset.periods.closed_before)
        records = tuple(self._record(alias, item) for alias, item in dataset.entities.items())
        bulk = tuple(self._bulk(index, item) for index, item in enumerate(dataset.bulk))
        return SeedPlan(
            dataset=dataset.dataset,
            version=dataset.version,
            file=self._file,
            random_seed=dataset.random_seed,
            closed_before=closed_before,
            include_shared=tuple(dataset.include_shared),
            current_user=current_user,
            records=records,
            bulk=bulk,
        )

    def error(self, config_path: str | None, reason: str) -> SeedError:
        """A :class:`SeedError` at ``config_path`` of the dataset definition."""
        return SeedError(self._file, config_path, reason)

    def _current_user(self, own: str | None, instance_user: str | None) -> str | None:
        """The current user of the build context (requirements 20.5 and 20.16).

        The dataset's own ``current_user`` overrides the one of instance.yaml. It is read as
        an internal id and never as an alias or a code, so it carries the id prefix of the
        user context entity; that the record exists is checked by loading step 8, which has
        the built data (requirement 2.10). A value that comes from instance.yaml is not
        checked here: it is no item of this file.
        """
        if own is None:
            return instance_user
        entity = self._domain.user_entity
        if entity is None:
            # No single user context entity: step 5 reported that (requirement 1.14).
            return own
        prefix = f"{entity.id_prefix}_"
        if not own.startswith(prefix):
            reason = _NOT_USER_ID.format(value=own, entity=entity.name, prefix=prefix)
            raise self.error(CURRENT_USER_KEY, reason)
        return own

    def _period(self, text: str) -> MonthPeriod:
        """``periods.closed_before`` as a parsed period expression (requirement 20.6)."""
        try:
            expr = parse_date_expr(text, Category.PERIOD)
        except DateExprError as error:
            reason = _NOT_PERIOD.format(value=text, position=error.position)
            raise self.error(PERIODS_PATH, reason) from None
        if isinstance(expr, MonthPeriod):
            return expr
        # month(N) is the only expression of the period category, so a parsed expression of
        # that category is one; the reason stays the one of a text that is no period.
        raise self.error(PERIODS_PATH, _NOT_PERIOD.format(value=text, position=1))

    # --- one record ----------------------------------------------------------------------

    def _record(self, alias: str, record: DatasetRecord) -> PlanRecord:
        """One record of ``entities``, resolved and registered under its alias."""
        base = f"{ENTITIES_KEY}.{alias}"
        kind = self._kind(base, record)
        bill = self._domain.bills.get(kind)
        if bill is not None:
            level = bill.header
        else:
            entity = self._domain.entities.get(kind)
            if entity is None:
                raise self.error(f"{base}.{KIND_KEY}", _UNKNOWN_KIND.format(kind=kind))
            level = entity.fields
        prefix = alias.partition(":")[0]
        if prefix != kind:
            raise self.error(base, _ALIAS_PREFIX.format(prefix=prefix, kind=kind))
        lines = self._lines_value(base, kind, record, bill)
        source, conversion = self._source(base, kind, record, bill)
        status, actions = self._status(base, record, bill)
        mapped = _NO_MAPPING if conversion is None else conversion.header_mapping
        push = "" if conversion is None else conversion.id
        written = _own_fields(record, bill is not None)
        resolved = PlanRecord(
            alias=alias,
            kind=kind,
            config_path=base,
            bill=bill is not None,
            fields=self._level(base, kind, level, written, mapped=mapped, push=push),
            lines=self._lines(base, kind, lines, bill, source, conversion),
            source=source,
            conversion=None if conversion is None else conversion.id,
            status=status,
            actions=actions,
        )
        self._records[alias] = resolved
        return resolved

    def _kind(self, base: str, record: DatasetRecord) -> str:
        """The reserved ``kind`` of a record, which the document model requires (20.16)."""
        kind = record.get(KIND_KEY)
        if isinstance(kind, str):
            return kind
        raise self.error(f"{base}.{KIND_KEY}", _KIND_NOT_TEXT)

    def _lines_value(
        self, base: str, kind: str, record: DatasetRecord, bill: BillModel | None
    ) -> tuple[Mapping[str, FieldValue], ...]:
        """The lines of a record: the one list it may write, and only a bill has lines."""
        lines: tuple[Mapping[str, FieldValue], ...] = ()
        for key, value in record.items():
            if isinstance(value, tuple):
                if key != LINES_KEY:
                    raise self.error(f"{base}.{key}", _LINES_UNDER.format(key=key))
                lines = value
        if lines and bill is None:
            raise self.error(f"{base}.{LINES_KEY}", _ENTITY_LINES.format(kind=kind))
        return lines

    def _source(
        self, base: str, kind: str, record: DatasetRecord, bill: BillModel | None
    ) -> tuple[str | None, ConversionModel | None]:
        """The source bill of a pushed record and the conversion that generates it.

        ``source`` is an alias declared before this record whose kind is a bill, and
        exactly one conversion of the instance goes from that bill to this record's kind
        (requirement 20.6). Both are None for a record the build creates on its own.
        """
        if SOURCE_KEY not in record:
            return None, None
        path = f"{base}.{SOURCE_KEY}"
        if bill is None:
            raise self.error(path, _SOURCE_NOT_BILL.format(kind=kind))
        alias = record[SOURCE_KEY]
        if not isinstance(alias, str):
            raise self.error(path, _SOURCE_VALUE)
        found = self._alias(path, alias)
        if not found.bill:
            raise self.error(path, _SOURCE_IS_ENTITY.format(alias=alias, kind=found.kind))
        matches = tuple(
            item
            for item in self._domain.conversions.values()
            if item.source == found.kind and item.target == bill.name
        )
        if not matches:
            raise self.error(path, _NO_CONVERSION.format(source=found.kind, target=bill.name))
        if len(matches) > 1:
            reason = _MANY_CONVERSIONS.format(
                source=found.kind,
                target=bill.name,
                count=len(matches),
                ids="、".join(item.id for item in matches),
            )
            raise self.error(path, reason)
        return alias, matches[0]

    def _status(
        self, base: str, record: DatasetRecord, bill: BillModel | None
    ) -> tuple[BillStatus | None, tuple[StateAction, ...]]:
        """The state a bill record reaches and the operations that reach it (req 20.2).

        ``status`` is a reserved key of a bill record: the state of a bill is written by the
        engine, so the dataset declares which operations to run and the state has to be one
        of the bill's own preset state machine (requirement 20.6). On a record of an entity
        whose ``has_status`` is true the same key is the writable standard field, which the
        field pass takes.
        """
        if bill is None or STATUS_KEY not in record:
            return None, ()
        path = f"{base}.{STATUS_KEY}"
        written = record[STATUS_KEY]
        if not isinstance(written, str):
            raise self.error(path, _STATUS_VALUE)
        status = next((state for state in bill.statuses if state == written), None)
        if status is None:
            reason = _UNKNOWN_STATUS.format(
                status=written,
                bill=bill.name,
                machine=bill.state_machine,
                states="、".join(bill.statuses),
            )
            raise self.error(path, reason)
        return status, status_actions(bill, status)

    def _lines(
        self,
        base: str,
        kind: str,
        lines: Sequence[Mapping[str, FieldValue]],
        bill: BillModel | None,
        source: str | None,
        conversion: ConversionModel | None,
    ) -> tuple[PlanLine, ...]:
        """The lines of a bill record, in written order."""
        if bill is None:
            return ()
        return tuple(
            self._line(f"{base}.{LINES_KEY}[{index}]", kind, line, bill, source, conversion)
            for index, line in enumerate(lines)
        )

    def _line(
        self,
        base: str,
        kind: str,
        line: Mapping[str, FieldValue],
        bill: BillModel,
        source: str | None,
        conversion: ConversionModel | None,
    ) -> PlanLine:
        """One line of a bill record: its ``source_line`` and its written values."""
        source_line = self._source_line(base, line, source, conversion)
        if conversion is not None and source_line is None:
            raise self.error(base, _SOURCE_LINE_MISSING)
        written = ((key, value) for key, value in line.items() if key != SOURCE_LINE_KEY)
        inputs = None if conversion is None else _line_inputs(conversion)
        push = "" if conversion is None else conversion.id
        return PlanLine(
            config_path=base,
            fields=self._level(base, kind, bill.lines, written, inputs=inputs, push=push),
            source_line=source_line,
        )

    def _source_line(
        self,
        base: str,
        line: Mapping[str, FieldValue],
        source: str | None,
        conversion: ConversionModel | None,
    ) -> int | None:
        """The line number of the source line a pushed line comes from (req 20.16).

        A positive integer, and the source bill has at least that many lines, which the
        record of its alias says; the build turns the number into the internal id of that
        line, which is what a push takes (``source_line_id``).
        """
        if SOURCE_LINE_KEY not in line:
            return None
        path = f"{base}.{SOURCE_LINE_KEY}"
        if conversion is None or source is None:
            raise self.error(path, _SOURCE_LINE_ONLY_PUSH)
        written = line[SOURCE_LINE_KEY]
        # A bool is an int to Python, but it is no line number.
        if type(written) is not int or written < 1:
            raise self.error(path, _SOURCE_LINE_VALUE)
        count = len(self._records[source].lines)
        if written > count:
            raise self.error(
                path, _SOURCE_LINE_RANGE.format(line_no=written, alias=source, count=count)
            )
        return written

    # --- the written values of one level -------------------------------------------------

    def _level(
        self,
        base: str,
        kind: str,
        fields: Mapping[str, FieldModel],
        written: Iterable[tuple[str, FieldValue]],
        *,
        mapped: Mapping[str, str] = _NO_MAPPING,
        inputs: frozenset[str] | None = None,
        push: str = "",
    ) -> tuple[PlanField, ...]:
        """The written values of a record, of a header or of one line, in written order.

        ``mapped`` are the fields the conversion of a push fills from the source, which the
        record must not write (requirement 20.6); ``inputs`` are the line inputs such a push
        takes, None for a level that is not pushed, since a push reads nothing else from a
        line and a value written there would be dropped without a word; ``push`` is the id
        of that conversion, which both reasons name.
        """
        resolved: list[PlanField] = []
        given: set[str] = set()
        for key, value in written:
            path = f"{base}.{key}"
            field = self._field(path, kind, fields, key)
            if field.name in mapped:
                raise self.error(path, _MAPPED_FIELD.format(field=field.name, conversion=push))
            if not field.writable:
                raise self.error(path, _READONLY_FIELD.format(field=field.name))
            if inputs is not None and field.name not in inputs:
                reason = _NOT_PUSH_INPUT.format(
                    field=field.name, conversion=push, inputs="、".join(sorted(inputs))
                )
                raise self.error(path, reason)
            resolved.append(PlanField(field, key, self._value(path, field, value, given)))
            given.add(field.name)
        return tuple(resolved)

    def _field(
        self, path: str, kind: str, fields: Mapping[str, FieldModel], key: str
    ) -> FieldModel:
        """The field a written key names (requirement 20.16).

        A ref field is written without its ``_id`` suffix (``vendor`` for ``vendor_id``) and
        a field named ``kind``, whose name the reserved key of a record takes, as
        ``<kind value>_kind`` (``material_kind`` on a record of material). The name of the
        field itself comes first, so an object that has a field named like one of those
        spellings keeps it.
        """
        field = lookup_field(fields, key)
        if field is None and key == f"{kind}{KIND_SUFFIX}":
            field = lookup_field(fields, KIND_KEY)
        if field is not None:
            return field
        direct = fields.get(key)
        if direct is not None and direct.type == REF_TYPE:
            written = key.removesuffix(ID_SUFFIX)
            raise self.error(path, _REF_SPELLING.format(field=key, written=written))
        if lookup_field(fields, KIND_KEY) is not None:
            written = f"{kind}{KIND_SUFFIX}"
            raise self.error(path, _KIND_SPELLING.format(kind=kind, field=key, written=written))
        raise self.error(path, _UNKNOWN_FIELD.format(kind=kind, field=key))

    def _value(
        self, path: str, field: FieldModel, value: FieldValue, given: Container[str]
    ) -> PlanValue:
        """One written value, by the type of the field it fills (requirement 20.16)."""
        if field.type == REF_TYPE:
            return self._ref(path, field, value)
        if field.type == DATE_TYPE:
            return self._date(path, field, value)
        if isinstance(value, GeneratorObject):
            problem = _generator_problem(field, value, given)
            if problem is not None:
                raise self.error(path, problem)
            return Drawn(value)
        return Written(value)

    def _ref(self, path: str, field: FieldModel, value: FieldValue) -> AliasId | CodeId:
        """A ref value: an alias of the dataset, or a code the build resolves (req 20.16).

        A value of the form ``<kind>:<name>`` is an alias, which has to be declared before
        this value and to name a record of the entity the field refers to; any other text is
        the code of a record of that entity, which the build looks up among the records it
        created.
        """
        if isinstance(value, GeneratorObject):
            raise self.error(path, _GENERATOR_REF.format(field=field.name))
        if not isinstance(value, str):
            raise self.error(path, _REF_VALUE.format(field=field.name))
        target = field.ref
        if target is None:
            raise self.error(path, _NO_REF_TARGET.format(field=field.name))
        if _ALIAS.fullmatch(value) is None:
            return CodeId(entity=target, code=value)
        found = self._alias(path, value)
        if found.kind != target:
            reason = _ALIAS_KIND.format(alias=value, kind=found.kind, field=field.name, ref=target)
            raise self.error(path, reason)
        return AliasId(value)

    def _alias(self, path: str, alias: str) -> PlanRecord:
        """The record ``alias`` names, which has to be declared before this point."""
        found = self._records.get(alias)
        if found is not None:
            return found
        if alias in self._declared:
            raise self.error(path, _LATE_ALIAS.format(alias=alias))
        raise self.error(path, _UNKNOWN_ALIAS.format(alias=alias))

    def _date(self, path: str, field: FieldModel, value: FieldValue) -> DateAt:
        """A date value: an expression of the date category (requirement 20.6).

        Dates are written relative to the anchor date, which the build evaluates them at, so
        a date of the calendar is not one of them (seed-data.md section 4).
        """
        if isinstance(value, GeneratorObject):
            raise self.error(path, _GENERATOR_DATE.format(field=field.name))
        if not isinstance(value, str):
            raise self.error(path, _DATE_VALUE.format(field=field.name))
        try:
            expr = parse_date_expr(value, Category.DATE)
        except DateExprError as error:
            reason = _NOT_DATE.format(value=value, position=error.position)
            raise self.error(path, reason) from None
        if isinstance(expr, MonthPeriod):
            # month(N) belongs to the period category, which this call did not ask for.
            raise self.error(path, _NOT_DATE.format(value=value, position=1))
        return DateAt(expr)

    # --- one bulk item -------------------------------------------------------------------

    def _bulk(self, index: int, item: BulkDef) -> PlanBulk:
        """One item of ``bulk``: the entity it generates and the generators it draws."""
        base = f"{BULK_KEY}[{index}]"
        path = f"{base}.{KIND_KEY}"
        entity = self._domain.entities.get(item.kind)
        if entity is None:
            reason = _BULK_BILL if item.kind in self._domain.bills else _BULK_NOT_ENTITY
            raise self.error(path, reason.format(kind=item.kind))
        if entity.shared:
            raise self.error(path, _BULK_SHARED.format(kind=item.kind))
        return PlanBulk(
            kind=item.kind,
            count=item.count,
            config_path=base,
            fields=self._bound(entity.name, entity.fields),
        )

    def _bound(self, entity: str, fields: Mapping[str, FieldModel]) -> tuple[PlanField, ...]:
        """The generators the packs bind to the fields of ``entity`` (requirement 20.4).

        The bindings of ``seed/generators.yaml``, in the order the packs and their keys are
        written, which is the order the build draws them in. A binding names the field by
        its own name (``article.base_measure_id``), so the spellings of a dataset record do
        not apply here; the field has to exist, to be writable and to take the kind of value
        the generator draws. An error of a binding names the file of that pack,
        ``<pack>/seed/generators.yaml``, and not the dataset.
        """
        resolved: list[PlanField] = []
        given: dict[str, str] = {}
        for pack in self._packs:
            document = pack.generators
            if document is None:
                continue
            file = f"{pack.name}/{document.file}"
            for key, bound, decl in _bindings(document.document.generators, entity):
                path = f"generators.{key}"
                field = fields.get(bound)
                if bound in given:
                    first = given[bound]
                    reason = _DOUBLE_BINDING.format(field=key, first=first, second=pack.name)
                    raise SeedError(file, path, reason)
                if field is None:
                    raise SeedError(file, path, _UNKNOWN_FIELD.format(kind=entity, field=bound))
                if not field.writable:
                    raise SeedError(file, path, _READONLY_FIELD.format(field=bound))
                if field.type == REF_TYPE:
                    raise SeedError(file, path, _GENERATOR_REF.format(field=bound))
                if field.type == DATE_TYPE:
                    raise SeedError(file, path, _GENERATOR_DATE.format(field=bound))
                problem = _generator_problem(field, decl, given)
                if problem is not None:
                    raise SeedError(file, path, problem)
                resolved.append(PlanField(field, key, Drawn(decl)))
                given[bound] = pack.name
        return tuple(resolved)


def _generator_problem(
    field: FieldModel, decl: GeneratorObject, given: Container[str]
) -> str | None:
    """Why ``decl`` cannot fill ``field``, or None when it can (requirements 20.6, 4.7).

    The parameter models of the pack layer checked each parameter on its own, so what is
    left is :func:`~erp_mock.seed.generators.param_problem`, the name ``short_name`` derives
    from - a value the same record has to be given before it - and the precision
    ``decimal_range`` draws in: the generator draws whole steps of the last place its bounds
    are written with (:class:`~erp_mock.seed.generators.ValueGenerators`), so a bound with
    more places than the field keeps would draw values the engine rounds away.
    """
    problem = param_problem(decl)
    if problem is not None:
        return _GENERATOR_PARAM.format(gen=decl.gen, problem=problem)
    if isinstance(decl, ShortNameGenerator) and NAME_FIELD not in given:
        return _SHORT_NAME_NEEDS_NAME
    if not isinstance(decl, DecimalRangeGenerator):
        return None
    limit = _field_places(field.type)
    if limit is None:
        return _GENERATOR_TYPE.format(field=field.name, type=field.type)
    places = max(_places(decl.min), _places(decl.max))
    if places > limit:
        return _DECIMAL_PLACES.format(places=places, field=field.name, type=field.type, limit=limit)
    return None


def _bindings(
    generators: Mapping[str, GeneratorObject], entity: str
) -> Iterator[tuple[str, str, GeneratorObject]]:
    """The bindings of ``generators`` that name a field of ``entity``, in written order.

    Each one as ``(<entity>.<field>, <field>, generator)``; the key of a binding is the
    field path of design.md data model 1.
    """
    for key, decl in generators.items():
        name, _, bound = key.partition(".")
        if name == entity:
            yield key, bound, decl


def lookup_field(fields: Mapping[str, FieldModel], key: str) -> FieldModel | None:
    """The field ``key`` names: the field itself, or the ref field ``<key>_id`` (req 20.16).

    The one spelling rule a record of a business pack's ``seed/shared/`` shares with a
    record of a dataset, so the builder reads the Shared_Data through this function
    (:mod:`erp_mock.seed.builder`); the ``<kind value>_kind`` spelling is applied on top of
    it by the caller (:data:`KIND_SUFFIX`).
    """
    field = fields.get(key)
    if field is not None and field.type != REF_TYPE:
        return field
    ref = fields.get(f"{key}{ID_SUFFIX}")
    if ref is not None and ref.type == REF_TYPE:
        return ref
    return None


def _own_fields(record: DatasetRecord, bill: bool) -> Iterator[tuple[str, FieldValue]]:
    """The written field values of a record: everything but its reserved keys.

    ``status`` is reserved on a bill, where it declares the state operations to run, and is
    a written value on a record of an entity whose ``has_status`` is true, where it is the
    writable standard field of requirement 5.1.
    """
    reserved = {KIND_KEY, SOURCE_KEY, LINES_KEY} | ({STATUS_KEY} if bill else set())
    for key, value in record.items():
        if key in reserved or isinstance(value, tuple):
            continue
        yield key, value


def _line_inputs(conversion: ConversionModel) -> frozenset[str]:
    """The fields a pushed line may write: the line inputs but ``source_line_id``.

    The line of a push carries the ``required_line_inputs`` of the conversion and nothing
    else (requirement 8.1); the source line itself is written as ``source_line``, the line
    number (requirement 20.16).
    """
    return frozenset(conversion.required_line_inputs) - {SOURCE_LINE_ID}


def _field_places(field_type: str) -> int | None:
    """The decimal places ``field_type`` keeps, None when it is no decimal type (req 4.7).

    :data:`~erp_mock.common.decimals.PLACES` holds the four decimal kinds, whose names are
    those field types.
    """
    for kind, places in PLACES.items():
        if kind == field_type:
            return places
    return None


def _places(text: str) -> int:
    """Number of written decimals of a decimal string.

    The step ``decimal_range`` draws in is the last place its bounds are written with
    (:class:`~erp_mock.seed.generators.ValueGenerators`), so this is the precision such a
    generator produces.
    """
    return len(text.partition(".")[2])
