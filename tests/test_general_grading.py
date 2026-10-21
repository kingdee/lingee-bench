"""Evaluation failures must stay distinct from candidate business failures."""
import importlib.util
import json
from pathlib import Path
import sys
from urllib.error import URLError

import pytest

from conftest import requires_general

pytestmark = requires_general

ROOT = Path(__file__).resolve().parents[1]
GRADERS = sorted((ROOT/'datasets/lg-bench-general-v1.0/tasks').glob('*/tests/grading'))


def module(path):
    spec = importlib.util.spec_from_file_location('grading_' + path.parent.parent.parent.name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize('grading', GRADERS, ids=lambda p:p.parent.parent.name)
def test_transport_failure_is_marked_as_evaluation_error(grading, monkeypatch):
    judge = module(grading/'judge_core.py')
    monkeypatch.setenv('JUDGE_API_KEY', 'unit-test-placeholder')
    monkeypatch.setattr(judge.time, 'sleep', lambda _: None)
    def fail(*args, **kwargs):
        raise URLError('local test: upstream unavailable')
    monkeypatch.setattr(judge, '_chat', fail)
    row = judge.judge_item({'id':'r1','question':'q','pass_criteria':[],'fail_criteria':[]}, 'task', 'answer', 'evidence', 'rules')
    assert row['evaluation_error'] is True


@pytest.mark.parametrize('grading', GRADERS, ids=lambda p:p.parent.parent.name)
def test_invalid_evaluation_removes_reward_files(grading, tmp_path, monkeypatch):
    grader = module(grading/'run_grade.py')
    monkeypatch.setattr(grader, 'LOGS', str(tmp_path))
    for name in ['reward.json','reward.txt']:(tmp_path/name).write_text('1')
    with pytest.raises(SystemExit) as caught:
        grader.emit({'status':'invalid_judge_call_failed'}, {'reward':0}, 2)
    assert caught.value.code == 2
    assert not (tmp_path/'reward.json').exists()
    assert not (tmp_path/'reward.txt').exists()
    assert json.loads((tmp_path/'score.json').read_text())['status']=='evaluation_error'


@pytest.mark.parametrize('grading', GRADERS, ids=lambda p:p.parent.parent.name)
def test_legitimate_zero_remains_a_scored_result(grading, tmp_path, monkeypatch):
    grader=module(grading/'run_grade.py');monkeypatch.setattr(grader,'LOGS',str(tmp_path))
    with pytest.raises(SystemExit):grader.emit({'status':'dual_channel','score':0}, {'reward':0},1)
    assert json.loads((tmp_path/'reward.json').read_text())['reward']==0
    assert json.loads((tmp_path/'score.json').read_text())['status']=='dual_channel'


def test_protocol_does_not_accept_wrong_item_or_boolean_score():
    judge=module(GRADERS[0]/'judge_core.py')
    for row in [{'id':'other','verdict':'pass','score':1}, {'id':'r1','verdict':'pass','score':True}]:
        with pytest.raises(ValueError):judge._normalize_verdict('r1',row)
    assert judge._normalize_verdict('r1',{'id':'r1','verdict':'fail','score':0,'reason':'business evidence missing'})['score']==0


def test_short_nonempty_artifact_reaches_its_business_rules(tmp_path, monkeypatch):
    path=ROOT/'datasets/lg-bench-general-v1.0/tasks/SCH-meeting-date-policy-check-L1/tests/grading/run_grade.py'
    grader=module(path);answer=tmp_path/'result.json';answer.write_text('{"answer":"ok"}')
    (tmp_path/'judge.yaml').write_text('score_merge: {rule_weight: 0.5, llm_weight: 0.5}')
    monkeypatch.setattr(grader,'LOGS',str(tmp_path/'logs'));monkeypatch.setattr(grader,'TESTS',str(tmp_path))
    monkeypatch.setattr(sys,'argv',['run_grade.py',str(answer)]);monkeypatch.setenv('JUDGE_API_KEY','unit-test-placeholder')
    def sh(command):
        value={'passed_count':1,'total_count':1,'rule_score':1,'missing_groups':[]} if command[1].endswith('score_core.py') else {'judge_score':1}
        return 0,json.dumps(value),''
    monkeypatch.setattr(grader,'sh',sh)
    with pytest.raises(SystemExit) as caught:grader.main()
    assert caught.value.code==0
    assert json.loads((tmp_path/'logs/score.json').read_text())['status']=='dual_channel'
