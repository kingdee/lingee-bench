# 任务编写

任务采用Harbor原生目录布局，企业管理和通用任务分别维护。正式题目、输入和评分文件的版本须保持一致，新增题目时同步题集清单、摘要和对应Job。

## 原生任务

1. 在`datasets/<dataset>/tasks/<task-id>/`创建任务，使用`task.toml`声明任务身份、版本、资源和评分设置；单步与多步布局均按Harbor schema校验。
2. `instruction.md`说明业务目标、日期、组织、单位、输出位置及允许的状态变更。必要业务规则必须对Agent可见。
3. 业务输入放入`environment/`构建上下文；评分文件放入`tests/`。Agent环境不得包含参考答案、评分金标或凭据。
4. 多步任务分别声明各步用户指令，后续用户回复或授权只在对应步骤提供。需要原生续聊时，在Job中显式配置`resume_trajectory`。
5. 评分器依据公开要求检查结果。缺失或错误交付按任务规则评分；基础设施和Judge故障单独报告。多解任务按业务约束评分。
6. 为必要约束准备正确与错误样例，覆盖遗漏、重复、单位、期间、对象绑定及状态变化等边界。业务事实须能由输入独立核验。
7. 更新任务版本与题集摘要，在仓库根目录运行校验和相关回归。

```bash
uv run lg-bench validate --path datasets/<dataset>/tasks
uv run python scripts/check_release.py
uv run pytest -q
```

输入与来源要求见[输入资产](source-review.md)，成绩报告及版本比较见[运行与评分约定](design.md)。

## 本地示例任务

`examples/tasks/ap-three-way-match`额外提供本地参考解。`lg-bench smoke`复制输入到临时工作区，顺序运行`solution/solve.sh`和`tests/test.sh`；脚本接受`BENCH_WORKSPACE`、`BENCH_LOG_DIR`，默认路径为`/workspace`和`/logs/verifier`。

该示例金额使用整数分，以JSON业务产物评分。参考解应得1，空提交和必要约束负例应得0。原生任务校验不要求每题都提供本地参考解，本地smoke也不代表真实Agent成绩。

```bash
uv run lg-bench smoke --path examples/tasks/ap-three-way-match
```
