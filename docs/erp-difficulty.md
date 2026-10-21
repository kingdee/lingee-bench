# ERP固定30题：L1—L4难度分级

规则版本：`erp-task-difficulty-v3.1`；发布日期：2026-10-18；题集范围见 `datasets/lg-bench-enterprise-v1.0/dataset.toml`。

该分级衡量公开任务要求Agent承担的业务工作，是作者结构审阅后的先验标签，尚未经过多模型重复实验校准。它不改变题面、评分、金标、权重、超时或样本选择；也不按某个harness的得分、耗时、调用次数或输出长度定级。

## 名称与目录

任务目录和`task.toml`的`[task].name`使用`-L1`至`-L4`难度后缀。例如：

| 用例 | 发布目录 | task.name |
|---|---|---|
| TC-02 | `finance-tc-02-L1` | `kingdee/tc-02-L1` |
| BGT-01 | `finance-bgt-01-L2` | `kingdee/bgt-01-L2` |
| QA-11 | `manufacturing-qa-11-L4` | `kingdee/qa-11-L4` |

`metadata.difficulty`须与名称后缀一致。`case_id`标识业务用例，mock数据集和MCP白名单由正式Job绑定；`dataset.toml`按`task.name`登记正式题集，目录名与任务名的对应关系见上表。

## 统一规则

先写出`已给事实/服务结果 → Agent必须建立的关系 → 必须完成的计算或判断 → 可验收交付`，扣除服务已经完成的计算、预测、找环或风险结果，判断剩余的必要工作。

| 等级 | 必要工作 | 边界 |
|---|---|---|
| L1 事实读取 | 定位已有事实并保真展示 | 已有字段的筛选/分组、分页及口径保真不自动升级 |
| L2 局部规则加工 | 派生新指标/汇总、按业务规则形成分类或风险判断，或实现局部交互 | 多个独立计算、多图、多文件不自动升级 |
| L3 关联与流程 | 建立跨来源、粒度、对象或状态关系，再按明确业务口径推进计算、解释或处理 | 主要难点是关系正确和状态一致；既定触发条件下的指定建议仍属规则处理 |
| L4 综合研判与方案 | 综合具体业务事实，形成规则未直接给出的实质诊断、整体评价或针对性方案并论证；也包括复杂协调、冲突鉴别和连带重建 | 不因“分析/建议”关键词升级，也不额外强制全局优化、唯一因果证明或执行闭环 |

专业研判进入L4须同时满足：

1. 公开题面实质要求，且已有判据验收。
2. 现成服务、公式、阈值或固定映射未直接给出该判断或方案。
3. 必须结合本题多类事实或目标具体论证，抄表或泛化建议不足以完成。

复杂协调/重建也可使用固定规则，不能因为有规则一概降级。去掉领域名称、合并重复章节之后，决定性环节仍应存在；不做该环节必须会影响实质验收。不从题目未禁止的高级做法中制造新要求。

同一标准适用于财务、制造和供应链，按各题必需工作逐题分类，不要求每个领域都覆盖四级。

## 领域分布

| 领域 | L1 | L2 | L3 | L4 | 合计 |
|---|---:|---:|---:|---:|---:|
| 财务 | 1 | 4 | 6 | 3 | 14 |
| 制造 | 1 | 0 | 5 | 1 | 7 |
| 供应链 | 1 | 5 | 2 | 1 | 9 |
| 合计 | 3 | 9 | 13 | 5 | 30 |

L4包括FA-20、cn-COST-006、FA-16、QA-11和TC-059。TC-041仅对冻结空窗实例定L2。类别数量不求相等，每领域也不要求四个级别都出现。

## 分级示例

| 任务 | 已给规则之外仍须完成的工作 | 当前级别 |
|---|---|---|
| FA-20 | 根据损益、现金流、资产质量、偿付支撑等具体信息形成经营整体评价，不能只列报表与比率 | L4 |
| TC-059 | 结合客户/物料贡献、交付和回款事实形成风险研判及针对问题的下月行动 | L4 |
| QA-11 | 据生产、维修、暴露和出货事实论证S/O/D并设计具体遏制措施；RPN公式没有提供这些依据和措施内容 | L4 |
| cn-COST-006 | 场景2须基于实际嵌套问题作具体流程诊断及四档措施；服务已找环，环算法不算Agent工作 | L4 |
| FA-16 | 阈值扫描之外须结合具体业务事实解释异常，并给出对应管理建议 | L4 |
| BGT-05 | 预警优先级、阈值、外推与净占用口径已给；关联单据并解释剩余结构，不要求另设计管理方案 | L3 |
| QC-029 | 依照明确分母、Top5和工序规则解释两日变化，区分现象与待验证原因，未要求完成原因诊断或具体处置方案 | L3 |
| TC-053 | 已给负余额/超额和严重逾期情景，已审放货建议方向明确，属于局部风险判断 | L2 |

## 30题清单

难度及规则版本记录在各任务的`task.toml`中。

