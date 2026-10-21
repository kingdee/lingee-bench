# purchase 业务包

示例业务包的采购主线（docs/packs/example-p2p.md 第 4 到 7 节）：采购申请单、采购订单、采购入库单
三张单据，它们之间的两个下推与反写，采购入库单审核时的库存联动，包级规则 R-PUR-001，以及采购价格
查询。基础资料（供应商、物料、计量单位与换算、仓库、部门、员工）全部取自 `core`，所以本包只依赖它。

## 业务范围

三张单据，都用预置的 standard 状态机（draft、submitted、approved、closed）：

| 单据 | ID 前缀 | 编号前缀 | 计价 | 来源 | 关键字段 |
|---|---|---|---|---|---|
| purchase_requisition 采购申请单 | preq | CGSQ | 否 | 无 | 申请部门、申请人、需求日期；分录为物料、计量单位、申请数量 |
| purchase_order 采购订单 | po | CGDD | 是 | 采购申请单（可选） | 供应商、交货日期；分录为物料、计量单位、数量、单价、税率 |
| purchase_receipt 采购入库单 | prcv | CGRK | 是 | 采购订单（必须） | 供应商；分录为物料、计量单位、入库仓库、数量、单价、税率 |

申请单不计价：申请只说要什么、要多少、什么时候要，单价在下推订单时才由采购员填写。它的分录自己声明
`qty` 字段并在 `line_quantity` 中指定，所以引擎同样为分录添加只读的 `qty_base`，下推额度按基本单位计。
订单与入库单计价，分录的数量取标准字段 `qty`，金额由引擎按含税或不含税口径计算。

两个下推与各自的反写：

| 下推 | 来源到目标 | 源单状态 | 额度 | 调用方逐行输入 | 映射带入 | 反写 |
|---|---|---|---|---|---|---|
| `purchase_requisition_to_order` | 申请单到订单 | approved | 按数量 | `source_line_id`、`qty`、`price` | 表头交货日期取需求日期；分录物料、计量单位 | 订单审核时把分录 `qty_base` 累加到申请行 `linked_qty`，不自动关闭申请单 |
| `purchase_order_to_receipt` | 订单到入库单 | approved | 按数量 | `source_line_id`、`qty`、`warehouse_id` | 表头供应商、单价含税口径；分录物料、计量单位、单价、税率 | 入库单审核时把分录 `qty_base` 累加到订单行 `linked_qty`，订单各行入足后自动关闭 |

两者的 `line_selection` 都是 `by_source_line_id`：目标分录必须指明来源行。额度按源行的 `qty_base`
减去占用数量计，占用统计同一下推生成的全部未删除下游单据，包括草稿；`linked_qty` 只统计已审核和已关闭
的下游单据，由反写维护。两个反写的 `on` 都是 `approve`、`reverse_on` 都是 `unapprove`、`op` 都是
`add`、`value` 都是 `line.qty_base`，只有订单到入库单的那个声明了 `auto_close_source_when:
all_lines_linked`。

一个库存联动：`purchase_receipt_inventory`，`on` 为 `approve`，`direction` 为 `in`，仓库字段为分录的
`warehouse_id`，数量字段为分录的 `qty_base`。审核时每个分录写一条 `+qty_base` 的库存流水并计入
（物料，入库仓库）的即时库存；反审核时删除这些流水并相应减少即时库存。订单与申请单没有库存联动。

一条包级规则：`R-PUR-001`，enforced，`bill` 为 `purchase_order`，`on` 为 create、update、submit，
`check` 为 `header.delivery_date >= header.biz_date`，`error_code` 为 `VALIDATION_FAILED`，`field`
为 `delivery_date`，message 为"交货日期不得早于业务日期"。`on` 中的 create 同时适用于直接新建和由申请单
下推生成。

一个查询工具：`purchase_price_query`，`source` 为 `purchase_order_line`，必填参数 `supplier_id`
（比较表头）与 `material_id`（比较分录），`filter` 为 `header.status in ['approved', 'closed']`，
按 `header.biz_date`、`header.number` 降序，`latest` 返回单价、是否含税、业务日期与编号，`history`
至多 5 条。

暴露的工具（M0 工具清单中属于 purchase 的 14 个，见 example-p2p.md 第 9 节）：

- 采购申请单：`purchase_requisition_search`、`purchase_requisition_create`
- 采购订单：`purchase_order_search`、`purchase_order_get`、`purchase_order_create`、
  `purchase_order_create_from_requisition`、`purchase_order_update`、`purchase_order_submit`、
  `purchase_order_approve`、`purchase_order_close`
- 采购入库单：`purchase_receipt_create_from_order`、`purchase_receipt_submit`、
  `purchase_receipt_approve`
- 查询：`purchase_price_query`

另外声明但不暴露的 13 个工具：`purchase_requisition_` 的 `get`、`update`、`delete`、`submit`、
`approve`；`purchase_order_` 的 `delete`、`unsubmit`、`unapprove`、`unclose`；`purchase_receipt_`
的 `search`、`get`、`delete`、`unapprove`。example-p2p.md 第 8 节的 purchase 工具面有这 27 个，
M0 工具清单只留 14 个，所以 operations 保留、由 `tools/exposure.yaml` 的 `exclude` 去掉
（需求 21 第 4 条）；配置保留下来，供领域层、初始数据集构建和包级测试使用。

