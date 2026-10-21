"""Step 5, part one: the domain model and the references between the packs (req 2.9).

The fifth step is the first one that reads the configuration of the packs as a whole. This
module builds the :class:`~erp_mock.config.model.DomainModel` the steps after it work on
(design.md 2.4) and, while doing so, resolves every name a document writes: the target of a
ref field, a ``default_from`` path, the source, target and mapped fields of a conversion,
the conversion and target field of a writeback, the bill and fields of a linkage and of a
rule, and the source, parameters and returned fields of a declarative query.

What a reference may reach is the pack's own configuration and the configuration of the
packs its ``depends_on`` lists directly, never a pack reached only through another one
(requirement 2.9, business-pack.md section 5). A name that neither offers is one reference
integrity error at the item that writes it, naming what could not be resolved; the checks
that build on that reference are skipped while the rest of the step goes on collecting its
errors (requirement 2.2), so a conversion whose source does not resolve still has the
fields of its target resolved, and the model keeps None where a reference failed instead of
a name that was never there.

The field view is what the step leaves behind (requirements 1.3 to 1.5). For every entity
and every level of every bill it merges the engine standard fields with the declared ones:

- the standard fields of the level come first, in the order of
  :mod:`erp_mock.config.model`, and last among them the link field of each push whose
  source the bill is: ``linked_qty`` on its lines when the push's limit_by is qty,
  ``linked_amount`` on its header when it is amount, or the field the writeback of that
  conversion names instead. Those fields come from the conversions, which name bills, so
  the step resolves the sources of the conversions on their own first
  (:func:`_link_fields`) and builds the bills afterwards;
- a declaration with the name of a standard field adds no field of its own: it gives that
  field its label and, when the field is writable, its defaults (requirement 1.5). What
  such a declaration may say at all is checked by ``loader.step5_fields``
  (requirement 1.6), so a default on a read-only standard field is reported there and
  ignored here, as is a declaration without ``type`` whose name is no standard field;
- the declared fields follow, in declaration order.

What is not this module's: namespaces (requirement 2.7), where a name declared twice keeps
its first declaration here - in the order of instance.yaml and then of the file path - and
``loader.step5_namespace`` reports the conflict; the types of the fields a reference
reaches, which ``loader.step5_fields`` checks (requirements 1.6, 6.12, 9.3 and 9.11); and
the rule expressions, which ``loader.step5_expr`` checks (requirement 10.2). A reference
here is resolved as soon as the name exists, whatever it names.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from erp_mock.common.frozen import FrozenMap
from erp_mock.config.fields import DecimalField, EnumField, RefField, StandardOverride, TypedField
from erp_mock.config.model import (
    LINE_SUFFIX,
    LINKED_AMOUNT,
    LINKED_QTY,
    BillModel,
    ConversionModel,
    DefaultFrom,
    DomainModel,
    EntityModel,
    FieldLevel,
    FieldModel,
    FieldRef,
    LinkageModel,
    QueryModel,
    QueryParamModel,
    RuleModel,
    SearchModel,
    SortRef,
    UnitConversionModel,
    WritebackModel,
    entity_standard_view,
    header_standard_view,
    line_standard_view,
)
from erp_mock.config.pack import (
    BillDef,
    ConversionDef,
    DeclarativeQueryDef,
    DescribedRuleDef,
    EnforcedRuleDef,
    EntityDef,
    InventoryLinkageDef,
    LineQuantityDef,
    PrimitiveQueryDef,
    QueryObject,
    RuleObject,
    SearchDef,
    WritebackDef,
)
from erp_mock.loader.errors import StepErrors
from erp_mock.loader.step4 import (
    CONVERSION_KEY,
    LINKAGE_KEY,
    RULE_KEY,
    WRITEBACK_KEY,
    RawDoc,
    RawPack,
)

__all__ = ["build"]

# How a configuration writes a field of the header or of the lines of a bill
# (:data:`~erp_mock.config.pack.FieldPath`), and how a writeback writes a field of the
# source bill or of one of its lines (:data:`~erp_mock.config.pack.WritebackTarget`).
_PATH_LEVELS: Final[Mapping[str, FieldLevel]] = {
    "header": "header",
    "line": "line",
    "source": "header",
    "source_line": "line",
}
# The two levels of a bill, in the order a rule's detail field is looked up in.
_BILL_LEVELS: Final[tuple[FieldLevel, ...]] = ("header", "line")
# The direction of a sort key; no direction means ascending (requirement 5.14).
_DESCENDING: Final = "desc"
# The suffix of the name of a ref field (requirement 1.12), which a ``default_from`` path
# writes without.
_ID_SUFFIX: Final = "_id"

# What a reference names, for the reason of an error about it.
_ENTITY: Final = "实体"
_BILL: Final = "单据"
_OBJECT: Final = "实体或单据"
_CONVERSION: Final = "下推配置"


def build(packs: Sequence[RawPack], errors: StepErrors) -> DomainModel:
    """Build the domain model of ``packs`` and resolve their references (requirement 2.9).

    ``packs`` are the packs instance.yaml references, in its order, with the documents
    step 4 validated. Every name that could not be resolved is recorded in ``errors``; the
    model holds what did resolve, so this part of step 5 passed only when ``errors`` stayed
    empty.
    """
    return _Builder(_Index(packs), errors).build()


# --- what the packs declare --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Decl[D]:
    """One declared object: the document, the pack that declares it and its file."""

    pack: str
    file: str
    document: D


class _Index:
    """What the packs declare, by name, and which packs each pack may reach.

    The declarations are collected in the order of instance.yaml and, inside a pack, of the
    file path, so a name declared twice keeps its first declaration and the model comes out
    the same on every file system (requirement 24). ``visible`` holds, per pack, the packs
    a reference of that pack may reach: the pack itself and the packs its ``depends_on``
    lists directly (requirement 2.9); step 3 has shown those to be packs of this instance.
    """

    __slots__ = ("bills", "conversions", "entities", "org_entity", "packs", "visible", "writebacks")

    def __init__(self, packs: Sequence[RawPack]) -> None:
        self.packs = tuple(packs)
        self.entities = _by_name(self.packs, _entities, lambda doc: doc.entity)
        self.bills = _by_name(self.packs, _bills, lambda doc: doc.bill)
        self.conversions = _by_name(self.packs, _conversions, lambda doc: doc.id)
        self.writebacks = tuple(_declarations(self.packs, _writebacks))
        self.visible: Mapping[str, frozenset[str]] = {
            pack.name: frozenset({pack.name, *pack.manifest.depends_on}) for pack in self.packs
        }
        # The entity the standard ``org_id`` of every bill header refers to: the one entity
        # of the instance whose context role is org. ``loader.step5_namespace`` reports it
        # when there is not exactly one (requirement 1.14), and until then no header knows
        # the target of that field.
        orgs = [
            name
            for name, decl in self.entities.items()
            if decl.document.context is not None and decl.document.context.role == "org"
        ]
        self.org_entity: str | None = orgs[0] if len(orgs) == 1 else None

    def entity(self, name: str, pack: str) -> _Decl[EntityDef] | None:
        """The entity ``name``, if a reference of the pack ``pack`` may reach it."""
        return _reachable(self.entities.get(name), self.visible[pack])

    def bill(self, name: str, pack: str) -> _Decl[BillDef] | None:
        """The bill ``name``, if a reference of the pack ``pack`` may reach it."""
        return _reachable(self.bills.get(name), self.visible[pack])

    def conversion(self, name: str, pack: str) -> _Decl[ConversionDef] | None:
        """The conversion ``name``, if a reference of the pack ``pack`` may reach it."""
        return _reachable(self.conversions.get(name), self.visible[pack])

    def owner(self, name: str) -> str | None:
        """The pack that declares the entity, bill or conversion ``name``, if any does."""
        decl = self.entities.get(name) or self.bills.get(name) or self.conversions.get(name)
        return None if decl is None else decl.pack


# One selector per kind of document, so that the two generic helpers below keep the
# document type of the kind they are given.


def _entities(pack: RawPack) -> Sequence[RawDoc[EntityDef]]:
    return pack.entities


def _bills(pack: RawPack) -> Sequence[RawDoc[BillDef]]:
    return pack.bills


def _conversions(pack: RawPack) -> Sequence[RawDoc[ConversionDef]]:
    return pack.conversions


def _writebacks(pack: RawPack) -> Sequence[RawDoc[WritebackDef]]:
    return pack.writebacks


def _linkages(pack: RawPack) -> Sequence[RawDoc[InventoryLinkageDef]]:
    return pack.linkages


def _rules(pack: RawPack) -> Sequence[RawDoc[RuleObject]]:
    return pack.rules


def _declarations[D](
    packs: Sequence[RawPack], documents: Callable[[RawPack], Sequence[RawDoc[D]]]
) -> Iterator[_Decl[D]]:
    """The documents of one kind of every pack, in pack and then file order."""
    for pack in packs:
        for doc in documents(pack):
            yield _Decl(pack.name, doc.file, doc.document)


def _by_name[D](
    packs: Sequence[RawPack],
    documents: Callable[[RawPack], Sequence[RawDoc[D]]],
    name_of: Callable[[D], str],
) -> dict[str, _Decl[D]]:
    """The documents of one kind by name, keeping the first declaration of a name."""
    found: dict[str, _Decl[D]] = {}
    for decl in _declarations(packs, documents):
        found.setdefault(name_of(decl.document), decl)
    return found


def _reachable[D](decl: _Decl[D] | None, visible: frozenset[str]) -> _Decl[D] | None:
    """``decl``, if the referring pack may reach the pack that declares it."""
    return decl if decl is not None and decl.pack in visible else None


# --- the builder -------------------------------------------------------------------------


class _Builder:
    """Builds the domain model of one instance, recording every name it cannot resolve."""

    __slots__ = ("_errors", "_index", "_levels", "_link")

    def __init__(self, index: _Index, errors: StepErrors) -> None:
        self._index = index
        self._errors = errors
        # The link fields of each bill level, and the field names of every object and
        # level; both are needed while the field views themselves are being built.
        self._link = _link_fields(index)
        self._levels = _field_names(index, self._link)

    def build(self) -> DomainModel:
        """Build the model: the entities and bills first, then what references them."""
        return DomainModel(
            entities=FrozenMap(
                (name, self._entity(decl)) for name, decl in self._index.entities.items()
            ),
            bills=FrozenMap((name, self._bill(decl)) for name, decl in self._index.bills.items()),
            conversions=FrozenMap(
                (name, self._conversion(decl)) for name, decl in self._index.conversions.items()
            ),
            writebacks=self._writebacks(),
            linkages=self._linkages(),
            rules=self._rules(),
            queries=self._queries(),
        )

    # --- resolving a name ----------------------------------------------------------------

    def _report(self, decl: _Decl[object], path: str, reason: str) -> None:
        """Record one error at ``path`` of the document ``decl`` was read from."""
        self._errors.add_at(decl.pack, decl.file, path, reason)

    def _unresolved(self, decl: _Decl[object], path: str, kind: str, name: str) -> None:
        """Record that ``name`` could not be resolved (requirement 2.9).

        A name no pack declares is simply absent; a name another pack declares is there but
        out of reach, which the reason says, since that is what the author has to fix.
        """
        owner = self._index.owner(name)
        if owner is None or owner == decl.pack:
            reason = f"引用的{kind} {name} 不存在"
        else:
            reason = (
                f"引用的{kind} {name} 由业务包 {owner} 声明，"
                f"本包的 depends_on 没有直接声明对它的依赖"
            )
        self._report(decl, path, reason)

    def _entity_name(self, name: str, decl: _Decl[object], path: str) -> str | None:
        """Resolve ``name`` as an entity the referring pack may reach."""
        if self._index.entity(name, decl.pack) is not None:
            return name
        self._unresolved(decl, path, _ENTITY, name)
        return None

    def _bill_name(self, name: str, decl: _Decl[object], path: str) -> str | None:
        """Resolve ``name`` as a bill the referring pack may reach."""
        if self._index.bill(name, decl.pack) is not None:
            return name
        self._unresolved(decl, path, _BILL, name)
        return None

    def _object_name(self, name: str, decl: _Decl[object], path: str) -> str | None:
        """Resolve ``name`` as an entity or a bill, the targets a ref field may name."""
        if self._index.entity(name, decl.pack) or self._index.bill(name, decl.pack):
            return name
        self._unresolved(decl, path, _OBJECT, name)
        return None

    def _field(
        self, name: str, obj: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> FieldRef | None:
        """Resolve ``name`` as a field of ``level`` of the entity or bill ``obj``.

        ``obj`` is a name an earlier reference resolved, so only the field is looked up
        here, against the names of the view that level will have.
        """
        if name in self._levels.get((obj, level), frozenset()):
            return FieldRef(level, name)
        self._report(decl, path, _no_field(obj, level, name))
        return None

    def _own_field(
        self, name: str, obj: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> str | None:
        """Resolve a field of ``obj`` at a known level, returning the name alone."""
        ref = self._field(name, obj, level, decl, path)
        return None if ref is None else ref.name

    def _fields(
        self, names: Sequence[str], obj: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> tuple[FieldRef, ...]:
        """Resolve a list of field names, each reported at its own index."""
        found = (
            self._field(name, obj, level, decl, f"{path}[{index}]")
            for index, name in enumerate(names)
        )
        return tuple(field for field in found if field is not None)

    def _path(self, text: str, bill: str, decl: _Decl[object], path: str) -> FieldRef | None:
        """Resolve ``header.<field>``, ``line.<field>`` and the two ``source`` forms."""
        prefix, _, name = text.partition(".")
        return self._field(name, bill, _PATH_LEVELS[prefix], decl, path)

    def _sort(
        self, keys: Sequence[str], obj: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> tuple[SortRef, ...]:
        """Resolve the sort keys of a search: ``<field>`` or ``<field> desc`` (req 5.14)."""
        found: list[SortRef] = []
        for index, key in enumerate(keys):
            name, _, direction = key.partition(" ")
            field = self._field(name, obj, level, decl, f"{path}[{index}]")
            if field is not None:
                found.append(SortRef(field, direction == _DESCENDING))
        return tuple(found)

    def _search(
        self, search: SearchDef | None, obj: str, level: FieldLevel, decl: _Decl[object]
    ) -> SearchModel | None:
        """Resolve the ``search`` of an entity or a bill (requirements 5.9 to 5.14).

        The fields of a bill's search are fields of its header: a search matches, filters,
        sorts and summarizes bills, not lines (requirements 5.9 and 5.10).
        """
        if search is None:
            return None
        return SearchModel(
            keyword_fields=self._fields(
                search.keyword_fields, obj, level, decl, "search.keyword_fields"
            ),
            filters=self._fields(search.filters, obj, level, decl, "search.filters"),
            default_sort=self._sort(search.default_sort, obj, level, decl, "search.default_sort"),
        )

    # --- entities ------------------------------------------------------------------------

    def _entity(self, decl: _Decl[EntityDef]) -> EntityModel:
        """Build the model of one entity, resolving every name it writes."""
        entity = decl.document
        name = entity.entity
        context = entity.context
        flag: str | None = None
        if context is not None and context.default_flag is not None:
            flag = self._own_field(
                context.default_flag, name, "record", decl, "context.default_flag"
            )
        standard = entity_standard_view(code=entity.code is not None, has_status=entity.has_status)
        return EntityModel(
            name=name,
            pack=decl.pack,
            file=decl.file,
            id_prefix=entity.id_prefix,
            fields=self._level(decl, entity.fields, standard, "fields"),
            label=entity.label,
            code=entity.code,
            shared=entity.shared,
            has_status=entity.has_status,
            context=None if context is None else context.role,
            default_flag=flag,
            unit_conversion=self._unit_conversion(decl),
            operations=tuple(entity.operations),
            search=self._search(entity.search, name, "record", decl),
            declared_fields=tuple(entity.fields),
        )

    def _unit_conversion(self, decl: _Decl[EntityDef]) -> UnitConversionModel | None:
        """Resolve the ``unit_conversion`` of the measured entity (requirement 6.8).

        It names a field of this entity and the entity that holds the conversion records
        with three fields of its own. The unit entity is what the base unit field refers
        to; ``loader.step5_fields`` checks the types of all five (requirement 6.12), so a
        base unit field that is no ref field leaves the unit entity unknown. One name that
        does not resolve leaves the entity without a unit conversion, since the primitive
        could not read a conversion record with it.
        """
        declared = decl.document.unit_conversion
        if declared is None:
            return None
        name = decl.document.entity
        base = self._own_field(
            declared.base_unit_field, name, "record", decl, "unit_conversion.base_unit_field"
        )
        records = self._entity_name(declared.entity, decl, "unit_conversion.entity")
        fields = {
            "material_field": declared.material_field,
            "unit_field": declared.unit_field,
            "factor_field": declared.factor_field,
        }
        resolved = {
            key: (
                None
                if records is None
                else self._own_field(field, records, "record", decl, f"unit_conversion.{key}")
            )
            for key, field in fields.items()
        }
        if base is None or records is None or not all(resolved.values()):
            return None
        declared_base = decl.document.fields.get(declared.base_unit_field)
        return UnitConversionModel(
            base_unit_field=base,
            unit_entity=declared_base.ref if isinstance(declared_base, RefField) else None,
            entity=records,
            material_field=fields["material_field"],
            unit_field=fields["unit_field"],
            factor_field=fields["factor_field"],
        )

    # --- bills ---------------------------------------------------------------------------

    def _bill(self, decl: _Decl[BillDef]) -> BillModel:
        """Build the model of one bill: both field views and every name it writes."""
        bill = decl.document
        name = bill.bill
        header = header_standard_view(
            pricing=bill.pricing,
            state_machine=bill.state_machine,
            org_entity=self._index.org_entity,
            link_fields=self._link.get((name, "header"), ()),
        )
        lines = line_standard_view(
            pricing=bill.pricing,
            line_quantity=bill.line_quantity is not None,
            link_fields=self._link.get((name, "line"), ()),
        )
        return BillModel(
            name=name,
            pack=decl.pack,
            file=decl.file,
            id_prefix=bill.id_prefix,
            number_prefix=bill.number.prefix,
            state_machine=bill.state_machine,
            header=self._level(decl, bill.header, header, "header"),
            lines=self._level(decl, bill.lines, lines, "lines"),
            label=bill.label,
            pricing=bill.pricing,
            partner_field=self._partner(decl),
            line_quantity=self._line_quantity(decl),
            operations=tuple(bill.operations),
            search=self._search(bill.search, name, "header", decl),
            header_declared_fields=tuple(bill.header),
            line_declared_fields=tuple(bill.lines),
        )

    def _partner(self, decl: _Decl[BillDef]) -> str | None:
        """Resolve the partner field of a bill (meta-model.md section 4)."""
        partner = decl.document.partner
        if partner is None:
            return None
        return self._own_field(partner.field, decl.document.bill, "header", decl, "partner.field")

    def _line_quantity(self, decl: _Decl[BillDef]) -> LineQuantityDef | None:
        """Resolve the quantity semantics of the lines (requirements 1.4 and 6.6).

        The declaration names fields of the lines; only a bill without pricing names the
        quantity field, a pricing bill measuring its standard ``qty``. The types and the
        combination with pricing are checked by ``loader.step5_fields``
        (requirement 6.12), so the declaration is kept as written once its names resolve.
        """
        quantity = decl.document.line_quantity
        if quantity is None:
            return None
        name = decl.document.bill
        names = {
            "material_field": quantity.material_field,
            "unit_field": quantity.unit_field,
        }
        if quantity.qty_field is not None:
            names["qty_field"] = quantity.qty_field
        resolved = [
            self._own_field(field, name, "line", decl, f"line_quantity.{key}")
            for key, field in names.items()
        ]
        return quantity if all(resolved) else None

    # --- one level of fields -------------------------------------------------------------

    def _level(
        self,
        decl: _Decl[object],
        declared: Mapping[str, TypedField | StandardOverride],
        standard: Sequence[FieldModel],
        path: str,
    ) -> FrozenMap[str, FieldModel]:
        """The field view of one level: the standard fields, then the declared ones.

        A declaration with the name of a standard field is merged into that field
        (:func:`_merge`) and adds none of its own; a declaration without ``type`` whose
        name is no standard field is left out and reported by ``loader.step5_fields``.
        ``path`` is the config path of the level (``header``, ``fields``), so an error
        about a field is reported at ``<path>.<field>.<item>``.
        """
        fields = {field.name: _merge(field, declared.get(field.name)) for field in standard}
        for name, field in declared.items():
            if name not in fields and isinstance(field, TypedField):
                fields[name] = self._declared(field, name, decl, f"{path}.{name}")
        return FrozenMap(
            (name, self._resolve_default_from(field, declared, fields, decl, path))
            for name, field in fields.items()
        )

    def _declared(
        self, declared: TypedField, name: str, decl: _Decl[object], path: str
    ) -> FieldModel:
        """One declared field as a :class:`FieldModel`; ``default_from`` comes afterwards."""
        return FieldModel(
            name=name,
            type=declared.type,
            origin="declared",
            writable=not declared.readonly,
            label=declared.label,
            required=declared.required,
            unique=declared.unique,
            ref=(
                self._object_name(declared.ref, decl, f"{path}.ref")
                if isinstance(declared, RefField)
                else None
            ),
            values=(tuple(declared.values or ()) if isinstance(declared, DecimalField) else ()),
            enum_values=(
                FrozenMap(declared.values) if isinstance(declared, EnumField) else FrozenMap()
            ),
            max_length=getattr(declared, "effective_max_length", None),
            default=declared.default,
            default_context=getattr(declared, "default_context", None),
        )

    def _resolve_default_from(
        self,
        field: FieldModel,
        declared: Mapping[str, TypedField | StandardOverride],
        fields: Mapping[str, FieldModel],
        decl: _Decl[object],
        path: str,
    ) -> FieldModel:
        """Resolve the ``default_from`` of one field of the view (requirements 2.9 and 5.1).

        ``<ref field without _id>.<field>`` names a ref field of the same level, standard
        fields such as ``org_id`` included, and a field of what that ref field refers to. A
        standard field takes the path from its same-name declaration, and only when it is
        writable (requirement 1.5); the order the defaults are applied in is settled by
        ``loader.step5_fields``, which rejects a cycle between them.
        """
        text = _default_from(field, declared.get(field.name))
        if text is None:
            return field
        item = f"{path}.{field.name}.default_from"
        prefix, _, name = text.partition(".")
        ref_name = f"{prefix}{_ID_SUFFIX}"
        reference = fields.get(ref_name)
        if reference is None or reference.type != "ref":
            self._report(decl, item, f"default_from 的 {prefix} 不是本层的 ref 字段 {ref_name}")
            return field
        target = reference.ref
        if target is None:
            # The target of that ref field did not resolve and was reported there.
            return field
        if self._field(name, target, "record", decl, item) is None:
            return field
        return replace(field, default_from=DefaultFrom(ref_name, target, name))

    # --- conversions and writebacks ------------------------------------------------------

    def _conversion(self, decl: _Decl[ConversionDef]) -> ConversionModel:
        """Resolve one conversion: its two bills and every mapped field (requirement 8.1)."""
        conversion = decl.document
        key = CONVERSION_KEY
        source = self._bill_name(conversion.source, decl, f"{key}.source")
        target = self._bill_name(conversion.target, decl, f"{key}.target")
        level: FieldLevel = "line" if conversion.limit_by == "qty" else "header"
        return ConversionModel(
            id=conversion.id,
            pack=decl.pack,
            file=decl.file,
            source=conversion.source,
            target=conversion.target,
            require_source_status=conversion.require_source_status,
            limit_by=conversion.limit_by,
            link_field=FieldRef(level, _link_name(self._index, conversion.id, level)),
            line_selection=conversion.line_selection,
            header_mapping=self._mapping(
                conversion.header_mapping, target, source, "header", decl, key
            ),
            line_mapping=self._mapping(conversion.line_mapping, target, source, "line", decl, key),
            required_line_inputs=self._inputs(conversion, target, decl, key),
        )

    def _inputs(
        self, conversion: ConversionDef, target: str | None, decl: _Decl[ConversionDef], key: str
    ) -> tuple[str, ...]:
        """Resolve ``required_line_inputs``: the fields of a target line the caller fills."""
        if target is None:
            return ()
        path = f"{key}.required_line_inputs"
        fields = self._fields(conversion.required_line_inputs, target, "line", decl, path)
        return tuple(field.name for field in fields)

    def _mapping(
        self,
        mapping: Mapping[str, str],
        target: str | None,
        source: str | None,
        level: FieldLevel,
        decl: _Decl[ConversionDef],
        key: str,
    ) -> FrozenMap[str, str]:
        """Resolve one mapping of a conversion (requirement 8.1).

        The key is a field of the target bill's level and the value ``source.<field>`` or
        ``source_line.<field>`` of the source bill's; the model keeps the pairs whose two
        ends both resolve, with the prefix of the value removed.
        """
        name = "header_mapping" if level == "header" else "line_mapping"
        resolved: dict[str, str] = {}
        for field, text in mapping.items():
            path = f"{key}.{name}.{field}"
            filled = None if target is None else self._field(field, target, level, decl, path)
            read = None if source is None else self._path(text, source, decl, path)
            if filled is not None and read is not None:
                resolved[filled.name] = read.name
        return FrozenMap(resolved)

    def _writebacks(self) -> FrozenMap[str, WritebackModel]:
        """Build the writebacks of every pack, in pack and then file order."""
        return FrozenMap(
            (decl.document.id, self._writeback(decl)) for decl in self._index.writebacks
        )

    def _writeback(self, decl: _Decl[WritebackDef]) -> WritebackModel:
        """Resolve one writeback: its conversion and the source field it keeps (req 8.8).

        The target is a field of the source bill of that conversion, so a conversion that
        does not resolve, or whose source does not, leaves the target as written: that
        reference was reported where it was written.
        """
        writeback = decl.document
        key = WRITEBACK_KEY
        conversion = self._index.conversion(writeback.conversion, decl.pack)
        if conversion is None:
            self._unresolved(decl, f"{key}.conversion", _CONVERSION, writeback.conversion)
        source = (
            None if conversion is None else self._index.bill(conversion.document.source, decl.pack)
        )
        target = (
            None
            if source is None
            else self._path(writeback.target, source.document.bill, decl, f"{key}.target")
        )
        return WritebackModel(
            id=writeback.id,
            pack=decl.pack,
            file=decl.file,
            conversion=writeback.conversion,
            target=target if target is not None else _written_target(writeback),
            value=writeback.value,
            on=writeback.on,
            op=writeback.op,
            reverse_on=writeback.reverse_on,
            auto_close_source_when=writeback.auto_close_source_when,
        )

    # --- linkages, rules and queries -----------------------------------------------------

    def _linkages(self) -> FrozenMap[str, LinkageModel]:
        """Build the inventory linkages of every pack, in pack and then file order."""
        return FrozenMap(
            (decl.document.id, self._linkage(decl))
            for decl in _declarations(self._index.packs, _linkages)
        )

    def _linkage(self, decl: _Decl[InventoryLinkageDef]) -> LinkageModel:
        """Resolve one inventory linkage: its bill and each field it names (requirement 9)."""
        linkage = decl.document
        key = LINKAGE_KEY
        bill = self._bill_name(linkage.bill, decl, f"{key}.bill")
        adjust = linkage.adjust
        return LinkageModel(
            id=linkage.id,
            pack=decl.pack,
            file=decl.file,
            bill=linkage.bill,
            primitive=linkage.primitive,
            direction=linkage.direction,
            qty=self._linkage_field(linkage.qty_field, bill, decl, f"{key}.qty_field"),
            on=linkage.on,
            warehouse=self._linkage_field(
                linkage.warehouse_field, bill, decl, f"{key}.warehouse_field"
            ),
            from_warehouse=self._linkage_field(
                linkage.from_warehouse_field, bill, decl, f"{key}.from_warehouse_field"
            ),
            to_warehouse=self._linkage_field(
                linkage.to_warehouse_field, bill, decl, f"{key}.to_warehouse_field"
            ),
            adjust_field=(
                None
                if adjust is None
                else self._linkage_field(adjust.field, bill, decl, f"{key}.adjust.field")
            ),
            increase=() if adjust is None else tuple(adjust.increase),
            decrease=() if adjust is None else tuple(adjust.decrease),
        )

    def _linkage_field(
        self, text: str | None, bill: str | None, decl: _Decl[object], path: str
    ) -> FieldRef | None:
        """Resolve one field path of a linkage; a bill that did not resolve leaves it out."""
        if text is None or bill is None:
            return None
        return self._path(text, bill, decl, path)

    def _rules(self) -> FrozenMap[str, RuleModel]:
        """Build the rules of every pack, in pack and then file order."""
        return FrozenMap(
            (decl.document.id, self._rule(decl))
            for decl in _declarations(self._index.packs, _rules)
        )

    def _rule(self, decl: _Decl[RuleObject]) -> RuleModel:
        """Resolve one rule: the bill and detail field of an enforced one, or a described one."""
        rule = decl.document
        if isinstance(rule, DescribedRuleDef):
            return RuleModel(
                id=rule.id,
                pack=decl.pack,
                file=decl.file,
                enforcement=rule.enforcement,
                description=rule.description,
                applies_to_tools=tuple(rule.applies_to_tools),
            )
        bill = self._bill_name(rule.bill, decl, f"{RULE_KEY}.bill")
        return RuleModel(
            id=rule.id,
            pack=decl.pack,
            file=decl.file,
            enforcement=rule.enforcement,
            bill=rule.bill,
            on=tuple(rule.on),
            check=rule.check,
            error_code=rule.error_code,
            field=self._rule_field(rule, bill, decl, f"{RULE_KEY}.field"),
            message=rule.message,
        )

    def _rule_field(
        self, rule: EnforcedRuleDef, bill: str | None, decl: _Decl[object], path: str
    ) -> FieldRef | None:
        """Resolve the field of an enforced rule's detail item (requirement 10.7).

        It is a field of the bill's header or of its lines, the header first, since a
        detail item names either ``<field>`` or ``lines[i].<field>``.
        """
        if rule.field is None or bill is None:
            return None
        for level in _BILL_LEVELS:
            if rule.field in self._levels.get((bill, level), frozenset()):
                return FieldRef(level, rule.field)
        self._report(decl, path, f"单据 {bill} 的表头与分录都没有字段 {rule.field}")
        return None

    def _queries(self) -> FrozenMap[str, QueryModel]:
        """Build the queries of every exposure, by tool name, in declaration order."""
        found: dict[str, QueryModel] = {}
        for pack in self._index.packs:
            exposure = pack.exposure
            if exposure is None:
                continue
            decl = _Decl(pack.name, exposure.file, exposure.document)
            for index, query in enumerate(exposure.document.queries):
                found.setdefault(query.tool, self._query(query, decl, f"queries[{index}]"))
        return FrozenMap(found)

    def _query(self, query: QueryObject, decl: _Decl[object], path: str) -> QueryModel:
        """Resolve one query of an exposure: one a primitive provides, or a declared one."""
        if isinstance(query, PrimitiveQueryDef):
            # The primitive defines the parameters and the result (requirement 9.7);
            # ``loader.step5_namespace`` checks that the pack declares that primitive and
            # that it provides that query (requirement 2.6).
            return QueryModel(
                tool=query.tool,
                pack=decl.pack,
                file=decl.file,
                primitive=query.primitive,
                query=query.query,
            )
        return self._declarative(query, decl, path)

    def _declarative(
        self, query: DeclarativeQueryDef, decl: _Decl[object], path: str
    ) -> QueryModel:
        """Resolve a declarative query: source, parameters, sort and returns (req 5.13).

        The source is an entity, a bill or the lines of a bill, written ``<bill>_line``.
        Every path is resolved against it, so a source that does not resolve leaves the
        query with nothing but its tool name and its filter.
        """
        source, level = self._source(query.source, decl, f"{path}.source")
        history = query.returns.history
        limit = None if history is None else history.limit
        paginated = history is not None and history.paginate
        if source is None or level is None:
            return QueryModel(
                tool=query.tool,
                pack=decl.pack,
                file=decl.file,
                filter=query.filter,
                history_limit=limit,
                history_paginated=paginated,
            )
        return QueryModel(
            tool=query.tool,
            pack=decl.pack,
            file=decl.file,
            source=source,
            source_level=level,
            params=self._params(query, source, level, decl, path),
            filter=query.filter,
            sort=self._query_sort(query.sort, source, level, decl, f"{path}.sort"),
            latest=self._query_fields(
                query.returns.latest, source, level, decl, f"{path}.returns.latest"
            ),
            history=(
                ()
                if history is None
                else self._query_fields(
                    history.fields, source, level, decl, f"{path}.returns.history.fields"
                )
            ),
            history_limit=limit,
            history_paginated=paginated,
        )

    def _source(
        self, name: str, decl: _Decl[object], path: str
    ) -> tuple[str | None, FieldLevel | None]:
        """Resolve the source of a declarative query and the rows it reads."""
        if self._index.entity(name, decl.pack) is not None:
            return (name, "record")
        if self._index.bill(name, decl.pack) is not None:
            return (name, "header")
        bill = name.removesuffix(LINE_SUFFIX)
        if bill != name and self._index.bill(bill, decl.pack) is not None:
            return (bill, "line")
        self._unresolved(decl, path, _OBJECT, name)
        return (None, None)

    def _params(
        self,
        query: DeclarativeQueryDef,
        source: str,
        level: FieldLevel,
        decl: _Decl[object],
        path: str,
    ) -> FrozenMap[str, QueryParamModel]:
        """Resolve the parameters of a declarative query and the fields they map to."""
        found: dict[str, QueryParamModel] = {}
        for name, param in query.params.items():
            item = f"{path}.params.{name}"
            maps_to = self._query_field(param.maps_to, source, level, decl, f"{item}.maps_to")
            entity = (
                None if param.ref is None else self._entity_name(param.ref, decl, f"{item}.ref")
            )
            if maps_to is not None:
                found[name] = QueryParamModel(
                    name=name,
                    type=param.type,
                    maps_to=maps_to,
                    ref=entity,
                    required=param.required,
                )
        return FrozenMap(found)

    def _query_sort(
        self, keys: Sequence[str], source: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> tuple[SortRef, ...]:
        """Resolve the sort keys of a declarative query: ``<field path> [desc]``."""
        found: list[SortRef] = []
        for index, key in enumerate(keys):
            text, _, direction = key.partition(" ")
            field = self._query_field(text, source, level, decl, f"{path}[{index}]")
            if field is not None:
                found.append(SortRef(field, direction == _DESCENDING))
        return tuple(found)

    def _query_fields(
        self, paths: Sequence[str], source: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> tuple[FieldRef, ...]:
        """Resolve a list of field paths of a declarative query, each at its own index."""
        found = (
            self._query_field(text, source, level, decl, f"{path}[{index}]")
            for index, text in enumerate(paths)
        )
        return tuple(field for field in found if field is not None)

    def _query_field(
        self, text: str, source: str, level: FieldLevel, decl: _Decl[object], path: str
    ) -> FieldRef | None:
        """Resolve one ``header.<field>`` or ``line.<field>`` path of a declarative query.

        A bill source resolves both levels, whichever rows the query reads; whether a query
        may use ``line.`` at all is settled by ``loader.step5_expr`` (requirement 10.10).
        An entity has one level, so ``header.<field>`` is one of its fields and
        ``line.<field>`` reaches nothing.
        """
        prefix, _, name = text.partition(".")
        if level != "record":
            return self._field(name, source, _PATH_LEVELS[prefix], decl, path)
        if prefix != "header":
            self._report(decl, path, f"实体 {source} 没有分录，无法解析 {text}")
            return None
        return self._field(name, source, "record", decl, path)


# --- the link fields and the field names of every level ----------------------------------


def _link_name(index: _Index, conversion: str, level: FieldLevel) -> str:
    """The link field of a conversion at ``level``: its writeback's target, or the default.

    A writeback of that conversion whose target is on ``level`` names the field the push's
    quota is counted against; without one the engine keeps :data:`LINKED_QTY` on a line and
    :data:`LINKED_AMOUNT` on a header (meta-model.md section 7).
    """
    for decl in index.writebacks:
        writeback = decl.document
        prefix, _, name = writeback.target.partition(".")
        if writeback.conversion == conversion and _PATH_LEVELS[prefix] == level:
            return name
    return LINKED_QTY if level == "line" else LINKED_AMOUNT


def _link_fields(index: _Index) -> Mapping[tuple[str, FieldLevel], tuple[str, ...]]:
    """The link fields each bill level carries because the bill is the source of a push.

    A conversion adds the link field of its limit_by to its source - the lines for qty, the
    header for amount - and a writeback of that conversion whose target names another field
    adds that one too, on the level of its target (requirement 1.3, meta-model.md 7). The
    names are resolved without reporting anything: a source that is no bill the pack may
    reach is reported where the conversion itself is built.
    """
    found: dict[tuple[str, FieldLevel], tuple[str, ...]] = {}
    for decl in index.conversions.values():
        conversion = decl.document
        if index.bill(conversion.source, decl.pack) is None:
            continue
        level: FieldLevel = "line" if conversion.limit_by == "qty" else "header"
        key = (conversion.source, level)
        found[key] = _added(found.get(key, ()), _link_name(index, conversion.id, level))
        for writeback in index.writebacks:
            if writeback.document.conversion != conversion.id:
                continue
            prefix, _, name = writeback.document.target.partition(".")
            target = (conversion.source, _PATH_LEVELS[prefix])
            found[target] = _added(found.get(target, ()), name)
    return found


def _added(names: tuple[str, ...], *more: str) -> tuple[str, ...]:
    """``names`` with each of ``more`` appended once, keeping the order of the first use."""
    found = list(names)
    for name in more:
        if name not in found:
            found.append(name)
    return tuple(found)


def _field_names(
    index: _Index, link: Mapping[tuple[str, FieldLevel], tuple[str, ...]]
) -> Mapping[tuple[str, FieldLevel], frozenset[str]]:
    """The field names of every level of every object, standard and declared.

    The names alone, so a reference to a field of another object resolves while the field
    views are still being built; they are the names of those views
    (:meth:`_Builder._level`).
    """
    names: dict[tuple[str, FieldLevel], frozenset[str]] = {}
    for name, entity_decl in index.entities.items():
        entity = entity_decl.document
        standard = entity_standard_view(code=entity.code is not None, has_status=entity.has_status)
        names[(name, "record")] = _names(standard, entity.fields)
    for name, bill_decl in index.bills.items():
        bill = bill_decl.document
        header = header_standard_view(
            pricing=bill.pricing, link_fields=link.get((name, "header"), ())
        )
        lines = line_standard_view(
            pricing=bill.pricing,
            line_quantity=bill.line_quantity is not None,
            link_fields=link.get((name, "line"), ()),
        )
        names[(name, "header")] = _names(header, bill.header)
        names[(name, "line")] = _names(lines, bill.lines)
    return names


def _names(
    standard: Sequence[FieldModel], declared: Mapping[str, TypedField | StandardOverride]
) -> frozenset[str]:
    """The names of one level: its standard fields and the fields the pack declares."""
    return frozenset(field.name for field in standard) | declared.keys()


# --- merging a same-name declaration into a standard field -------------------------------


def _merge(standard: FieldModel, declared: TypedField | StandardOverride | None) -> FieldModel:
    """A standard field with the same-name declaration of the pack merged in (req 1.5).

    The declaration gives the label and, for a writable standard field, the constant
    default and the context default; the type, the writability and everything else stay the
    engine's. What such a declaration may say at all is checked by ``loader.step5_fields``
    (requirement 1.6), which reports a default on a read-only standard field, so one is
    ignored here.
    """
    if declared is None:
        return standard
    label = declared.label or standard.label
    if not standard.writable:
        return replace(standard, label=label)
    context = getattr(declared, "default_context", None)
    return replace(
        standard,
        label=label,
        default=standard.default if declared.default is None else declared.default,
        default_context=context if context is not None else standard.default_context,
    )


def _default_from(field: FieldModel, declared: TypedField | StandardOverride | None) -> str | None:
    """The ``default_from`` that applies to one field of a view, None when there is none.

    A declared field brings its own; a standard field takes it from its same-name
    declaration, and only when the field is writable (requirement 1.5).
    """
    if declared is None:
        return None
    return declared.default_from if field.origin == "declared" or field.writable else None


def _written_target(writeback: WritebackDef) -> FieldRef:
    """The target of a writeback as written, for a source bill that did not resolve."""
    prefix, _, name = writeback.target.partition(".")
    return FieldRef(_PATH_LEVELS[prefix], name)


def _no_field(obj: str, level: FieldLevel, name: str) -> str:
    """The reason of an error about a field the level a reference reached does not have."""
    match level:
        case "record":
            return f"实体 {obj} 没有字段 {name}"
        case "header":
            return f"单据 {obj} 的表头没有字段 {name}"
        case "line":
            return f"单据 {obj} 的分录没有字段 {name}"
