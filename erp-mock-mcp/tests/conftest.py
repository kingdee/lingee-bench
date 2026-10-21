"""Suite-wide pytest configuration: Hypothesis defaults and the ``req`` marker check."""

from pathlib import Path
from typing import Final

import pytest
from hypothesis import settings

from erp_mock.common.jsonio import dumps_pretty, encode_utf8

# Helper modules may use plain ``assert``; give them pytest's assertion rewriting like
# test modules get. This must run before any tests.helpers module is imported.
pytest.register_assert_rewrite("tests.helpers")

# Replace Hypothesis' built-in "default" profile with a copy that has no deadline (the
# built-in "ci" profile, which Hypothesis loads on CI servers, has none either). A settings
# object copies unspecified values from the active profile when it is created, so a test's
# @settings(max_examples=...) decorator inherits deadline=None. Property tests declare
# their example count explicitly: max_examples=500 for properties 3 and 17, 100 otherwise.
settings.register_profile("default", settings.get_profile("default"), deadline=None)

# pytester runs pytest in-process for plugin tests; tests.helpers.req_marker fails the
# collection of tests whose @pytest.mark.req(...) arguments are malformed.
pytest_plugins = ["pytester", "tests.helpers.req_marker"]

# The option that writes the ``req`` markers of this run to a file, and the attribute
# argparse stores it under (requirement 25.9).
EXPORT_OPTION: Final = "--export-req-markers"
EXPORT_DEST: Final = "export_req_markers"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--export-req-markers`` (requirement 25.9).

    Off by default, so a run that does not ask for it writes nothing::

        uv run pytest tests/ --collect-only -q --export-req-markers=var/req-markers.json

    The export is what the requirement tracing check reads (scripts/req_trace.py); with
    ``--collect-only`` it costs one collection and runs no test.
    """
    parser.addoption(
        EXPORT_OPTION,
        action="store",
        default=None,
        metavar="PATH",
        help=(
            "把本次收集到的每个用例的 req 标记与其 nodeid 写入 PATH（JSON，UTF-8）；"
            "不带该选项时不写任何文件。需求追踪检查（scripts/req_trace.py）读取该文件。"
        ),
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Write the ``req`` markers of the collected tests where the option asked for them.

    Runs after collection, so it writes the same document whether the run goes on to
    execute the tests or was started with ``--collect-only``. A relative path is resolved
    against the directory pytest was started in, and missing parent directories are
    created.
    """
    given = session.config.getoption(EXPORT_DEST, default=None)
    if not given:
        return
    # Imported here: at module level it would run before register_assert_rewrite() above.
    from tests.helpers.req_marker import marker_export

    path = Path(str(given)).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_utf8(dumps_pretty(marker_export(session.items))))
