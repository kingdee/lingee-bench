# inventory 业务包

示例业务包的库存包（docs/packs/example-p2p.md 第 4、6 节），依赖 core 的物料、计量单位与仓库。
即时库存与库存流水由 inventory 原语维护，本包只声明一张单据与一个查询。

## 业务范围

一张单据：

| 单据 | ID 前缀 | 编号前缀 | 状态机 | 表头 | 分录 |
|---|---|---|---|---|---|
| stock_adjustment 库存调整单 | sadj | KCTZ | standard | warehouse_id 仓库、reason 调整原因（盘盈 / 盘亏） | material_id 物料、unit_id 计量单位、qty 调整数量 |

一张单据只调整一个仓库，所以仓库写在表头；分录声明了数量语义（物料字段、计量单位字段、数量
字段），引擎据此补只读的 qty_base，把 qty 按 core 的换算关系折成基本单位。单据不计价：盘点差异
只有数量，没有单价与金额。

审核联动（adjust 方向）：审核时为每个分录写一条库存流水，表头 reason 为 gain（盘盈）时取
+qty_base、为 loss（盘亏）时取 -qty_base，并计入对应（物料，仓库）的即时库存；反审核时删除这些
流水并冲回。审核或反审核使任一（物料，仓库）的即时库存小于 0 时报 R-GEN-007 并整体回滚。

包级规则：

| 编号 | 执行方式 | 内容 | 落在哪里 |
|---|---|---|---|
| R-INV-002 | described | 库存调整单只用于盘点差异，采购到货必须通过采购入库单入库 | 原样出现在 `stock_adjustment_create` 的 zh-detailed 描述里 |

暴露的工具（M0 工具清单中属于 inventory 的两个，见 example-p2p.md 第 9 节）：

- `stock_adjustment_create`
- `inventory_balance_query`（原语查询：按可选的物料与仓库精确筛选，返回以基本单位计的即时库存）

另外声明但不暴露的工具：`stock_adjustment_submit`、`stock_adjustment_approve`。example-p2p.md
第 8 节的 inventory 工具面有这两个，M0 工具清单没有，所以 operations 保留、由
tools/exposure.yaml 的 exclude 去掉（需求 21 第 4 条）；配置保留下来，供领域层、初始数据集构建和
包级测试使用——数据集或用例写 `status: approved`，Seed_Builder 就会执行提交与审核，从而触发联动。

## 使用的原语

| 原语 | 用在哪里 |
|---|---|
| crud | 库存调整单的新建与读写 |
| numbering | 内部 ID（sadj）与单据编号（KCTZ） |
| state_machine | 单据的 standard 状态机：draft、submitted、approved、closed |
| unit_conversion | 分录的 `line_quantity` 声明：qty 按物料的换算关系折成 qty_base |
| inventory | `linkages/stock_adjustment_inventory.yaml` 的 adjust 联动，以及 exposure.yaml 中的即时库存查询 |

本包不使用 pricing、conversion、writeback、rule_expr：调整单不计价，既不是下推的来源也不是目标，
M0 范围内也没有 enforced 规则（R-INV-002 是 described，不含规则表达式）。

## 已知简化

- 工具缺口：M0 只暴露 `stock_adjustment_create` 与 `inventory_balance_query`。库存调整单没有
  submit、approve 工具，所以运行期只能建草稿，不能从工具层把它审核出库存变化；要让初始库存存在，
  得在初始数据集或包级用例里写 `status: approved`。单据也没有 search、get、update、delete 工具，
  建完之后无法再按 ID 读回或改写。引言中 M0 已知限制列出的另外几个工具缺口（采购申请单的 get、
  采购入库单的 get、search、delete）在 purchase 包。
- 没有调拨单 transfer_order：example-p2p.md 第 4 节的调拨单与 R-INV-001（调出仓与调入仓不得相同）
  都不在 M0 范围内（需求 21 第 4 条），所以 transfer 方向的联动在示例业务包里没有用例，只在引擎
  测试夹具中覆盖。两个仓库之间移库在 M0 只能用一张盘亏加一张盘盈表示。
- 没有库存流水查询 inventory_transaction_search：流水由引擎写入，M0 只能通过
  `inventory_balance_query` 看到汇总后的即时库存，看不到每一条流水及其来源单据。
- 调整原因只有盘盈与盘亏两个取值，并且写在表头：一张单据不能同时登记盘盈行与盘亏行，混在一起时
  要开两张单据。真实系统里的调整原因通常是可配置的基础资料，并且按行记录。
- 没有批次、库位、保质期与成本：即时库存只按（物料，仓库）汇总数量，调整不产生成本与凭证。
- 负库存一律禁止（R-GEN-007），没有"允许负库存"的仓库或物料开关。
- 工具描述里不写具体取值：描述与业务数据的交叉检查覆盖实例内全部业务包的描述，而 core 共享数据的
  编码（计量单位、仓库、组织、部门、员工的编码）都很短，按子串比对容易误报（需求 3 第 5 条）。