## 使用的原语

| 原语 | 用在哪里 |
|---|---|
| crud | 三张单据的增删改查、分页检索与详情，以及 `purchase_price_query` 的选取、排序与取最新 |
| numbering | 内部 ID、分录 ID 和单据编号（CGSQ、CGDD、CGRK 加业务日期与 4 位序号） |
| pricing | 采购订单与采购入库单的 `pricing: true`：分录金额与表头汇总 |
| unit_conversion | 三张单据分录的 `line_quantity` 声明：把采购单位的数量折算成 `qty_base` |
| state_machine | 三张单据的 `state_machine: standard` 与 `operations` |
| conversion | `conversions/` 下的两个下推，含额度校验与源单状态校验 |
| writeback | `writebacks/` 下的两个反写，含订单的自动关闭与自动反关闭 |
| inventory | `linkages/purchase_receipt_inventory.yaml` |
| rule_expr | R-PUR-001 的 `check`，以及 `purchase_price_query` 的 `filter` |

也就是 M0 的全部九个原语。example-p2p.md 第 2 节为 purchase 列的是 state_machine、pricing、
conversion、writeback、inventory、rule_expr——那张表只列 L2 原语，L1 的 crud、numbering、
unit_conversion 由 core 一行带过，本包同样在用，所以 manifest 里一并声明（需求 2 第 6 条）。

本包没有 `seed/` 目录：它不声明实体，没有可绑定生成器的实体字段，也不提供共享基础数据；工具描述里
不写具体取值，需要举例时取 `packs/core/seed/examples/` 中的示例数据。单据由初始数据集的 `entities`
按正常操作生成（新建或下推，再按 `status` 执行提交、审核、关闭）。

## 已知简化

- 工具缺口（引言中 M0 已知限制所列）：
  - 工具清单没有 `purchase_requisition_get`。模型无法通过工具读到申请单的分录 ID 和数量，
    `purchase_requisition_search` 的摘要里也没有分录，所以
    `purchase_order_create_from_requisition` 在 M0 主要由已知 ID 的调用方使用（包级测试、上层用例）。
    已审核的申请单只能来自初始数据集：申请单的 submit 与 approve 工具也不暴露。
  - 工具清单没有采购入库单的 `get`、`search`、`delete`。入库单的 id 只出现在
    `purchase_receipt_create_from_order` 的返回中，建错的草稿既读不到也删不掉，而它会一直占用订单行的
    可下推额度（占用统计含草稿）；需要时通过 Admin API 重置沙箱。
  - 订单的 `unsubmit`、`unapprove`、`unclose`、`delete` 和入库单的 `unapprove` 也不暴露：M0 只走正向
    流程，提交、审核、关闭都不可撤销。反向操作的配置和引擎行为都在，只是没有工具面。
- 申请单到订单的下推不带入供应商：申请单上没有供应商（询价、比价不在 M0 范围内），下推出的订单草稿
  `supplier_id` 为空，要先用 `purchase_order_update` 补上才能提交。下推不因必填字段为空而失败，必填性
  在提交时按 R-GEN-002 校验。
- 订单的交货日期取申请单的需求日期，而 R-PUR-001 在 create 时也求值，所以需求日期早于下推当天业务日期的
  申请单无法下推成订单。初始数据集里用来演示该下推的申请单，需求日期要写在锚点日期当天或之后（写成相对
  日期表达式，如 `month(0).day(28)` 在月末、`today+10d`）。
- 分录的 `tax_rate` 没有允许值列表：它是引擎标准字段，同名声明只能写 label 和三个缺省属性
  （需求 1 第 6 条），所以这里只声明 `default_from: material.tax_rate`。`core` 的 `material.tax_rate`
  仍然约束在 0、0.06、0.09、0.13 四个值内，但调用方可以给订单行填别的税率。
- 入库仓库在分录上，不在表头：example-p2p.md 第 4 节把"仓库"列为入库单的表头关键字段，这里放在分录，
  这样一个订单行才能分几个仓库入库（需求 8 第 7 条），库存联动的仓库字段也因此是 `line.warehouse_id`。
  代价是一张入库单的各行可以指向不同仓库，没有"整单一个仓库"的校验。
- 采购入库单只能由订单下推：`operations` 里没有 `create`，也没有 `update`——下推生成的草稿不允许整体
  替换分录（需求 5 第 4 条），而它的表头除标准字段外只有供应商，由映射带入，没有需要改写的内容。
  不经订单的直接入库、采购退货、暂估入库都不在 M0 范围内。
- 没有应付与成本：入库审核只改库存，不生成暂估凭证，也不产生应付单——应付单、付款单和凭证属于 M1 的
  `ap` 与 `gl` 包。`core` 的供应商账期 `payment_term_days` 因此不参与任何计算。
- 只有一个币别 CNY，金额字段不带币别；订单没有价税合计以外的折扣、运费等费用项；申请单与订单都没有
  审批流，approve 是一步到位的状态推进。
- `purchase_price_query` 只看采购订单分录：已审核和已关闭订单中该（供应商，物料）的单价。入库单的实际
  入库单价、不同计量单位之间的价格折算都不参与，`price` 的口径由同一记录返回的 `price_includes_tax`
  说明。
