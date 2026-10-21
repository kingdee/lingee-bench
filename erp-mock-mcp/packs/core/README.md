# core 业务包

示例业务包的基础资料包（docs/packs/example-p2p.md 第 3 节），purchase 与 inventory 都依赖它。
没有单据：单据、下推、反写、联动和包级规则都在那两个包里。

## 业务范围

八个实体，分成共享基础数据与业务数据两类：

| 实体 | 数据来源 | ID 前缀 | 编码 | 关键字段 |
|---|---|---|---|---|
| org 组织 | 共享（seed/shared/01-org.yaml） | org | 写定 | code、name、is_default |
| department 部门 | 共享（02-department.yaml） | dept | D + 3 位 | name、org_id、kind |
| employee 员工 | 共享（03-employee.yaml） | emp | E + 3 位 | name、department_id、role |
| unit 计量单位 | 共享（04-unit.yaml） | unit | 写定 | code、name |
| warehouse 仓库 | 共享（05-warehouse.yaml） | wh | WH + 2 位 | name、status |
| supplier 供应商 | 初始数据集 | sup | S + 4 位 | name、short_name、payment_term_days、default_tax_rate、status |
| material 物料 | 初始数据集 | mat | M + 5 位 | name、spec、kind、base_unit_id、purchase_unit_id、tax_rate、standard_cost、status |
| unit_conversion 单位换算 | 初始数据集（随物料） | ucv | 无 | material_id、from_unit_id、factor |

两个上下文实体：org 是组织上下文实体，is_default 为 true 的那一条是缺省组织，单据表头的 org_id
缺省取它；employee 是用户上下文实体，实例配置与初始数据集的 current_user 写它的内部 ID。二者都由
内置工具 business_context_get 返回，本包不为它们生成工具。

物料声明了基本单位、采购单位与换算关系：库存按基本单位记账，引擎据此把单据分录的 qty 折算成
qty_base；采购单据分录的计量单位缺省取采购单位。换算关系的一条记录表示 1 个换算单位折合 factor
个基本单位。

暴露的工具（M0 工具清单中属于 core 的五个，见 example-p2p.md 第 9 节）：

- `supplier_search`、`supplier_get`
- `material_search`、`material_get`（get 附带全部单位换算关系）
- `warehouse_search`

另外声明但不暴露的工具：`department_search`、`employee_search`。example-p2p.md 第 8 节的 core
工具面有这两个，M0 工具清单没有，所以 operations 保留、由 tools/exposure.yaml 的 exclude 去掉
（需求 21 第 4 条）；配置保留下来，供领域层、初始数据集构建和包级测试使用。

## 使用的原语

| 原语 | 用在哪里 |
|---|---|
| crud | 八个实体的新建、修改、删除、检索与详情 |
| numbering | 内部 ID，以及 department、employee、warehouse、supplier、material 的编码生成 |
| unit_conversion | 物料的 `unit_conversion` 声明：基本单位字段与换算关系实体 |

本包不使用 state_machine、pricing、conversion、writeback、inventory、rule_expr：没有单据、下推、
反写、库存联动，也没有 enforced 规则。manifest 的 primitives 只列上面三个，多声明一个未使用的原语
不会被加载校验拦住，但会让包的依赖面看起来比实际大。

## 已知简化

- 工具缺口：M0 只暴露读工具。基础资料没有 create、update、delete 工具，仓库也没有 get；
  供应商和物料由初始数据集提供，运行期只能读不能增删改。需要新的基础资料时改数据集并重建快照。
  部门、员工、计量单位和单位换算完全不生成工具：部门与员工的检索工具被 exclude 去掉，计量单位和
  换算关系只随 `material_get` 的结果出现。引言中 M0 已知限制列出的工具缺口（采购申请单的 get、
  采购入库单的 get、search、delete、库存调整单的 submit、approve）都在 purchase 与 inventory 包。
- 物料没有分类实体：example-p2p.md 第 3 节的 `category_id` 没有实现，`material_search` 的"分类"
  用 enum 字段 `kind`（原材料、产成品、商品）代替，所以没有多级分类，也没有按分类树检索。
- `material.base_unit_id` 的缺省值写成内部 ID `unit_000001`，即本包共享数据中的第一条计量单位。
  这样数据集才能用 bulk 批量生成背景物料——没有绑定生成器的字段只能取缺省值，而基本单位既不能为空
  也无法由生成器填写。代价是这个缺省值依赖本包共享数据的创建顺序：数据集不引入 core 的共享数据时，
  bulk 生成的物料会因为引用不到计量单位而构建失败。
- 只有一个币别 CNY，金额字段不带币别；供应商没有地址、联系人和银行账户；员工只有岗位，没有权限
  （M0 不做权限校验，所有操作对当前用户开放）；仓库不分组织，也没有库位。
- `payment_term_days` 和 `default_tax_rate` 只是供应商自身的属性：M0 没有应付业务，账期不参与任何
  计算；采购单据分录的税率缺省取物料的 `tax_rate`，不取供应商的缺省税率。
- 共享数据的文件名带序号（`01-` 到 `05-`），因为包内共享数据按文件路径的码点升序创建，而部门引用
  组织、员工引用部门。按实体名命名会把 department.yaml 排到 org.yaml 前面，构建时引用不到组织。
- 工具描述里不写具体取值：本包共享数据的编码（`ORG01`、`D001`、`PCS` 等）都很短，而描述与业务数据
  的交叉检查按子串比对，很容易误报（需求 3 第 5 条）。需要举例时取 `seed/examples/` 中的示例数据。
