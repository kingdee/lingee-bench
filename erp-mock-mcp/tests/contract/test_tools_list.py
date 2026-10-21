"""The tools/list contract snapshot of p2p-demo (requirements 11.10, 21.3 and 25.3).

``snapshots/p2p-demo.semantic.tools-list.json`` is what a client connected to the example
instance receives when it asks for tools/list: the 22 tools of the M0 tool list
(example-p2p.md section 9) with their descriptions, their input and output schemas and
their annotations, as the SDK serializes them. The file is compared byte for byte on every
run and rewritten only by ``--update-snapshots`` (tests/contract/conftest.py), so anything
that changes what the model is told - a word of a description, a key of a schema, a tool
added or dropped - turns up as a reviewable change of this file (requirement 25.3).

How the payload is serialized, which is what makes the comparison meaningful:

- the gateway answers tools/list with ``ProfileModel.tools_list_result()``, an
  ``mcp.types.ListToolsResult`` built from the payload the Loader cached for the profile
  (:func:`~erp_mock.loader.instance.tools_list_payload`; the cache is a frozen tree and not
  an SDK model, requirement 2.11);
- the SDK's own session puts that result into the JSON-RPC response with
  ``model_dump(by_alias=True, mode="json", exclude_none=True)`` and both transports write
  the message with ``model_dump_json(by_alias=True, exclude_none=True)`` (V2,
  tests/probes/test_sdk_server.py);
- the snapshot file holds that very tree indented by
  :func:`~erp_mock.common.jsonio.dumps_pretty`, which writes the Chinese of the
  descriptions as it is (``ensure_ascii=False``), keeps every key in its insertion order
  and ends the file with a newline.
  :func:`test_the_snapshot_holds_what_the_sdk_writes_on_the_wire` pins the two together:
  the bytes a transport writes are the compact form of exactly this tree, so the file is
  the wire content, indented to be diffable.

Which tools the payload lists is asserted on the payload itself rather than on the file:
exactly 22, ``business_context_get`` first and then the tools of core, purchase and
inventory in that order (requirement 21.3). A snapshot proves only that nothing changed, so
an ``--update-snapshots`` that blessed a wrong tool list would still fail here.

The payload depends on the configuration and the profile alone, never on the anchor date,
the dataset, the session or the working directory (requirement 11.10), so this module runs
the validation steps that read the documents and leaves step 8 - the build of the dataset -
to tests/packs/test_p2p_demo.py. The anchor date is passed in all the same: the clock of
p2p-demo is in mode today, and reading the system clock in a test is what requirement 25.10
forbids. :func:`test_the_payload_does_not_depend_on_the_anchor_date` loads the instance at a
second date and gets the same bytes; that it holds across processes and hash seeds too is
requirement 24.9, checked by the determinism tests.
"""

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from erp_mock.common.jsonio import JsonValue, dumps_compact, dumps_pretty, encode_utf8
from erp_mock.loader.errors import Step
from erp_mock.loader.instance import TOOLS_KEY, ProfileModel
from erp_mock.loader.loader import Loader
from erp_mock.tools.generator import BUSINESS_CONTEXT_GET
from tests.contract.conftest import Snapshots

# The example instance and the example business packs of the repository.
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INSTANCE_DIR: Final = REPO_ROOT / "instances" / "p2p-demo"
PACKS_DIR: Final = REPO_ROOT / "packs"

# The instance and the profile the snapshot is of, which are the names in its file name.
INSTANCE: Final = "p2p-demo"
PROFILE: Final = "semantic"
SNAPSHOT_FILE: Final = f"{INSTANCE}.{PROFILE}.tools-list.json"

# The validation steps that read the documents, which is every step the payload comes out
# of: step 8 takes up the Snapshot of the dataset and needs a data directory, and the tools
# of a profile do not depend on it (requirement 11.10, module docstring).
CONFIG_STEPS: Final[tuple[Step, ...]] = (1, 2, 3, 4, 5, 6, 7)

# Two anchor dates, neither of which may reach the payload: the one the module loads under
# and the leap day requirement 25.4 names.
ANCHOR_DATE: Final = date(2026, 10, 9)
OTHER_ANCHOR_DATE: Final = date(2000, 2, 29)

# The key of a tool's name in the payload, under the name the MCP schema gives it.
NAME_KEY: Final = "name"

# --- the 22 tools, in the order tools/list returns them ----------------------------------

# The tools of each pack, in the output order of requirement 11.9: inside a pack the tools
# of the entities and bills first, by the path of the declaring file and, per object, in the
# order of its ``operations``, then the push tools by file path, then the query tools in
# declaration order. What is not here is what each pack's ``exclude`` drops to reach the M0
# tool list (requirement 21.4): core's org, employee, unit and department tools, the get,
# search and delete of the purchase receipt, purchase_requisition_get and the state
# operations of the stock adjustment.
CORE_TOOLS: Final[tuple[str, ...]] = (
    # entities/material.yaml, entities/supplier.yaml, entities/warehouse.yaml
    "material_search",
    "material_get",
    "supplier_search",
    "supplier_get",
    "warehouse_search",
)
PURCHASE_TOOLS: Final[tuple[str, ...]] = (
    # entities/purchase_order.yaml
    "purchase_order_search",
    "purchase_order_get",
    "purchase_order_create",
    "purchase_order_update",
    "purchase_order_submit",
    "purchase_order_approve",
    "purchase_order_close",
    # entities/purchase_receipt.yaml
    "purchase_receipt_submit",
    "purchase_receipt_approve",
    # entities/purchase_requisition.yaml
    "purchase_requisition_search",
    "purchase_requisition_create",
    # conversions/purchase_order_to_receipt.yaml, conversions/purchase_requisition_to_order.yaml
    "purchase_receipt_create_from_order",
    "purchase_order_create_from_requisition",
    # tools/exposure.yaml, the one queries item (requirement 21.13)
    "purchase_price_query",
)
INVENTORY_TOOLS: Final[tuple[str, ...]] = (
    # entities/stock_adjustment.yaml, then the primitive query of tools/exposure.yaml
    "stock_adjustment_create",
    "inventory_balance_query",
)

