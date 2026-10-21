# ERP评分说明

当前ERP正式范围为[`dataset.toml`](../datasets/lg-bench-enterprise-v1.0/dataset.toml)登记的30道任务。
各题的题面、评分文件和版本随题集发布在Hugging Face（见[题集](../datasets/README.md)），文件完整性以
[`dataset.toml`](../datasets/lg-bench-enterprise-v1.0/dataset.toml)中的Harbor包摘要校验，发布版本以Git提交或tag固定。

## 内容与数值

内容Judge依据各题的criteria和已采集证据评分；数值层独立核验公开题面规定的
表名、列名、业务键、期间、类型、单位及缺失和冲突处理。最终权重以各题评分配置为准。
评分器按公开接口读取，不从邻近数字、别名猜测、正文或未指定附件补数。

数值计分采用 `numeric-items-v2`：正确独立字段数÷应评分字段总数。
同组一个字段错误不撤销其他字段的得分；必需项缺失或非法仍计入分母。
真正未报告的可选项不参与，已报告但错误或非法的可选项保留在分母。
重复引用不扩大项数，冲突值不得择优，公式按应评应用逐项汇总。
表外正确数字不能补救公开接口内错误或缺失的项。

内容Judge的实际请求固定 `reasoning_effort: "low"`，使用有限判据编号与证据编号。
未知、重复、漏项等无效协议结果记为 `evaluation_error`，不修补返回内容或伪装为候选业务零分。
连接通过 `LG_EVAL_JUDGE_URL`、`LG_EVAL_JUDGE_MODEL`、`LG_EVAL_JUDGE_API_KEY` 配置。

## 成绩与故障

- `graded`：正式评分完成；均值报告该桶的实际分母。
- `evaluation_error`：评分器、取证或Judge协议失败，单独统计。
- `execution_error`：Harbor执行异常，单独统计。
- `no_reward`、`pending`：分别表示结束后缺评分、尚无完成结果，不按零分补齐。

多轮任务按各题声明的末步评分；不把首轮快照当最终结果。
续聊、数据集及工具白名单由正式逐题job明确配置，跨harness比较须使用同一题目、输入、
评分版本和运行预算。新契约后的正式业务成绩须重新运行；旧输出回放仅证明对应的诊断范围。
格式样例或Judge接口回放通过不代表Agent已完成业务任务。

## 输入与版本

任务适用的组织、期间、单位及业务口径以公开题面为准。按同一Git、任务、输入及评分版本报告成绩；更换版本后重新运行，历史结果保留原档。
