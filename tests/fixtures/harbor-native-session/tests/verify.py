"""Deterministic session smoke; no ERP, judge, or filesystem-based memory credit."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(trajectory, capture_dir, round_number):
    raw = trajectory.read_bytes()
    data = json.loads(raw)
    steps = [s for s in data['steps'] if not s.get('is_copied_context')]
    terminal = steps[-1]
    assert terminal['source'] == 'agent' and not terminal.get('tool_calls'), 'No terminal ATIF reply'
    reply = terminal['message']
    session = data.get('session_id')
    assert session and session != 'unknown', 'No native session identity'
    facts = {
        'session_id': session, 'reply': reply,
        'tool_calls': sum(len(s.get('tool_calls') or []) for s in steps),
        'trajectory_sha256': hashlib.sha256(raw).hexdigest(),
    }
    snapshot = capture_dir / 'round-1.json'
    if round_number == 1:
        capture_dir.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(facts, ensure_ascii=False))
        return facts, {'overall': float(reply.strip() == '已记住' and not facts['tool_calls'])}
    first = json.loads(snapshot.read_text())
    checks = {
        'memory': reply.strip() == '露竹-79241|42',
        'same_native_session': first['session_id'] == session,
        'no_tools': first['tool_calls'] == facts['tool_calls'] == 0,
        'current_user_collected': any(s['source'] == 'user' and '请回复上轮' in s['message'] for s in steps),
        'distinct_round_evidence': first['trajectory_sha256'] != facts['trajectory_sha256'],
    }
    return {**facts, 'checks': checks}, {'overall': float(all(checks.values()))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--round', type=int, choices=[1, 2], required=True)
    parser.add_argument('--trajectory', type=Path, default=Path('/logs/agent/trajectory.json'))
    parser.add_argument('--capture-dir', type=Path, default=Path('/opt/lg-session-smoke'))
    parser.add_argument('--log-dir', type=Path, default=Path('/logs/verifier'))
    args = parser.parse_args()
    facts, reward = verify(args.trajectory, args.capture_dir, args.round)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    (args.log_dir / 'results.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
    (args.log_dir / 'reward.json').write_text(json.dumps(reward))
    print(json.dumps({'round': args.round, 'reward': reward, **facts}, ensure_ascii=False))
