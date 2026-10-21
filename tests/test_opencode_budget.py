"""Output budget propagation through Harbor's native OpenCode lifecycle."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.opencode import OpenCode
from harbor.agents.model_connection import ResolvedModelConnection
from harbor.models.agent.context import AgentContext

from lg_bench.agents.opencode import LgOpenCode, OUTPUT_TOKEN_LIMIT_ENV


@pytest.mark.parametrize("budget", [65535, 4096])
def test_budget_reaches_native_run_and_resume(tmp_path, budget):
    kwargs = {} if budget == 65535 else {"max_output_tokens": budget}
    agent = LgOpenCode(tmp_path, model_name="glm-5.3-flash", **kwargs)
    agent.exec_as_agent = AsyncMock(return_value=SimpleNamespace(return_code=0, stdout="", stderr=""))

    async def turns():
        await agent.run("first turn", SimpleNamespace(), AgentContext())
        await agent.resume("second turn", SimpleNamespace(), AgentContext())

    asyncio.run(turns())
    assert LgOpenCode.run is OpenCode.run
    runs = [c.kwargs for c in agent.exec_as_agent.await_args_list
            if "run --format=json" in c.kwargs["command"]]
    assert len(runs) == 2
    assert "--continue" not in runs[0]["command"]
    assert "--continue" in runs[1]["command"]
    assert all(c["env"][OUTPUT_TOKEN_LIMIT_ENV] == str(budget) for c in runs)
    assert runs[0]["env"]["XDG_DATA_HOME"] == runs[1]["env"]["XDG_DATA_HOME"]
    plan = agent.dry_run_plan()
    assert plan["run_env"] == {OUTPUT_TOKEN_LIMIT_ENV: str(budget)}
    config = json.loads(next(iter(plan["config_files"].values())))
    assert "maxOutputTokens" not in config["provider"]["lg-openai"]["models"]["glm-5.3-flash"]


def test_budget_preserves_connection_without_exposing_credentials(tmp_path, monkeypatch):
    inherited = ResolvedModelConnection(
        provider="fixture-provider", api_key="fixture-secret", base_url="https://fixture.invalid",
        configured_base_url="https://configured.invalid",
        env={"EXISTING": "preserved", "FIXTURE_API_KEY": "fixture-secret", OUTPUT_TOKEN_LIMIT_ENV: "32000"},
    )
    monkeypatch.setattr(OpenCode, "model_connection", property(lambda self: inherited))
    agent = LgOpenCode(tmp_path, model_name="glm-5.3-flash")
    connection = agent.model_connection
    for field in ("provider", "api_key", "base_url", "configured_base_url"):
        assert getattr(connection, field) == getattr(inherited, field)
    assert connection.env == {**inherited.env, OUTPUT_TOKEN_LIMIT_ENV: "65535"}
    assert inherited.env[OUTPUT_TOKEN_LIMIT_ENV] == "32000"
    assert agent.dry_run_plan()["run_env"] == {OUTPUT_TOKEN_LIMIT_ENV: "65535"}


@pytest.mark.parametrize("value", [0, -1, True, 65535.0, "65535", None])
def test_budget_rejects_invalid_values(tmp_path, value):
    with pytest.raises(ValueError, match="positive integer"):
        LgOpenCode(tmp_path, model_name="glm-5.3-flash", max_output_tokens=value)