| 用例 | 任务 | 任务版本 | 难度 | 分级依据 | 状态 |
|---|---|---|---|---|---|
| [BGT-01](../datasets/lg-bench-enterprise-v1.0/tasks/finance-bgt-01-L2/task.toml) | 预算执行进度 | 0.3.6 | **L2** | 按明示的总体/维度口径整理预算执行结果并计算执行率；不要求跨单据诊断或占用处置。 | 已分级 |
| [BGT-05](../datasets/lg-bench-enterprise-v1.0/tasks/finance-bgt-05-L3/task.toml) | 预算监控与预警 | 0.3.6 | **L3** | 执行、完整月份进度和净占用口径共同支持三类预警、单据缺口和剩余结构解释。 | 已分级 |
| [BR-01](../datasets/lg-bench-enterprise-v1.0/tasks/finance-br-01-L3/task.toml) | 银行存款对账 | 0.3.6 | **L3** | 关联银行对账单和日记账，按余额与历史未达关系选择处理路径并保持确认状态。 | 已分级 |
| [CAS-01-0](../datasets/lg-bench-enterprise-v1.0/tasks/finance-cas-01-0-L2/task.toml) | 出纳结账检查首轮 | 0.3.6 | **L2** | 当前首轮汇集启用检查项与异常，形成有口径的检查简报和HTML。 | 已分级 |
| [CMS-02](../datasets/lg-bench-enterprise-v1.0/tasks/finance-cms-02-L3/task.toml) | 成本关账检查与异常处理 | 0.3.6 | **L3** | 检查结果关联业务单据，按异常类型分流处理，区分首次检查、处理和复验状态。 | 已分级 |
| [cn-COST-006](../datasets/lg-bench-enterprise-v1.0/tasks/finance-cn-cost-006-L4/task.toml) | 嵌套成本分析独立场景 | 0.3.6 | **L4** | 场景2须从实际嵌套环和业务明细形成具体流程诊断及对应四档措施；系统给出环，不代做研判和措施设计。 | 已分级 |
| [FA-11](../datasets/lg-bench-enterprise-v1.0/tasks/finance-fa-11-L2/task.toml) | 本期毛利汇总互动报告 | 0.4.6 | **L2** | 当期四指标及明确公式关系、单位展示和有效交互是当前必要工作；服务已有当期汇总。 | 已分级 |
| [FA-16](../datasets/lg-bench-enterprise-v1.0/tasks/finance-fa-16-L4/task.toml) | 三报表异常诊断 | 0.4.0 | **L4** | 阈值扫描之外，必须把具体业务信息解释为异常原因并形成对应管理建议；属于专业诊断。 | 已分级 |
| [FA-20](../datasets/lg-bench-enterprise-v1.0/tasks/finance-fa-20-L4/task.toml) | 综合经营分析 | 0.4.5 | **L4** | 须解释具体损益、资产质量和偿付支撑并形成整体经营评价，已给报表及比率不能直接替代这些专业判断。 | 已分级 |
| [GL-03](../datasets/lg-bench-enterprise-v1.0/tasks/finance-gl-03-L3/task.toml) | 工资计提凭证编制 | 0.4.6 | **L3** | 工资项目、部门核算维度和账簿科目需对应，拆分及汇总共同形成凭证。 | 已分级 |
| [GL-04](../datasets/lg-bench-enterprise-v1.0/tasks/finance-gl-04-L2/task.toml) | 总账结账检查 | 0.3.6 | **L2** | 一个账簿/当前期内按已知状态和分级组织检查结果，并给对应处理提示。 | 已分级 |
| [INV-007](../datasets/lg-bench-enterprise-v1.0/tasks/finance-inv-007-L3/task.toml) | 确认映射后恢复发票匹配 | 0.3.6 | **L3** | 用户确认映射作用于原批次，需保持发票/映射身份并区分保存与恢复匹配结果。 | 已分级 |
| [TC-02](../datasets/lg-bench-enterprise-v1.0/tasks/finance-tc-02-L1/task.toml) | 往来对账总览与续展 | 0.3.6 | **L1** | 直接采用系统已返回的diffAmt及汇总行，按零/非零常规筛选分组并说明既定镜像视角；题面禁止自行重算差额，不要求新增计算或业务处置判断。 | 已分级 |
| [WLDZ-S02](../datasets/lg-bench-enterprise-v1.0/tasks/finance-wldz-s02-L3/task.toml) | 客户对账函创建与发送 | 0.3.6 | **L3** | 发送对象依赖本次创建成功、审核及邮箱状态，必须保持各业务阶段对象对应。 | 已分级 |
| [OM-002](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-om-002-L3/task.toml) | 车间生产进度日报 | 0.3.6 | **L3** | 分录、关联汇报、返修/退库与时间期望需对应，才能形成有效进度和异常结论。 | 已分级 |
| [OM-029](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-om-029-L3/task.toml) | 组织生产进度日报 | 0.4.0 | **L3** | 关联工单分录、数量和状态，按明示业务规则形成生产日报。 | 已分级 |
| [QA-11](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-qa-11-L4/task.toml) | D3风险评估与遏制计划 | 0.3.6 | **L4** | 须结合生产、维修、暴露与出货事实论证S/O/D并形成具体遏制措施；RPN公式及风险阈值没有代做证据评估和措施设计。 | 已分级 |
| [QC-001](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-qc-001-L1/task.toml) | 工序质检明细查询 | 0.3.6 | **L1** | 展示固定审核窗口内原始检验明细，不要求派生质量率或风险判断。 | 已分级 |
| [QC-010](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-qc-010-L3/task.toml) | 工序质检分析看板 | 0.3.6 | **L3** | 批次、巡检行与缺陷明细的统计关系需建立，并共同支撑风险图和一致解读。 | 已分级 |
| [QC-016](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-qc-016-L3/task.toml) | 高风险工序开启全检 | 0.3.6 | **L3** | 计算结果和已有全检状态共同决定受控处理对象，保留失败/跳过等状态。 | 已分级 |
| [QC-029](../datasets/lg-bench-enterprise-v1.0/tasks/manufacturing-qc-029-L3/task.toml) | 两日质量对比与工序定位 | 0.5.6 | **L3** | 两日统计粒度与分母、缺陷Top5并集及工序增幅需对应，解读应与这些关系一致。 | 已分级 |
| [FC-002](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-fc-002-L2/task.toml) | 既有需求预测结果报告 | 0.3.6 | **L2** | 按任务定义映射期间并提供与目标对象关联的有效详情交互；预测结果已由服务生成。 | 已分级 |
| [PA-01-0](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-pa-01-0-L1/task.toml) | 可对账入库概况首轮 | 0.3.6 | **L1** | 正确定位并展示已有维度候选汇总，当前首轮不生成或审核对账单。 | 已分级 |
| [SCCT-01](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-scct-01-L2/task.toml) | 地区风险集中度分析 | 0.4.5 | **L2** | 选择一致的实体/节点/金额口径比较已聚合地区，形成有依据的集中区域结论。 | 已分级 |
| [TC-017](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-017-L3/task.toml) | 信用前置的发货转换预览 | 0.3.6 | **L3** | 信用风险须与可转换订单的资格和确认边界对应，涉及不同业务对象的关联判断。 | 已分级 |
| [TC-041](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-041-L2/task.toml) | 固定交期窗口履约扫描 | 0.4.6 | **L2** | 当前空窗仍需说明排序/跳过、执行状态及确认边界；只触发局部规则判断，不给未发生的逐单履约链计复杂度。 | 当前冻结空窗实例 |
| [TC-053](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-053-L2/task.toml) | 信用风险与放货建议 | 0.3.6 | **L2** | 分方案/币种解释余额、占用与逾期，按明确异常信号给放货建议。 | 已分级 |
| [TC-059](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-059-L4/task.toml) | 销售月报与下月行动 | 0.4.5 | **L4** | 须结合贡献、交付和回款事实形成风险研判，并提出对应本月具体问题的下月行动；排序及异常标记不能直接给出行动内容。 | 已分级 |
| [TC-068](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-068-L3/task.toml) | 收款跟进与后续催收预览 | 0.4.5 | **L3** | 应收与订单/发货事实关联，重叠款项需识别，续轮催收对象保持前轮上下文和账龄范围。 | 已分级 |
| [TC-076](../datasets/lg-bench-enterprise-v1.0/tasks/supply-chain-tc-076-L2/task.toml) | 销售订单交付异常 | 0.4.5 | **L2** | 在订单行内应用明确的逾期未发/已发未签收规则并按客户、订单和行分别统计。 | 已分级 |

