"""冒烟测试一个实例的全部工具（默认 instances/p2p-demo 的 22 个）。

经 stdio 连一个新沙箱，串起一条采购到入库的业务链：查基础资料、按预置的已审核申请单下推、
新建并提交审核采购订单、下推入库并审核、手工关闭一张已审核订单、建库存调整单，中间穿插全部
检索与查询工具。每个工具调用后打印 PASS 或 FAIL，最后给出覆盖的工具数并以退出码表示结果：
0 表示 tools/list 中的每个工具都被调用过且没有返回 isError，1 表示有工具未触达或失败。

这是面向 p2p-demo 的烟雾测试，用来快速确认全部工具都能从命令行走通，不是回归测试；带断言的
回归用 tests/packs 下的声明式用例或 tests/integration 的协议流程。流程里的工具名、部门内部 ID
与检索关键字都是 p2p-demo 专有的，指向别的实例多半会失败。

从仓库根运行::

    uv run python scripts/smoke_tools.py
    uv run python scripts/smoke_tools.py --instance instances/p2p-demo --anchor-date 2026-06-15

被测子进程会被设一个很高的 ERP_MOCK_MAX_CALLS_PER_SECOND：烟雾测试在一秒内发出的调用数超过
默认的每秒 20 次（erp_mock.cli.options 的 ERP_MOCK_MAX_CALLS_PER_SECOND），不调高会在中途被
LIMIT_EXCEEDED 拒绝。沙箱数据写进一个临时目录，结束时删除。

每次 tools/call 的工具名、入参与出参按发送顺序写进 --out 指定的 JSON 文件（缺省 smoke-tools-result.json）。
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# 默认被测对象：仓库自带的示例实例与业务包（erp_mock.cli.options 的默认值）。
DEFAULT_INSTANCE: Final = REPO_ROOT / "instances" / "p2p-demo"
DEFAULT_PACKS_DIR: Final = REPO_ROOT / "packs"
DEFAULT_ANCHOR: Final = "2026-06-15"

# 结果文件缺省路径（相对运行时的工作目录）。
DEFAULT_OUT: Final = "smoke-tools-result.json"

# 安装好的控制台脚本，紧挨着跑本脚本的解释器（scripts/perf.py 用同样的取法）。
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# 把速率上限调高，让烟雾测试的突发调用不被拒绝（见模块文档）。
RATE_LIMIT_VAR: Final = "ERP_MOCK_MAX_CALLS_PER_SECOND"
HIGH_RATE: Final = "100000"

# p2p-demo 专有：第一个仓库的编码、数据集记录名里都带的关键字、共享数据里第一个部门的内部 ID。
WAREHOUSE_CODE: Final = "WH01"
DEMO_KEYWORD: Final = "演示"
DEPARTMENT_ID: Final = "dept_000001"

# 握手里客户端的自称。
CLIENT_INFO: Final = types.Implementation(name="erp-mock-smoke", version="0.0.0")


@dataclass(frozen=True, slots=True)
class Options:
    """一次运行的命令行选项。"""

    instance: Path
    packs_dir: Path
    anchor: str
    data_dir: Path | None
    out: Path


@dataclass(slots=True)
class Smoke:
    """一次冒烟测试的进度：会话与各工具的结果标记。"""

    session: ClientSession
    marks: dict[str, str] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    async def raw(self, tool: str, args: Mapping[str, Any]) -> types.CallToolResult:
        """发一次 tools/call，返回原始结果，不记账。"""
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=tool, arguments=dict(args))
            )
        )
        result = await self.session.send_request(request, types.CallToolResult)
        self.transcript.append(_record(len(self.transcript) + 1, tool, args, result))
        return result

    async def call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """发一次 tools/call，记 PASS/FAIL 并返回其 structuredContent（失败时为空）。"""
        result = await self.raw(tool, args)
        ok = not result.isError
        self.mark(tool, ok, "" if ok else "(isError) " + _text(result)[:80])
        if not ok:
            return {}
        return result.structuredContent or {}

    def mark(self, tool: str, ok: bool, note: str = "") -> None:
        """记录一个工具的结果并打印一行。"""
        self.marks[tool] = "PASS" if ok else "FAIL"
        _emit(f"  {'PASS' if ok else 'FAIL'}  {tool:<40} {note}")


def _text(result: types.CallToolResult) -> str:
    """错误结果的文本内容项，没有则空串。"""
    if result.content:
        first = result.content[0]
        if isinstance(first, types.TextContent):
            return first.text
    return ""


def _record(
    seq: int, tool: str, args: Mapping[str, Any], result: types.CallToolResult
) -> dict[str, Any]:
    """一次 tools/call 的记录：工具名、入参，以及出参（成功）或错误对象（失败）。"""
    entry: dict[str, Any] = {
        "seq": seq,
        "tool": tool,
        "args": dict(args),
        "is_error": bool(result.isError),
    }
    if result.isError:
        entry["error"] = _error(result)
    else:
        entry["output"] = result.structuredContent
    return entry


def _error(result: types.CallToolResult) -> Any:
    """失败结果里的 error 对象；文本不是 JSON 时原样返回文本。"""
    text = _text(result)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed["error"]
    return parsed


def _emit(line: str) -> None:
    """把一行写到 stdout（本脚本不是服务，输出即结果；ruff T20 禁用 print）。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _first_id(data: Mapping[str, Any]) -> str:
    """检索结果第一条记录的内部 ID。"""
    items = data.get("items") or []
    return str(items[0]["id"]) if items else ""


