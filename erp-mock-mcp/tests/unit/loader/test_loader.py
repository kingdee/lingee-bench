"""erp_mock.loader.loader: the order of the steps and stopping at the step that fails."""

from pathlib import Path

import pytest

from erp_mock.loader.errors import CROSS, ConfigErrors, Step, StepErrors
from erp_mock.loader.loader import STEPS, Loader


class RecordingLoader(Loader):
    """A loader that records the steps that ran and reports errors at one chosen step."""

    def __init__(self, failing_step: Step | None = None, found: int = 2) -> None:
        super().__init__(Path("instances/p2p-demo"), Path("packs"))
        self.failing_step = failing_step
        self.found = found
        self.ran: list[Step] = []

    def check(self, step: Step, errors: StepErrors) -> None:
        self.ran.append(step)
        self.pack_order = ("purchase", "core")
        if step != self.failing_step:
            return
        for index in range(self.found):
            errors.add_at("core", f"entities/e{index}.yaml", "fields", f"原因 {index}")


@pytest.mark.req("2.1")
def test_steps_run_in_order_with_the_cross_check_last() -> None:
    loader = RecordingLoader()

    with pytest.raises(AssertionError):
        # The recording loader runs no check and leaves no result behind, so every step
        # passes and the assembly of the read-only configuration object has nothing to
        # work with; what it assembles out of real results is tested in
        # tests/unit/loader/test_loaded_instance.py.
        loader.load()

    assert loader.ran == [1, 2, 3, 4, 5, 6, 7, 8, CROSS]
    assert tuple(loader.ran) == STEPS


@pytest.mark.req("2.1", "2.2")
def test_failing_step_reports_all_of_its_errors_and_no_later_step_runs() -> None:
    loader = RecordingLoader(failing_step=3, found=3)
    with pytest.raises(ConfigErrors) as caught:
        loader.load()
    assert caught.value.step == 3
    assert len(caught.value.errors) == 3
    assert loader.ran == [1, 2, 3]


@pytest.mark.req("2.2")
def test_errors_are_sorted_with_the_pack_order_the_steps_set() -> None:
    class TwoPackLoader(RecordingLoader):
        def check(self, step: Step, errors: StepErrors) -> None:
            super().check(step, errors)
            errors.add_at("core", "pack.yaml", None, "原因")
            errors.add_at("purchase", "pack.yaml", None, "原因")

    loader = TwoPackLoader()
    with pytest.raises(ConfigErrors) as caught:
        loader.load()
    assert caught.value.step == 1
    assert caught.value.packs == ("purchase", "core")
    assert [found.pack for found in caught.value.errors] == ["purchase", "core"]
    assert loader.ran == [1]
