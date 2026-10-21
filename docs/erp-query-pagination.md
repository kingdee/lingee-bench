# 查询分页

启用`returns.history.paginate`的查询按记录分页返回完整字段，单页受字节预算约束。分页由引擎统一实现。

## 调用与返回

- `offset`：从 0 开始的匹配记录位置，默认 0。
- `limit`：请求的最多记录数，默认 50；不超过配置的 `history.limit`（当前生成值为 500）。
- 每页完整 JSON（包括分页元信息和续读提示）的 UTF-8 字节数不超过 **16384**。
- 业务筛选后按查询排序及稳定记录 ID 排序，再按记录分页。页内记录保留全部已声明字段。

```json
{
  "latest": {"说明": "完整匹配集的首条参考记录"},
  "history": ["本页实际返回的完整记录"],
  "offset": 0,
  "returned_count": 24,
  "next_offset": 24,
  "has_more": true,
  "continuation": "还有记录；保持其他查询参数不变，使用 offset=24 继续读取。"
}
```

请求 50 条不一定返回 50 条。模型应保持原业务筛选与其他参数不变，将 `next_offset` 作为
下一次的 `offset`，直到 `has_more=false`、`next_offset=null`。统计时只遍历 `history`；
`latest` 在各页都是相同的首条参考，不能重复累加。空结果或越过末尾时返回空页并明确结束。

`history.limit` 在分页模式下是单页请求上限，不再截断完整匹配集。字节预算根据完整记录
的实际序列化结果计算，下一页按实际返回数量推进，避免请求 50 条、返回 24 条却跳过 26 条。
分页模式不使用表示有损截断的 `truncated` 标记；剩余记录由 `has_more` 与 `next_offset` 表达。
如果一条完整记录及元信息都超预算，返回 `LIMIT_EXCEEDED`，要求出题侧修复配置，禁止返回
无进展的“成功空页”。

## 检查与使用

在仓库根目录运行分页相关回归：

```bash
uv run --directory erp-mock-mcp pytest tests/unit/tools/test_query_pagination.py -q
```

分页记录中的业务日期、组织及统计口径以任务要求为准。修改查询声明后须重建mock镜像，再使用正式Job运行。
