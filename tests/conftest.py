"""测试公共约定。

正式题集不随仓库分发，按 datasets/README.md 从 Hugging Face 下载到 ``datasets/``。
依赖题集的用例统一用 :data:`requires_datasets` 标记：题集缺失时跳过（不是失败），
下载后再跑一遍 ``uv run pytest`` 就会覆盖它们。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTERPRISE = ROOT / "datasets/lg-bench-enterprise-v1.0"
GENERAL = ROOT / "datasets/lg-bench-general-v1.0"
ERP_ENVIRONMENT = ROOT / "datasets/environments/erp-mock-mcp"
DOWNLOAD_HINT = "题集未下载，见 datasets/README.md"


def requires(*paths: Path):
    missing = [p.relative_to(ROOT).as_posix() for p in paths if not p.exists()]
    return pytest.mark.skipif(bool(missing), reason=f"{DOWNLOAD_HINT}：缺少 {', '.join(missing)}")


requires_enterprise = requires(ENTERPRISE / "dataset.toml")
requires_general = requires(GENERAL / "dataset.toml")
requires_datasets = requires(ENTERPRISE / "dataset.toml", GENERAL / "dataset.toml")