# The packs in the order instance.yaml and the profile list them, each with its tools.
PACK_TOOLS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("core", CORE_TOOLS),
    ("purchase", PURCHASE_TOOLS),
    ("inventory", INVENTORY_TOOLS),
)

# The 22 tools of the M0 tool list, in the one order tools/list returns them
# (requirement 21.3): the built-in ``business_context_get`` first, then pack by pack.
EXPECTED_TOOLS: Final[tuple[str, ...]] = (
    BUSINESS_CONTEXT_GET,
    *(name for _, names in PACK_TOOLS for name in names),
)

# The pack each of those tools belongs to; the built-in tool belongs to none.
EXPECTED_PACKS: Final[tuple[str | None, ...]] = (
    None,
    *(pack for pack, names in PACK_TOOLS for _ in names),
)

TOOL_COUNT: Final = 22


# --- loading the profile and serializing its payload -------------------------------------


def semantic_profile(anchor_date: date) -> ProfileModel:
    """The semantic profile of p2p-demo, loaded at ``anchor_date``.

    Runs :data:`CONFIG_STEPS` over the real ``instances/p2p-demo``, so an error in any
    document of the instance or of the three packs fails here with
    :class:`~erp_mock.loader.errors.ConfigErrors`; the profile is the ``default_profile``
    of instance.yaml, which step 5 checked is one of its profiles.
    """
    loader = Loader(INSTANCE_DIR, PACKS_DIR, anchor_date)
    for step in CONFIG_STEPS:
        loader.run_step(step)
    loaded = loader.assemble()
    assert loaded.instance.default_profile == PROFILE
    return loaded.default_profile


@pytest.fixture(scope="module")
def profile() -> ProfileModel:
    """The semantic profile at :data:`ANCHOR_DATE`, loaded once for the module."""
    return semantic_profile(ANCHOR_DATE)


def snapshot_bytes(payload: JsonValue) -> bytes:
    """``payload`` as the snapshot file holds it: indented UTF-8 JSON ending in a newline.

    :func:`~erp_mock.common.jsonio.dumps_pretty` keeps every key in its insertion order,
    which here is the key order of the protocol, and writes non-ASCII characters as they
    are, so the descriptions read as the Chinese the packs wrote (module docstring).
    """
    return encode_utf8(dumps_pretty(payload))


def tool_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The tool names of a serialized tools/list payload, in the order it lists them."""
    return tuple(tool[NAME_KEY] for tool in payload[TOOLS_KEY])


# --- the snapshot ------------------------------------------------------------------------


@pytest.mark.req("11.10", "21.3", "25.3")
def test_the_payload_lists_the_twenty_two_tools_and_is_the_contract_snapshot(
    profile: ProfileModel, snapshots: Snapshots
) -> None:
    payload = profile.payload
    # ``nextCursor`` and ``_meta`` are None and left out, so the payload is ``{"tools": ...}``
    # alone: one response carries every tool of the profile (requirement 14.5).
    assert list(payload) == [TOOLS_KEY]

    # Exactly the 22 tools of the M0 tool list, in the output order of requirement 11.9:
    # business_context_get first, then core, purchase and inventory (requirement 21.3).
    # Asserted on the payload, not on the snapshot file, so an --update-snapshots that
    # blessed another tool list still fails (tests/contract/conftest.py).
    names = tool_names(payload)
    assert len(names) == TOOL_COUNT
    assert names[0] == BUSINESS_CONTEXT_GET
    assert names == EXPECTED_TOOLS
    # And those names are grouped by pack, the built-in tool belonging to none: the order
    # above is the order of the packs, not a coincidence of the names.
    assert tuple(spec.pack for spec in profile.tools) == EXPECTED_PACKS
    assert profile.tool_names == names

    # The payload byte for byte, serialized as the SDK sends it; a difference is reported
    # as a unified diff and only --update-snapshots rewrites the file (requirement 25.3).
    snapshots.check(SNAPSHOT_FILE, snapshot_bytes(payload))


@pytest.mark.req("25.3")
def test_the_snapshot_holds_what_the_sdk_writes_on_the_wire(profile: ProfileModel) -> None:
    result = profile.tools_list_result()
    # What the SDK's own session puts into the JSON-RPC result, and what both transports
    # write for the message that carries it (V2, tests/probes/test_sdk_server.py).
    sent = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    wire = result.model_dump_json(by_alias=True, exclude_none=True)

    # The cached payload is that serialization: the Loader produced it with the same call
    # and froze it, and building the SDK model back from it changes nothing.
    assert dumps_pretty(profile.payload) == dumps_pretty(sent)
    # And what a transport writes carries the same tree with the keys in the same order -
    # read back and written again through one writer, the two texts are equal - so the
    # snapshot file is the wire content, indented.
    assert dumps_compact(json.loads(wire)) == dumps_compact(sent)


@pytest.mark.req("11.10")
def test_the_payload_does_not_depend_on_the_anchor_date(profile: ProfileModel) -> None:
    # Which tools a profile exposes and what each of them says comes from the configuration
    # alone, so a second load at another date serializes to the same bytes; nothing of the
    # clock, the dataset or the session reaches the payload (requirement 11.10).
    other = semantic_profile(OTHER_ANCHOR_DATE)
    assert snapshot_bytes(other.payload) == snapshot_bytes(profile.payload)
