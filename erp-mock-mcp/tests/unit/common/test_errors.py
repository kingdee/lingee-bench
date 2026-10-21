"""erp_mock.common.errors: error codes and the ErpError exception."""

import contextlib
import copy
import pickle
from collections.abc import Iterator

import pytest

from erp_mock.common.errors import ErpError, ErrorCode
from erp_mock.common.jsonio import dumps_compact


@pytest.mark.req("13.1")
def test_error_codes() -> None:
    assert [code.value for code in ErrorCode] == [
        "VALIDATION_FAILED",
        "NOT_FOUND",
        "INVALID_STATE",
        "BUSINESS_RULE_VIOLATION",
        "CONFLICT",
        "LIMIT_EXCEEDED",
        "INTERNAL_ERROR",
    ]
    assert all(code == code.name for code in ErrorCode)
    assert dumps_compact({"code": ErrorCode.NOT_FOUND}) == '{"code":"NOT_FOUND"}'


@pytest.mark.req("13.1")
def test_fields() -> None:
    detail = {
        "field": "lines[0].qty",
        "reason": "exceeds_pushable_qty",
        "source_line_id": "po_000001-1",
        "pushable_qty": "500",
    }
    message = "入库数量超过采购订单行的可入库数量：lines[0].qty"
    error = ErpError(ErrorCode.BUSINESS_RULE_VIOLATION, message, "R-GEN-005", [detail])
    assert error.code is ErrorCode.BUSINESS_RULE_VIOLATION
    assert error.message == message
    assert error.rule_id == "R-GEN-005"
    assert error.details == (detail,)  # stored as a tuple
    assert str(error) == f"BUSINESS_RULE_VIOLATION R-GEN-005: {message}"

    plain = ErpError(ErrorCode.NOT_FOUND, "供应商不存在：supplier_id")
    assert plain.rule_id is None
    assert plain.details == ()
    assert str(plain) == "NOT_FOUND: 供应商不存在：supplier_id"


@pytest.mark.req("13.1")
@pytest.mark.parametrize("field", ["code", "message", "rule_id", "details"])
def test_fields_are_read_only(field: str) -> None:
    error = ErpError(ErrorCode.CONFLICT, "编码重复：code")
    with pytest.raises(AttributeError):
        setattr(error, field, None)
    with pytest.raises(AttributeError):
        delattr(error, field)
    assert (error.code, error.message, error.rule_id, error.details) == (
        ErrorCode.CONFLICT,
        "编码重复：code",
        None,
        (),
    )


@contextlib.contextmanager
def _transaction() -> Iterator[None]:
    try:
        yield
    except BaseException:
        raise  # a real transaction rolls back here


@pytest.mark.req("13.1")
def test_works_with_the_exception_machinery() -> None:
    error = ErpError(
        ErrorCode.INVALID_STATE,
        "单据状态不允许：status",
        details=[{"reason": "invalid_state", "status": "approved"}],
    )
    # contextmanager assigns __traceback__ to an exception that leaves its with block.
    with pytest.raises(ErpError) as info, _transaction():
        raise error
    assert info.value is error
    error.add_note("note")
    assert error.__notes__ == ["note"]
    for clone in (copy.copy(error), pickle.loads(pickle.dumps(error))):
        assert type(clone) is ErpError
        assert (clone.code, clone.message, clone.rule_id, clone.details) == (
            error.code,
            error.message,
            error.rule_id,
            error.details,
        )
        assert clone.__notes__ == ["note"]
