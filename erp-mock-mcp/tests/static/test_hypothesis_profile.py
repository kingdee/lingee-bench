"""Static checks of the Hypothesis defaults set in tests/conftest.py."""

import pytest
from hypothesis import settings

# Built at import time, like the @settings(...) decorator of a property test.
DECLARED = settings(max_examples=500)


@pytest.mark.req("25.2")
def test_default_profile_has_no_deadline() -> None:
    assert settings.get_profile("default").deadline is None
    assert settings().deadline is None


@pytest.mark.req("25.2")
def test_declared_example_count_keeps_the_profile_deadline() -> None:
    assert DECLARED.max_examples == 500
    assert DECLARED.deadline is None
