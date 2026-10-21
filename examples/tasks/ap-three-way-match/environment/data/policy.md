# 澄明制造：应付核对制度（虚构）

业务日期固定为 2026-09-30；金额单位为人民币分，数量为整数，不计税或汇率。
本批每张发票最多对应一个采购单；不同非重复发票的采购单不重叠。
有效收货只计算状态为 accepted 的记录，cancelled 不计入。发票金额 = 数量 × 单价。

逐票按以下优先级取第一个命中的决策与原因码：

1. 相同 supplier_id + invoice_number 为重复组，invoice_id 字典序最小者为原件；其余为 blocked / duplicate_invoice。
2. 采购单不存在：blocked / missing_po。
3. 供应商与采购单不一致：blocked / supplier_mismatch。
4. 采购单 approval_status 不是 approved：blocked / po_not_approved。
5. 发票数量超过采购单数量或 accepted 收货数量合计：blocked / quantity_exceeds_received。
6. 发票单价与采购单单价不一致（零容差）：blocked / price_mismatch。
7. 金额严格大于 1000000 分：needs_approval / amount_requires_approval。
8. 其余：ready_for_review / matched。等于 1000000 分不触发第 7 条。

ready_for_review 只表示可以交给有权审批者审阅，不表示允许当前岗位支付。
所有记录都须列出关联采购单及其 accepted 收货 ID（无论最终原因是什么）；不存在采购单时为 []。
只读输入，不可更改任何 data 文件，不可产生实际支付；payments 必须是空列表。
