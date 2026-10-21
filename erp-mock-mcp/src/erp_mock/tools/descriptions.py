"""The descriptions of the tools, taken verbatim from the variant of the profile (req 3.7).

The last part of the Tool_Generator of design.md 5.1: :mod:`erp_mock.tools.generator`
settles which tools there are, :mod:`erp_mock.tools.input_schema` what each of them takes,
and this module fills in the text the model reads - the ``description`` of the tool and the
``description`` of every one of its parameters. Nothing is generated here: the text of a
pack tool is the text of that pack's ``tools/descriptions/<variant>.yaml``, character for
character, and never a field ``label`` or a draft the engine wrote (requirement 3.7); the
text of the one built-in tool is the framework's own (:mod:`erp_mock.tools.builtin`,
requirement 3.9).

:class:`DescriptionIndex` is every description file of the instance, keyed by pack and
variant; :func:`build_profile_tools` takes the tools of a profile and gives back the same
tools with the text of the profile's variant in them::

    tools = build_profile_tools(generated.of(profile.packs), descriptions, profile)
    tools[0].description                                   # the text of the file
    tools[0].input_schema["properties"]["keyword"]["description"]

A parameter is named the way a description file names it (requirement 3.2): a top-level
parameter by its name, ``lines`` included, and a parameter of the lines element as
``lines[].<name>``, which is what :attr:`ToolSpec.param_paths
<erp_mock.tools.generator.ToolSpec.param_paths>` holds. The ``description`` key is written
through :func:`~erp_mock.tools.input_schema.schema_object`, so it lands in the fixed key
order of that module and the payload stays byte-identical between runs (requirement 11.10).
A description that is missing is written as the empty text rather than left out: whether a
pack wrote one for every tool and every parameter is settled by loading step 7
(:mod:`erp_mock.loader.step7`, requirement 3.2), and a loaded instance has them all.

This module also holds the framework's list of guiding phrases, the text of
``guiding_phrases.txt`` next to it (:data:`GUIDING_PHRASES`): descriptions say what a tool
does and where it applies, they do not tell the model which tool to prefer
(tool-design.md section 3). :func:`guiding_hits` is the test step 7 applies to every
description of every declared variant, and :func:`ascii_folded` the comparison it uses - a
phrase counts as present when it occurs as a substring, ASCII letters compared without
regard to case (requirement 3.4). The cross-check of requirement 3.5 compares the names,
codes and numbers of Business_Data the same way.
"""

import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import resources
from typing import Final, Self

from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import JsonValue
from erp_mock.config.instance import ProfileDef
from erp_mock.config.pack import DescriptionFile, ToolDescription
from erp_mock.tools.builtin import BUILTIN_DESCRIPTIONS
from erp_mock.tools.generator import ToolSpec
from erp_mock.tools.input_schema import LINES_PARAM, schema_object

__all__ = [
    "GUIDING_PHRASES",
    "GUIDING_PHRASES_FILE",
    "LINE_PATH_PREFIX",
    "DescriptionIndex",
    "ascii_folded",
    "build_profile_tools",
    "guiding_hits",
]

# The keys of an inputSchema this module reads: the parameters of an object and, for the
# lines array, the element object whose own properties are the parameters of one line
# (:data:`~erp_mock.tools.input_schema.KEYWORDS`). The ``description`` it writes is passed
# to :func:`~erp_mock.tools.input_schema.schema_object` as a keyword argument.
_PROPERTIES: Final = "properties"
_ITEMS: Final = "items"

# How a description file names a parameter of the lines element (requirement 3.2).
LINE_PATH_PREFIX: Final = f"{LINES_PARAM}[]."

# The file that holds the guiding phrases, next to this module in the installed package.
GUIDING_PHRASES_FILE: Final = "guiding_phrases.txt"

# ASCII letters are compared without regard to case (requirement 3.4); every other
# character, a full width letter included, is compared as it is.
_ASCII_FOLD: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def ascii_folded(text: str) -> str:
    """``text`` with its ASCII upper case letters folded to lower case (requirement 3.4)."""
    return text.translate(_ASCII_FOLD)