def _by_code(items: Sequence[Any], code: str) -> str:
    """items 中编码为 code 的记录 ID，找不到则取第一条。"""
    for item in items:
        if item.get("code") == code:
            return str(item["id"])
    return str(items[0]["id"]) if items else ""


async def run_flow(smoke: Smoke) -> None:
    """串起覆盖全部 22 个工具的采购到入库业务链。"""
    context = await smoke.call("business_context_get", {})
    biz_date = str(context.get("business_date", ""))

    supplier = _first_id(
        await smoke.call("supplier_search", {"keyword": DEMO_KEYWORD, "status": "enabled"})
    )
    await smoke.call("supplier_get", {"id": supplier})
    materials = (await smoke.call("material_search", {"status": "enabled"})).get("items") or []
    material = str(materials[0]["id"]) if materials else ""
    await smoke.call("material_get", {"id": material})
    warehouses = (await smoke.call("warehouse_search", {"status": "enabled"})).get("items") or []
    warehouse = _by_code(warehouses, WAREHOUSE_CODE)

    await smoke.call("purchase_price_query", {"supplier_id": supplier, "material_id": material})
    await smoke.call("inventory_balance_query", {})

    # 下推：数据集预置了一张已审核的采购申请单（M0 不暴露申请单的提交/审核工具）。
    requisitions = (await smoke.call("purchase_requisition_search", {"status": "approved"})).get(
        "items"
    ) or []
    requisition = str(requisitions[0]["id"]) if requisitions else ""
    await smoke.call(
        "purchase_order_create_from_requisition",
        {
            "requisition_id": requisition,
            "lines": [{"source_line_id": f"{requisition}-1", "qty": "5", "price": "10.00"}],
        },
    )
    await smoke.call(
        "purchase_requisition_create",
        {
            "department_id": DEPARTMENT_ID,
            "need_date": biz_date,
            "lines": [{"material_id": material, "qty": "3"}],
        },
    )

    # 新建采购订单，走完 create → update → submit → approve → get → search。
    order = str(
        (
            await smoke.call(
                "purchase_order_create",
                {
                    "supplier_id": supplier,
                    "delivery_date": biz_date,
                    "lines": [{"material_id": material, "qty": "3", "price": "10.08"}],
                },
            )
        ).get("id", "")
    )
    await smoke.call("purchase_order_update", {"id": order, "remark": "冒烟测试"})
    await smoke.call("purchase_order_submit", {"id": order})
    await smoke.call("purchase_order_approve", {"id": order})
    await smoke.call("purchase_order_get", {"id": order})
    await smoke.call("purchase_order_search", {"status": "approved"})

    # 下推入库并审核，反写后订单自动关闭。
    receipt = str(
        (
            await smoke.call(
                "purchase_receipt_create_from_order",
                {
                    "order_id": order,
                    "lines": [
                        {"source_line_id": f"{order}-1", "qty": "3", "warehouse_id": warehouse}
                    ],
                },
            )
        ).get("id", "")
    )
    await smoke.call("purchase_receipt_submit", {"id": receipt})
    await smoke.call("purchase_receipt_approve", {"id": receipt})

    await _close_an_order(smoke, order)

    await smoke.call(
        "stock_adjustment_create",
        {
            "warehouse_id": warehouse,
            "reason": "gain",
            "lines": [{"material_id": material, "qty": "5"}],
        },
    )


