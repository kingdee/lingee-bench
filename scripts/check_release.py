#!/usr/bin/env python3
"""Check the public repository and, when downloaded, the datasets.

Two layers:

- repository: always runs; needs nothing beyond a fresh clone (no datasets).
- datasets: runs when ``datasets/lg-bench-*`` exists (download per datasets/README.md),
  or is forced with ``--datasets``. ``--write`` refreshes dataset inventories and hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib
from urllib.parse import unquote

import sys

import yaml
from harbor.publisher.packager import Packager

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_gate import scan_paths  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATASETS = Path('datasets')
EXAMPLES = Path('examples/tasks')
ERP_ENV = DATASETS / 'environments/erp-mock-mcp'
FORMAL_ERP_JOBS = Path('configs/jobs/erp-cases')
#: erp-mock sidecar 在 trial 网络里的地址（configs/compose/erp-mock-mcp.yaml）。
ERP_MOCK_UPSTREAM = 'http://erp-mock:8000'
#: datasets/ 下随仓库提交的文件；其余内容都从 Hugging Face 下载，不入库。
DATASETS_TRACKED = {'datasets/README.md', 'datasets/.gitkeep'}
GENERAL = Path('datasets/lg-bench-general-v1.0')
ERP = Path('datasets/lg-bench-enterprise-v1.0')
#: 任务目录下允许存在、但不属于Harbor任务包的辅助文件（人工单题运行脚本与续聊素材镜像）。
AUXILIARY = {'run.sh', 'rounds', 'inputs'}


def public_files(root: Path) -> list[Path]:
    names = subprocess.check_output(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root
    ).decode().split('\0')
    return sorted({root / name for name in names if name and (root / name).is_file()})


def task_packages(root: Path, directory: Path) -> dict[str, Path]:
    """Map每个任务在 `task.toml` 中声明的包名到其任务目录。"""
    return {tomllib.loads((path / 'task.toml').read_text())['task']['name']: path
            for path in sorted((root / directory / 'tasks').iterdir())
            if (path / 'task.toml').is_file()}


def unpackaged_files(task: Path, files: list[Path]) -> set[str]:
    """任务目录中未被Harbor任务包覆盖的文件（排除Python缓存）。"""
    task = task.resolve()
    present = {p.relative_to(task).as_posix() for p in task.rglob('*')
               if p.is_file() and '__pycache__' not in p.parts}
    return present - {p.resolve().relative_to(task).as_posix() for p in files}


def refresh(root: Path) -> None:
    for directory in (ERP, GENERAL):
        manifest = root / directory / 'dataset.toml'
        text = manifest.read_text()
        packages = task_packages(root, directory)
        for entry in tomllib.loads(text)['tasks']:
            task = packages.get(entry['name'])
            if task is None:
                continue
            digest, _ = Packager.compute_content_hash(task)
            text = text.replace(entry['digest'], 'sha256:' + digest)
        manifest.write_text(text)


def _is_dataset_path(name: str | Path) -> bool:
    return Path(name).parts[:1] == DATASETS.parts


def _job_configs(root: Path):
    for config in sorted((root / 'configs/jobs').rglob('*.yaml')):
        if not config.name.endswith('.local.yaml'):
            yield config, yaml.safe_load(config.read_text())


def _job_task_paths(doc: dict) -> list[str]:
    return [r['path'] for r in doc.get('tasks', []) + doc.get('datasets', []) if 'path' in r]


def _mirror_errors(tasks: list[Path], root: Path) -> list[str]:
    errors = []
    for task in tasks:
        mirror = task.parent / 'tests/instruction.md'
        if mirror.is_file() and mirror.read_bytes() != (task.parent / 'instruction.md').read_bytes():
            errors.append(f'instruction mirror differs: {task.parent.relative_to(root)}')
    return errors


def check_repository(root: Path) -> list[str]:
    """Checks that hold for a fresh clone without any downloaded dataset."""
    errors = []
    files = public_files(root)
    for required in ['LICENSE', 'NOTICE', '.github/workflows/check.yml', 'datasets/README.md']:
        if root / required not in files:
            errors.append(f'missing public file: {required}')
    for path in files:
        name = path.relative_to(root).as_posix()
        if _is_dataset_path(name) and name not in DATASETS_TRACKED:
            errors.append(f'dataset file tracked in repository (publish it on Hugging Face): {name}')
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            errors.append(f'external symlink: {name}')
        if name.endswith('.local.yaml') or (name.startswith('.env') and name != '.env.example'):
            errors.append(f'local configuration in release: {name}')
        try:
            text = path.read_text()
        except (UnicodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r'/' + r'Users/[^/\s]+/|/' + r'home/yao/', line):
                errors.append(f'local absolute path: {name}:{lineno}')
            if re.search(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|\bghp_[A-Za-z0-9]{30,}\b|\bsk-[A-Za-z0-9_-]{24,}\b|\bows_[A-Za-z0-9]{16,}\b', line):
                errors.append(f'possible credential: {name}:{lineno}')
            if re.search(r'\.' + r'kingdee\.com\b', line):
                errors.append(f'internal host: {name}:{lineno}')
        if path.suffix == '.md':
            for match in re.finditer(r'(?<!!)\[[^\]\n]+\]\((<?[^\s)]+>?)(?:\s+[^)]*)?\)', text):
                target = match[1].strip('<>').split('#', 1)[0]
                if not target or re.match(r'\w+://|mailto:|app:|plugin:', target):
                    continue
                resolved = (path.parent / unquote(target)).resolve()
                if resolved.exists():
                    continue
                # 指向未下载题集的链接在下载后才能解析，不算断链。
                if resolved.is_relative_to((root / DATASETS).resolve()):
                    continue
                errors.append(f'broken link: {name} -> {target}')
    examples = sorted((root / EXAMPLES).glob('*/task.toml'))
    if not examples:
        errors.append(f'{EXAMPLES}: no example tasks')
    errors += _mirror_errors(examples, root)
    for config, doc in _job_configs(root):
        paths = [p for p in _job_task_paths(doc) if not _is_dataset_path(p)]
        paths += (doc.get('environment') or {}).get('extra_docker_compose', [])
        for name in paths:
            if not (root / name).exists():
                errors.append(f'configuration path missing: {config.relative_to(root)} -> {name}')
    return errors


def _binding_errors(root: Path) -> list[str]:
    """逐题核对 HF 题集的 task-bindings.json 与本仓库的正式 job、白名单片段。

    两边描述同一件事（每题绑定的数据集、MCP 路径和工具白名单）：题集是权威来源，
    configs/jobs/erp-cases/ 与 configs/compose/mcp-allow-*.yaml 是运行用的副本，必须逐题一致。
    """
    bindings_file = root / ERP_ENV / 'task-bindings.json'
    if not bindings_file.is_file():
        return [f'{bindings_file.relative_to(root)}: not downloaded (see datasets/README.md)']
    where = bindings_file.relative_to(root)
    errors = []
    # 正式 job：任务目录名 → 它挂载的白名单片段
    jobs: dict[str, str | None] = {}
    for config in sorted((root / FORMAL_ERP_JOBS).glob('*.yaml')):
        doc = yaml.safe_load(config.read_text())
        task = Path(doc['tasks'][0]['path']).name
        fragments = [c for c in (doc.get('environment') or {}).get('extra_docker_compose', [])
                     if Path(c).name.startswith('mcp-allow-')]
        if task in jobs:
            errors.append(f'{config.relative_to(root)}: duplicate formal job for {task}')
        jobs[task] = fragments[0] if len(fragments) == 1 else None
        if len(fragments) != 1:
            errors.append(f'{config.relative_to(root)}: expected exactly one mcp-allow fragment, found {len(fragments)}')
    bindings = json.loads(bindings_file.read_text())
    bound = {item['task'] for item in bindings}
    used_fragments = set()
    for item in bindings:
        task, dataset = item['task'], item['dataset']
        if task not in jobs:
            errors.append(f'{where}: {task} has no job in {FORMAL_ERP_JOBS}')
            continue
        fragment = jobs[task]
        if fragment is None:
            continue
        used_fragments.add(Path(fragment).name)
        if Path(fragment).name != f'mcp-allow-{dataset}.yaml':
            errors.append(f'{where}: {task} binds dataset {dataset}, job mounts {fragment}')
        path = root / fragment
        if not path.is_file():
            continue  # 缺文件已由仓库层的配置路径检查报告
        env = (yaml.safe_load(path.read_text()).get('services') or {}).get('lg-mcp-proxy', {}).get('environment') or {}
        url = str(env.get('LG_MCP_MOCK_URL', ''))
        if url != ERP_MOCK_UPSTREAM + item['mcp_path']:
            errors.append(f'{fragment}: LG_MCP_MOCK_URL {url} != {ERP_MOCK_UPSTREAM}{item["mcp_path"]} ({task})')
        tools = [t.strip() for t in str(env.get('LG_MCP_ALLOWED_TOOLS', '')).split(',') if t.strip()]
        expected = item['allowed_tools']
        if len(tools) != len(set(tools)):
            errors.append(f'{fragment}: duplicate tools in LG_MCP_ALLOWED_TOOLS')
        if set(tools) != set(expected):
            missing = sorted(set(expected) - set(tools))
            extra = sorted(set(tools) - set(expected))
            errors.append(f'{fragment}: tools differ from {where} ({task}); missing {missing}, extra {extra}')
    for task in sorted(set(jobs) - bound):
        errors.append(f'{FORMAL_ERP_JOBS}: job for {task} has no entry in {where}')
    for fragment in sorted((root / 'configs/compose').glob('mcp-allow-*.yaml')):
        if fragment.name not in used_fragments:
            errors.append(f'{fragment.relative_to(root)}: not bound by any task in {where}')
    return errors


def datasets_present(root: Path) -> bool:
    return any((root / directory / 'dataset.toml').is_file() for directory in (ERP, GENERAL))


def check_datasets(root: Path) -> list[str]:
    """Checks of the downloaded datasets against their manifests and the job configurations."""
    errors = []
    for directory in (ERP, GENERAL):
        if not (root / directory / 'dataset.toml').is_file():
            errors.append(f'{directory}: not downloaded (see datasets/README.md)')
    if errors:
        return errors
    for directory, count in [(ERP, 30), (GENERAL, 35)]:
        tasks = sorted((root / directory / 'tasks').glob('*/task.toml'))
        if len(tasks) != count:
            errors.append(f'{directory}: expected {count} tasks, found {len(tasks)}')
        errors += _mirror_errors(tasks, root)
    for directory in (ERP, GENERAL):
        packages = task_packages(root, directory)
        entries = tomllib.loads((root / directory / 'dataset.toml').read_text())['tasks']
        declared = [item['name'] for item in entries]
        if len(declared) != len(set(declared)) or set(declared) != set(packages):
            errors.append(f'{directory}: dataset inventory differs from task directories')
        for item in entries:
            task = packages.get(item['name'])
            if task is None:
                continue
            digest, files = Packager.compute_content_hash(task)
            if item['digest'] != 'sha256:' + digest:
                errors.append(f'{directory}: package digest mismatch: {task.name}')
            for name in sorted(unpackaged_files(task, files)):
                if name.split('/')[0] not in AUXILIARY:
                    errors.append(f'{directory}: unversioned task file: {task.name}/{name}')
    for task in (root / ERP / 'tasks').iterdir():
        for mirror in sorted(p for p in (task / 'rounds').rglob('*') if p.is_file()):
            step = task / 'steps' / mirror.relative_to(task / 'rounds')
            if not step.is_file() or step.read_bytes() != mirror.read_bytes():
                errors.append(f'round mirror differs: {mirror.relative_to(root)}')
        inputs = task / 'inputs/manifest.json'
        if inputs.is_file():
            for item in json.loads(inputs.read_text())['files']:
                path = task / item['task_path']
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    errors.append(f'declared input digest mismatch: {task.name}/{item["task_path"]}')
    for config, doc in _job_configs(root):
        for name in _job_task_paths(doc):
            if _is_dataset_path(name) and not (root / name).exists():
                errors.append(f'configuration path missing: {config.relative_to(root)} -> {name}')
    # ERP 白名单片段把 mock 上游指到 /d/<数据集>/mcp，数据集必须在下载的评测实例里登记。
    instance = root / ERP_ENV / 'instances/erp-benchmark/instance.yaml'
    if not instance.is_file():
        errors.append(f'{ERP_ENV}: erp-benchmark instance not downloaded (see datasets/README.md)')
    else:
        registered = set(yaml.safe_load(instance.read_text()).get('datasets') or [])
        for fragment in sorted((root / 'configs/compose').glob('mcp-allow-*.yaml')):
            for dataset in re.findall(r'/d/([\w-]+)/mcp', fragment.read_text()):
                if dataset not in registered:
                    errors.append(f'{fragment.relative_to(root)}: dataset {dataset} not in {instance.relative_to(root)}')
    errors += _binding_errors(root)
    for archive in (root / GENERAL / 'tasks').glob('*/environment/workspace.tar.gz'):
        with tarfile.open(archive) as tar:
            for member in tar:
                parts = Path(member.name).parts
                if member.name.startswith('/') or '..' in parts or member.issym() or member.islnk():
                    errors.append(f'unsafe archive entry: {archive.parent.parent.name}/{member.name}')
                if any(p.startswith('._') or p in {'.DS_Store', '__MACOSX'} for p in parts):
                    errors.append(f'OS metadata in archive: {archive.parent.parent.name}/{member.name}')
    # 下载内容的泄露扫描：明文与 Excel/压缩包/SQLite 内部（规则见 scripts/data_gate.py）。
    downloaded = [p for p in (root / DATASETS).rglob('*')
                  if p.is_file() and '.git' not in p.parts and '.cache' not in p.parts
                  and p.relative_to(root).as_posix() not in DATASETS_TRACKED]
    errors += scan_paths(downloaded, root)
    return errors


def check(root: Path, *, datasets: bool | None = None) -> list[str]:
    """Repository checks, plus dataset checks when downloaded (or when ``datasets`` is True)."""
    errors = check_repository(root)
    if datasets or (datasets is None and datasets_present(root)):
        errors += check_datasets(root)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--write', action='store_true',
                        help='refresh dataset.toml digests of the downloaded datasets first')
    parser.add_argument('--datasets', action='store_true',
                        help='require and check the downloaded datasets')
    args = parser.parse_args()
    if args.write:
        refresh(ROOT)
    datasets = True if args.datasets or args.write else None
    errors = check(ROOT, datasets=datasets)
    layers = ['repository'] + (['datasets'] if datasets or datasets_present(ROOT) else [])
    print(json.dumps({'status': 'failed' if errors else 'passed', 'layers': layers, 'errors': errors},
                     ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