def _read_phrases() -> tuple[str, ...]:
    """The phrases of :data:`GUIDING_PHRASES_FILE`, in the order the file writes them.

    One phrase per line; a blank line and a line that starts with ``#`` is a comment.
    Surrounding whitespace is stripped, nothing else: a phrase is matched as it is written.
    """
    text = resources.files("erp_mock.tools").joinpath(GUIDING_PHRASES_FILE).read_text("utf-8")
    lines = (line.strip() for line in text.splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


# The guiding phrases the framework maintains, as the file next to this module lists them
# (requirement 3.4). No description of any declared variant may contain any of them.
GUIDING_PHRASES: Final[tuple[str, ...]] = _read_phrases()


def guiding_hits(text: str, phrases: Sequence[str] = GUIDING_PHRASES) -> tuple[str, ...]:
    """The phrases of ``phrases`` that occur in ``text``, in the order of ``phrases``.

    A phrase occurs when it is a substring of the text, ASCII letters compared without
    regard to case (requirement 3.4); the phrase is returned as ``phrases`` writes it, so
    an error names the phrase of the list and not the text that matched it.
    """
    folded = ascii_folded(text)
    return tuple(phrase for phrase in phrases if ascii_folded(phrase) in folded)


# --- the description files of the instance -----------------------------------------------


@dataclass(frozen=True, slots=True)
class DescriptionIndex:
    """Every ``tools/descriptions/<variant>.yaml`` of the instance, by pack and variant.

    ``packs`` maps the name of a business pack to its variants and each variant to the
    descriptions of that file, keyed by tool name - the name as the pack exposes it, after
    ``exclude`` and ``rename`` (requirement 3.2). A pack or a variant without a file is
    simply absent, which :meth:`of` reports as no description at all; step 7 turns that
    into one error per tool and parameter.
    """

    packs: FrozenMap[str, FrozenMap[str, FrozenMap[str, ToolDescription]]] = FrozenMap()

    @classmethod
    def build(cls, files: Iterable[tuple[str, DescriptionFile]]) -> Self:
        """The index of ``files``, each a pack name with one of its description documents.

        The documents of one pack are taken in the order they are given, which is the order
        the files were read; a variant written twice keeps the first document, as a
        duplicate name does everywhere in the configuration.
        """
        found: dict[str, dict[str, FrozenMap[str, ToolDescription]]] = {}
        for pack, document in files:
            variants = found.setdefault(pack, {})
            variants.setdefault(document.variant, FrozenMap(document.tools))
        return cls(FrozenMap((pack, FrozenMap(variants)) for pack, variants in found.items()))

    def of(self, pack: str | None, variant: str) -> FrozenMap[str, ToolDescription]:
        """The descriptions of ``variant`` of ``pack``, empty when there is no such file.

        ``pack`` is None for the built-in tools, whose text the framework writes itself
        (requirement 3.9).
        """
        if pack is None:
            return BUILTIN_DESCRIPTIONS
        return self.packs.get(pack, FrozenMap()).get(variant, FrozenMap())

    def tool(self, spec: ToolSpec, variant: str) -> ToolDescription:
        """The descriptions written for ``spec`` in ``variant``, empty when there are none."""
        return self.of(spec.pack, variant).get(spec.name, ToolDescription())


# --- the tools of a profile --------------------------------------------------------------


def build_profile_tools(
    specs: Sequence[ToolSpec], descriptions: DescriptionIndex, profile: ProfileDef
) -> tuple[ToolSpec, ...]:
    """``specs`` with the descriptions of the variant of ``profile`` in them (req 3.7).

    ``specs`` are the tools the profile exposes, in the output order of requirement 11.9,
    which is what :meth:`GeneratedTools.of
    <erp_mock.loader.step6.GeneratedTools.of>` gives for the packs of the profile; the
    tools come back in that same order, each with its ``description`` and an
    ``input_schema`` whose parameters carry theirs. Which tools a profile exposes is
    decided by its packs and by ``include_tools`` and ``exclude_tools``, not here.
    """
    variant = profile.description_variant
    return tuple(_described(spec, descriptions.tool(spec, variant)) for spec in specs)


def _described(spec: ToolSpec, texts: ToolDescription) -> ToolSpec:
    """``spec`` with the tool text and the parameter texts of ``texts`` written into it."""
    return replace(
        spec,
        description=texts.description,
        input_schema=_described_object(spec.input_schema, texts.params, ""),
    )


def _described_object(
    schema: Mapping[str, JsonValue], texts: Mapping[str, str], prefix: str
) -> FrozenMap[str, JsonValue]:
    """``schema`` with the text of ``texts`` written into each of its parameters.

    ``prefix`` is what a parameter path of this object starts with: nothing at the top
    level and ``lines[].`` inside the lines element (requirement 3.2). The lines array
    carries its own text and the texts of the element parameters, so it is the one
    parameter whose ``items`` are described as well.
    """
    properties = _object(schema.get(_PROPERTIES))
    described: dict[str, JsonValue] = {}
    for name, param in properties.items():
        path = f"{prefix}{name}"
        schema_of = _object(param)
        if not prefix and name == LINES_PARAM:
            element = _described_object(_object(schema_of.get(_ITEMS)), texts, LINE_PATH_PREFIX)
            schema_of = {**schema_of, _ITEMS: element}
        described[name] = _with(schema_of, description=texts.get(path, ""))
    return _with(schema, properties=FrozenMap(described))


def _with(schema: Mapping[str, JsonValue], **items: JsonValue) -> FrozenMap[str, JsonValue]:
    """``schema`` with ``items`` set, the keys back in the order of the 14 keywords."""
    return schema_object(**{**schema, **items})


def _object(value: JsonValue) -> Mapping[str, JsonValue]:
    """``value`` as a schema object; an empty one when it is absent or not an object.

    Every inputSchema of :mod:`erp_mock.tools.input_schema` writes ``properties`` and gives
    the lines array an ``items`` object, so the empty case is a tool that takes nothing.
    """
    return value if isinstance(value, Mapping) else FrozenMap()