async def _close_an_order(smoke: Smoke, exclude: str) -> None:
    """手工关闭一张已审核（或已提交）的订单：预置的 po_000001 已审核、部分入库，可提前关闭。"""
    for status in ("approved", "submitted"):
        listed = (await smoke.call("purchase_order_search", {"status": status})).get("items") or []
        for item in listed:
            candidate = str(item["id"])
            if candidate == exclude:
                continue
            if not (await smoke.raw("purchase_order_close", {"id": candidate})).isError:
                smoke.mark("purchase_order_close", ok=True)
                return
    smoke.mark("purchase_order_close", ok=False, note="(isError) 没有可手工关闭的订单")


def _summary(smoke: Smoke, tools: Sequence[str]) -> int:
    """打印覆盖率并返回退出码：有工具未触达或失败则 1，否则 0。"""
    missing = [name for name in tools if name not in smoke.marks]
    failed = [name for name, mark in smoke.marks.items() if mark == "FAIL"]
    _emit(f"\n覆盖 {len(smoke.marks)}/{len(tools)} 个工具；未触达 {missing}；失败 {failed}")
    return 1 if missing or failed else 0


def _write_transcript(options: Options, smoke: Smoke) -> None:
    """把本次全部 tools/call 的工具名、入参与出参写成 JSON。"""
    document: dict[str, Any] = {
        "instance": str(options.instance),
        "anchor_date": options.anchor,
        "tools_called": sorted({str(entry["tool"]) for entry in smoke.transcript}),
        "calls": smoke.transcript,
    }
    options.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    options.out.write_text(text, encoding="utf-8")
    _emit(f"结果已写入 {options.out}（{len(smoke.transcript)} 次调用）")


async def drive(options: Options, data_dir: Path) -> int:
    """连一个 stdio 进程，跑完整条链并返回退出码。"""
    params = StdioServerParameters(
        command=str(COMMAND),
        args=[
            "stdio",
            "--instance",
            str(options.instance),
            "--packs-dir",
            str(options.packs_dir),
            "--anchor-date",
            options.anchor,
            "--data-dir",
            str(data_dir),
        ],
        env={**os.environ, RATE_LIMIT_VAR: HIGH_RATE},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, client_info=CLIENT_INFO) as session:
            init = await session.initialize()
            _emit(f"initialize: {init.serverInfo.name} {init.serverInfo.version}")
            tools = [tool.name for tool in (await session.list_tools()).tools]
            _emit(f"tools/list: {len(tools)} 个工具\n")
            smoke = Smoke(session=session)
            await run_flow(smoke)
            code = _summary(smoke, tools)
            _write_transcript(options, smoke)
            return code


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """解析命令行；用法错误以退出码 2 结束。"""
    parser = argparse.ArgumentParser(
        prog="scripts/smoke_tools.py",
        description="经 stdio 冒烟测试一个实例的全部工具（默认 instances/p2p-demo）。",
    )
    parser.add_argument("--instance", type=Path, default=DEFAULT_INSTANCE, help="实例配置目录。")
    parser.add_argument("--packs-dir", type=Path, default=DEFAULT_PACKS_DIR, help="业务包根目录。")
    parser.add_argument("--anchor-date", dest="anchor", default=DEFAULT_ANCHOR, help="锚点日期。")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="被测服务的 --data-dir，缺省为本脚本创建并在结束时删除的临时目录。",
    )
    parser.add_argument(
        "--out", type=Path, default=Path(DEFAULT_OUT), help="结果 JSON 的输出路径。"
    )
    args = parser.parse_args(argv)
    return Options(
        instance=Path(args.instance),
        packs_dir=Path(args.packs_dir),
        anchor=str(args.anchor),
        data_dir=None if args.data_dir is None else Path(args.data_dir),
        out=Path(args.out),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """跑一次冒烟测试并返回进程退出码。"""
    options = parse_args(argv)
    if not COMMAND.is_file():
        _emit(f"没有找到安装好的命令：{COMMAND}")
        return 1
    if options.data_dir is not None:
        data_dir, created = options.data_dir, False
    else:
        data_dir, created = Path(tempfile.mkdtemp(prefix="erp-mock-smoke-")), True

    async def _run() -> int:
        return await drive(options, data_dir)

    try:
        return anyio.run(_run)
    finally:
        if created:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
