"""A byte-bounded query page must preserve a complete, reachable record sequence."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.frozen import FrozenMap
from erp_mock.common.jsonio import dumps_compact
from erp_mock.config.model import FieldRef, QueryModel
from erp_mock.config.pack import DeclarativeQueryDef
from erp_mock.domain.query import run_query
from erp_mock.domain.views import QueryView, RecordView
from erp_mock.tools.generator import generate_pack_tools
from erp_mock.tools.results import TEXT_LIMIT, result_kinds, result_object, result_payload
from erp_mock.tools.validate import validate_args
from tests.helpers.engine_env import ANCHOR_DATE, engine_env
from tests.helpers.fixtures import fixture_domain


def paginated_query(max_rows=500):
    original = fixture_domain()
    query = QueryModel(
        tool="article_page_query",
        pack="fx_base",
        file="tools/exposure.yaml",
        source="article",
        source_level="record",
        latest=(FieldRef("record", "name"),),
        history=(FieldRef("record", "name"),),
        history_limit=max_rows,
        history_paginated=True,
    )
    domain = replace(original, queries=FrozenMap({**original.queries, query.tool: query}))
    spec = next(t for t in generate_pack_tools(domain, "fx_base") if t.name == query.tool)
    return domain, query, spec


@pytest.mark.parametrize("requested_limit", [1, 7, 50, 500])
def test_pages_reassemble_variable_width_unicode_records(requested_limit):
    domain, _, spec = paginated_query()
    rows = tuple(
        RecordView(FrozenMap({"name": f"第{i}条：" + "宽字段" * (10 + i % 31)})) for i in range(237)
    )
    received = []
    offset = 0
    validator = Draft202012Validator(json.loads(dumps_compact(spec.output_schema)))
    while True:
        candidates = rows[offset : offset + requested_limit]
        payload = result_payload(
            domain,
            spec,
            QueryView(
                latest=rows[0],
                history=candidates,
                offset=offset,
                has_more=offset + len(candidates) < len(rows),
            ),
        )
        page = json.loads(payload.text)
        assert len(payload.text.encode("utf-8")) <= TEXT_LIMIT == 16384
        assert page == payload.structured
        validator.validate(page)
        assert page["returned_count"] == len(page["history"]) > 0
        assert "truncated" not in page
        received.extend(page["history"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] == offset + page["returned_count"]
        assert f"offset={page['next_offset']}" in page["continuation"]
        offset = page["next_offset"]
    assert received == [{"name": row.fields["name"]} for row in rows]


def test_history_limit_bounds_each_page_not_the_whole_result(tmp_path: Path):
    domain, query, spec = paginated_query(max_rows=1)
    with engine_env(tmp_path / "sandbox") as env:
        with env.store.transaction() as uow:
            first = run_query(uow, domain, query, {}, anchor_date=ANCHOR_DATE)
            second = run_query(uow, domain, query, {"offset": 1}, anchor_date=ANCHOR_DATE)
            beyond = run_query(uow, domain, query, {"offset": 100}, anchor_date=ANCHOR_DATE)
    assert len(first.history) == len(second.history) == 1
    assert first.history != second.history
    assert first.latest == second.latest
    assert first.has_more and not second.has_more
    page = json.loads(result_payload(domain, spec, beyond).text)
    assert page["history"] == []
    assert page["returned_count"] == 0
    assert page["next_offset"] is None and not page["has_more"]


def test_empty_and_oversized_pages_do_not_claim_false_progress():
    domain, _, spec = paginated_query()
    empty = json.loads(result_payload(domain, spec, QueryView(offset=0)).text)
    assert empty["latest"] is None and empty["history"] == []
    assert empty["returned_count"] == 0 and empty["next_offset"] is None
    assert not empty["has_more"]
    row = RecordView(FrozenMap({"name": "字" * TEXT_LIMIT}))
    with pytest.raises(ErpError) as error:
        result_payload(domain, spec, QueryView(latest=row, history=(row,), offset=0))
    assert error.value.code == ErrorCode.LIMIT_EXCEEDED


def test_a_fitting_final_page_is_not_rejected_by_a_larger_continuation_notice():
    domain, _, spec = paginated_query()
    empty = RecordView(FrozenMap({"name": ""}))
    view = QueryView(latest=empty, history=(empty, empty), offset=0)
    kinds = result_kinds(domain, spec)
    overhead = len(dumps_compact(result_object(spec.output_schema, view, kinds)).encode())
    wide = RecordView(FrozenMap({"name": "x" * ((TEXT_LIMIT - overhead) // 2)}))
    view = replace(view, latest=wide, history=(wide, empty))
    prefix = replace(view, history=(wide,), has_more=True)
    assert len(dumps_compact(result_object(spec.output_schema, view, kinds)).encode()) <= TEXT_LIMIT
    assert (
        len(dumps_compact(result_object(spec.output_schema, prefix, kinds)).encode()) > TEXT_LIMIT
    )
    page = json.loads(result_payload(domain, spec, view).text)
    assert page["returned_count"] == 2 and not page["has_more"]


@pytest.mark.parametrize(
    "args",
    [
        {"offset": -1},
        {"offset": True},
        {"offset": 0.5},
        {"limit": 0},
        {"limit": True},
        {"limit": 501},
        {"limit": 1.5},
    ],
)
def test_invalid_pagination_controls_are_rejected(args):
    domain, _, spec = paginated_query()
    assert validate_args(domain, spec, args).error is not None


def test_pagination_control_names_cannot_shadow_business_filters():
    with pytest.raises(ValidationError):
        DeclarativeQueryDef.model_validate(
            {
                "tool": "example_query",
                "source": "article",
                "params": {"offset": {"type": "int", "maps_to": "header.name"}},
                "returns": {
                    "latest": ["header.name"],
                    "history": {
                        "fields": ["header.name"],
                        "limit": 50,
                        "paginate": True,
                    },
                },
            }
        )
