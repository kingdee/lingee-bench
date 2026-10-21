"""Immutable, insertion-ordered mapping for read-only configuration and domain models.

Configuration objects produced by the Loader must reject every modification attempt and
keep their content (requirement 2.11). Their collections are tuples and FrozenMaps.
FrozenMap is also a pydantic field type: ``FrozenMap[K, V]`` validates like
``dict[K, V]`` and yields a FrozenMap.
"""

from collections.abc import ItemsView, Iterable, Iterator, KeysView, Mapping, ValuesView
from types import MappingProxyType
from typing import Any, Self, TypeVar, get_args, overload

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

__all__ = ["FrozenMap"]

_K = TypeVar("_K")
_V_co = TypeVar("_V_co", covariant=True)


class FrozenMap(Mapping[_K, _V_co]):
    """A read-only Mapping that keeps insertion order and is hashable.

    ``FrozenMap(mapping)`` and ``FrozenMap(iterable_of_pairs)`` copy their argument, so
    later changes to the source do not show. Iteration, ``keys()``, ``values()`` and
    ``items()`` follow insertion order. Equality is that of Mapping: a FrozenMap equals
    any Mapping with the same items, whatever their order, a dict included. The hash
    combines the items (order-independent, consistent with equality) and raises
    TypeError when a value is unhashable, as a tuple does.

    Every modification attempt raises: item assignment and deletion raise TypeError;
    assigning or deleting any attribute raises AttributeError; there is no update, pop,
    popitem, clear, setdefault or ``|=``. The container is shallow: values that are
    themselves mutable stay mutable, so read-only models hold only immutable values.

    The value type is covariant like Mapping's: a ``FrozenMap[str, bool]`` is a
    ``FrozenMap[str, int]``.
    """

    __slots__ = ("_data",)

    # A proxy, so not even the private attribute exposes a mutable dict.
    _data: MappingProxyType[_K, _V_co]

    # cls is positional-only in every signature, as in the implementation; otherwise mypy
    # reports that the implementation does not accept FrozenMap.__new__(cls=...).
    @overload
    def __new__(cls, /) -> Self: ...
    @overload
    def __new__(cls, data: Mapping[_K, _V_co], /) -> Self: ...
    @overload
    def __new__(cls, data: Iterable[tuple[_K, _V_co]], /) -> Self: ...
    def __new__(cls, data: Mapping[_K, _V_co] | Iterable[tuple[_K, _V_co]] = (), /) -> Self:
        # Built in __new__, not __init__, so calling __init__ again cannot refill it.
        self = super().__new__(cls)
        object.__setattr__(self, "_data", MappingProxyType(dict(data)))
        return self

    def __getitem__(self, key: _K, /) -> _V_co:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object, /) -> bool:
        return key in self._data

    # The views of the underlying dict are read-only and faster than Mapping's mixins.
    def keys(self) -> KeysView[_K]:
        return self._data.keys()

    def values(self) -> ValuesView[_V_co]:
        return self._data.values()

    def items(self) -> ItemsView[_K, _V_co]:
        return self._data.items()

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self._data)!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        # copy, deepcopy and pickle rebuild the map through __new__; the default protocol
        # would restore the slot with setattr, which is rejected.
        return (type(self), (dict(self._data),))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(
            f"cannot assign attribute {name!r}: {type(self).__name__} is immutable"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError(
            f"cannot delete attribute {name!r}: {type(self).__name__} is immutable"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        """Validate ``FrozenMap[K, V]`` like ``dict[K, V]``, then wrap the result.

        Keys and values are validated with the schemas of K and V (Any when the type is
        unparametrized), under the strictness of the enclosing model, and error
        locations are those of a dict field: ``(field, key)`` for a value and
        ``(field, key, "[key]")`` for a key. Python input may be a dict or a FrozenMap,
        whose items are validated again; in lax mode any Mapping is accepted, as for
        dict. Serialization and the JSON Schema are those of the dict, so ``model_dump``
        returns plain dicts and the schema is ``{"type": "object", ...}``.
        """
        args = get_args(source)
        key_type, value_type = args if len(args) == 2 else (Any, Any)
        dict_schema = core_schema.dict_schema(
            keys_schema=handler.generate_schema(key_type),
            values_schema=handler.generate_schema(value_type),
        )
        from_dict = core_schema.no_info_after_validator_function(cls, dict_schema)
        return core_schema.json_or_python_schema(
            json_schema=from_dict,
            # A strict dict schema accepts only dict instances: unwrap a FrozenMap first.
            python_schema=core_schema.no_info_before_validator_function(_unwrap, from_dict),
            serialization=core_schema.plain_serializer_function_ser_schema(
                dict, info_arg=False, return_schema=dict_schema
            ),
        )


def _unwrap(value: object) -> object:
    """Turn a FrozenMap into a dict for validation; leave other input unchanged."""
    return dict(value) if isinstance(value, FrozenMap) else value
