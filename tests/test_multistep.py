"""Native Harbor resume and archive lifecycle, without model calls."""
import asyncio
import json
import logging
import shlex
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.opencode import OpenCode
from harbor.models.agent.context import AgentContext
from harbor.trial.artifact_handler import ArtifactHandler
from harbor.trial.multi_step import MultiStepTrial
from harbor.models.trajectories.trajectory import Trajectory

from lg_bench.agents.deepseek_harness import DeepSeekHarness
from lg_bench.agents.opencode import LgOpenCode


def test_local_adapter_uses_upstream_native_resume(tmp_path):
    assert LgOpenCode.SUPPORTS_RESUME
    assert LgOpenCode.run is OpenCode.run
    agent = LgOpenCode(tmp_path, model_name='glm-5.3-flash', install_mode='image')
    commands = []

    async def record(*args, **kwargs):
        commands.append(kwargs)
        return SimpleNamespace(return_code=0, stdout='', stderr='')

    agent.exec_as_agent = AsyncMock(side_effect=record)

    async def turns():
        environment = SimpleNamespace()
        await agent.run('第一轮记住标记。', environment, AgentContext())
        await agent.resume('继续。', environment, AgentContext())

    asyncio.run(turns())
    runs = [c for c in commands if 'run --format=json' in c.get('command', '')]
    assert len(runs) == 2
    assert '--continue' not in runs[0]['command']
    assert '--continue' in runs[1]['command']
    assert runs[0]['env']['XDG_DATA_HOME'] == runs[1]['env']['XDG_DATA_HOME']


def test_native_archiving_preserves_session_storage_but_moves_publish_files(tmp_path):
    agent_dir = tmp_path / 'agent'
    session = agent_dir / 'opencode/xdg-data/session.db'
    session.parent.mkdir(parents=True)
    session.write_text('opaque-native-session-state')
    publish = tmp_path / 'artifacts/logs/artifacts'
    publish.mkdir(parents=True)
    transient = publish / 'capture.json'
    transient.write_text('this publish directory is per-step')
    private = tmp_path / 'private-evidence/round-1/capture.json'
    private.parent.mkdir(parents=True)
    private.write_text('frozen-verifier-evidence')
    paths = SimpleNamespace(
        agent_dir=agent_dir, verifier_dir=tmp_path / 'verifier',
        artifacts_dir=tmp_path / 'artifacts',
        step_agent_dir=lambda n: tmp_path / 'steps' / n / 'agent',
        step_verifier_dir=lambda n: tmp_path / 'steps' / n / 'verifier',
        step_artifacts_dir=lambda n: tmp_path / 'steps' / n / 'artifacts',
    )
    trial = SimpleNamespace(paths=paths, _main_artifacts_mount_dir=publish,
        _artifact_handler=ArtifactHandler(artifacts=[], logger=logging.getLogger('test')))
    for name in ['_copy_agent_dir_to_step', '_move_agent_dir_to_step']:
        setattr(trial, name, MethodType(getattr(MultiStepTrial, name), trial))
    MultiStepTrial._archive_step_outputs(trial, SimpleNamespace(name='round-1'),
                                        preserve_agent=True)
    assert session.read_text() == 'opaque-native-session-state'
    assert (paths.step_agent_dir('round-1') / session.relative_to(agent_dir)).read_bytes() == session.read_bytes()
    assert private.read_text() == 'frozen-verifier-evidence'
    assert not transient.exists()
    assert (paths.step_artifacts_dir('round-1') / 'logs/artifacts/capture.json').exists()


def test_deepseek_resumes_exact_native_session_and_fresh_runs_stay_fresh(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name='glm-5.3-flash', install_mode='image')
    agent.missing_env = lambda: []
    agent.exec_as_agent = AsyncMock()

    async def turns():
        await agent.run('记住暗号。', SimpleNamespace(), AgentContext())
        (tmp_path / agent.OUTPUT_FILENAME).write_text('\n'.join(map(json.dumps, [
            {'type': 'session', 'sessionId': 'session-first'},
            {'type': 'final', 'text': '已记住。'},
        ])))
        agent.populate_context_post_run(AgentContext())
        await agent.resume('继续。', SimpleNamespace(), AgentContext())
        await agent.run('独立新任务。', SimpleNamespace(), AgentContext())

    asyncio.run(turns())
    commands = [c.kwargs['command'] for c in agent.exec_as_agent.await_args_list
                if '--json' in c.kwargs['command']]
    assert len(commands) == 3
    assert '--session-id' not in commands[0]
    assert '--session-id session-first --' in commands[1]
    assert '--session-id' not in commands[2]


def test_deepseek_resume_without_collected_identity_fails(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name='glm-5.3-flash')
    agent.missing_env = lambda: []
    agent.exec_as_agent = AsyncMock()
    with pytest.raises(ValueError, match='no collected session id'):
        asyncio.run(agent.resume('继续。', SimpleNamespace(), AgentContext()))
    assert not agent._resume
    assert all('--json' not in c.kwargs['command'] for c in agent.exec_as_agent.await_args_list)


def test_deepseek_resume_quotes_opaque_session_identity(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name='glm-5.3-flash')
    agent._session_id = "session-' with space; arbitrary text"
    agent._resume = True
    tokens = shlex.split(agent.build_run_command(shlex.quote('继续。')))
    assert tokens[tokens.index('--session-id') + 1] == agent._session_id


def test_deepseek_atif_retains_current_prompt_tools_and_lossless_final(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name='glm-5.3-flash')
    agent._instruction = '本轮恢复匹配。'
    reply = '完整终端回复。' * 3000
    events = [
        {'type': 'session', 'sessionId': 'session-first'},
        {'type': 'thinking', 'text': '先查明细。'},
        {'type': 'tool_call', 'callId': 'call1', 'tool': 'erp_query', 'input': {'page': 1}},
        {'type': 'tool_result', 'callId': 'call1', 'result': '查询结果'},
        {'type': 'status', 'phase': 'step_end',
         'usage': {'inputTokens': 20, 'cacheReadTokens': 10, 'outputTokens': 4}},
        {'type': 'text', 'text': reply[:8192], 'truncated': True},
        {'type': 'final', 'text': reply},
    ]
    (tmp_path / agent.OUTPUT_FILENAME).write_text('\n'.join(map(json.dumps, events)))
    context = AgentContext()
    agent.populate_context_post_run(context)
    trajectory = Trajectory.model_validate_json((tmp_path / 'trajectory.json').read_text())
    assert trajectory.session_id == 'session-first'
    assert trajectory.steps[0].message == agent._instruction
    assert trajectory.steps[1].tool_calls[0].arguments == {'page': 1}
    assert trajectory.steps[1].observation.results[0].content == '查询结果'
    assert trajectory.steps[-1].message == reply
    assert not trajectory.steps[-1].tool_calls
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (30, 10, 4)


def test_deepseek_incomplete_stream_does_not_fabricate_terminal_reply(tmp_path):
    agent = DeepSeekHarness(tmp_path, model_name='glm-5.3-flash')
    (tmp_path / agent.OUTPUT_FILENAME).write_text('\n'.join(map(json.dumps, [
        {'type': 'session', 'sessionId': 'session-first'},
        {'type': 'error', 'message': 'synthetic runtime failure'},
    ])))
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert not (tmp_path / 'trajectory.json').exists()
    assert context.metadata['has_final'] is False
