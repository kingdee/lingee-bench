"""The source release stays usable without private development material."""
import importlib.util
from pathlib import Path

from conftest import requires_datasets, requires_general

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_check', ROOT / 'scripts/check_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_repository_checks_pass_without_datasets():
    assert release.check_repository(ROOT) == []


@requires_datasets
def test_downloaded_datasets_match_their_manifests():
    assert release.check_datasets(ROOT) == []


def test_examples_are_the_only_bundled_tasks():
    names = {p.parent.name for p in (ROOT / 'examples/tasks').glob('*/task.toml')}
    assert names == {'ap-three-way-match', 'erp-mock-budget-scheme-smoke'}
    assert not list((ROOT / 'datasets').glob('*/tasks')) or (ROOT / 'datasets/README.md').is_file()


@requires_general
def test_general_builds_choose_architecture_and_verify_node_download():
    for p in (ROOT / 'datasets/lg-bench-general-v1.0/tasks').glob('*/environment/Dockerfile'):
        text = p.read_text()
        assert 'dpkg --print-architecture' in text
        assert 'sha256sum --check' in text
        assert 'npm i -g opencode-ai' not in text