## 元数据接口

每题`task.toml`的`[metadata]`使用以下字段：

```toml
difficulty = "L4"
difficulty_rule_version = "erp-task-difficulty-v3.1"
difficulty_status = "classified"
```

`difficulty`取`L1`、`L2`、`L3`、`L4`。`difficulty_status`说明分类适用范围：

- `classified`：当前结构已分级，不代表全部业务输入已独立验收或完成模型实测。
- `frozen-empty-window`：TC-041，只覆盖当前空窗实例，不能外推非空完整履约链。

这是ERP固定30题的四级量表，不将示例任务的easy/medium/hard或其他数据集的同名L标签自动视为已按该量表校准。读取标签时同时读取规则版本与状态。分级不改变聚合分数或难度权重，分级本身不替代业务验收。

## 维护与验证范围

任务元数据随题集更新，并随新的题集版本发布。
[`dataset.toml`](../datasets/lg-bench-enterprise-v1.0/dataset.toml)固定当前30题范围，
并按`task.name`记录各题Harbor包摘要；版本以Git提交或tag标识。

更新任务时同步难度、目录后缀和`task.name`，递增任务版本，并按
[题集维护流程](../datasets/lg-bench-enterprise-v1.0/README.md)完成原生schema、相关回归和文件完整性检查。
历史jobs及成绩保留原档，静态校验不代表重新执行了业务评测。

TC-041按当前空结果场景分级。
更换业务输入、工具能力、轮次或验收范围时应按同一规则重审。
评分和比较边界见[ERP评分说明](erp-scoring.md)。
