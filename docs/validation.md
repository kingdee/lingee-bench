# 检查命令

在仓库根目录执行。需要Python 3.12、uv和Docker；静态检查不调用模型。

## 发布文件与任务

```bash
uv run python scripts/check_release.py
uv run lg-bench validate
uv run lg-bench validate --path datasets/lg-bench-enterprise-v1.0/tasks
uv run lg-bench validate --path datasets/lg-bench-general-v1.0/tasks
```

发布检查核对题目范围、摘要、配置路径和公开文件，发现不一致时退出码非零。

## 回归

```bash
uv run pytest -q
uv run lg-bench smoke
uv run --directory erp-mock-mcp pytest tests/ -q
```

测试需要绑定本机回环端口。模型运行另用正式Job，静态检查及本地参考解不代表Agent能力成绩。

## ERP输入与镜像

```bash
uv run --directory erp-mock-mcp erp-mock-mcp validate \
  --instance ../datasets/environments/erp-mock-mcp/instances/erp-benchmark \
  --packs-dir ../datasets/environments/erp-mock-mcp/packs
```

评测实例随题集下载（见[题集](../datasets/README.md)）。更新题集后重建镜像；镜像中的输入必须与下载的题集版本一致。

## 运行结果

原始结果保存在`jobs/`，包括配置、轨迹、产物及verifier输出。汇总时分别统计正常评分与执行/评分故障；仅使用同一题目、输入、评分和预算版本比较成绩。不要将缺失评分或Judge故障补成业务零分。
