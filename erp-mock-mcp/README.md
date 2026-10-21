# ERP-Mock-MCP 使用说明

本文是面向使用者的操作手册：怎么装、怎么跑、有哪些命令和接口、怎么接自己的业务、出问题怎么查。业务声明位于`packs/`与`instances/`。

适用版本：framework 0.1.0。

## 目录

1. [这是什么](#1-这是什么)
2. [安装](#2-安装)
3. [五分钟上手](#3-五分钟上手)
4. [命令行参考](#4-命令行参考)
5. [两种接入方式](#5-两种接入方式)
6. [工具面](#6-工具面)
7. [会话与沙箱](#7-会话与沙箱)
8. [Admin API](#8-admin-api)
9. [配置你自己的业务](#9-配置你自己的业务)
10. [数据目录布局](#10-数据目录布局)
11. [环境变量、资源限制与安全](#11-环境变量资源限制与安全)
12. [观测：trace 与 state diff](#12-观测trace-与-state-diff)
13. [开发与自检](#13-开发与自检)
14. [排错](#14-排错)
15. [延伸阅读](#15-延伸阅读)

## 1. 这是什么

ERP-Mock-MCP 是一个可配置业务的 ERP 模拟服务，对外以 MCP 工具的形式暴露。框架本身与具体 ERP 业务无关：实体、单据、状态机、下推与反写、库存联动、业务规则、工具暴露全部写在业务包的 YAML 里，服务加载一份实例配置后按配置生成工具。换业务不改代码，只改配置。

它给上层（benchmark、agent 评测、训练环境、集成测试）提供三样东西：

- 沙箱。每个会话一套独立的数据库，可重置、可存取命名快照，互不影响。
- trace。每次工具调用一条结构化事件，含入参摘要、结果、服务端处理时间。
- state diff。相对初始数据集的净变化，可逐字节比较，用来判定一段对话到底改了什么。

确定性是它的核心约束：实例配置、profile、初始数据集、锚点日期、请求序列这五项相同时，工具结果、diff 与 trace 都相同，跨进程、跨传输、跨 `PYTHONHASHSEED` 都相同。运行期不访问回环地址以外的网络，也不调用任何大模型。

## 2. 安装

前置条件：

- Python 3.12（`requires-python = ">=3.12,<3.13"`，只支持 3.12）
- [uv](https://docs.astral.sh/uv/)。仓库用 uv 管依赖，`uv.lock` 锁定了全部直接与间接依赖的版本。

```bash
cd erp-mock-mcp
uv sync --locked
```

`--locked` 表示拒绝改动锁文件：锁文件和 `pyproject.toml` 不一致时直接报错，而不是悄悄升级。`uv sync` 默认会装上 dev 依赖组（pytest、mypy、ruff 等）。

装好后命令行入口是 `erp-mock-mcp`：

```bash
uv run erp-mock-mcp --version        # erp-mock-mcp, version 0.1.0
uv run erp-mock-mcp --help           # 四个子命令
```

本文后面的命令都写成 `uv run erp-mock-mcp ...`。激活虚拟环境后可以直接写 `erp-mock-mcp ...`。

## 3. 五分钟上手

下面三步都在仓库根 `erp-mock-mcp/` 下执行，用仓库自带的示例实例配置 `instances/p2p-demo`（采购到付款，3 个业务包，22 个工具）。

评测实例`erp-benchmark`及其业务包随题集发布在Hugging Face，按[题集下载](../datasets/README.md)放到`../datasets/environments/erp-mock-mcp/`。将下例中的`instances/p2p-demo`换成该目录下的`instances/erp-benchmark`、`--packs-dir`换成同目录的`packs`即可使用评测实例；正式题目的数据集和白名单由上层Job选择。

只想赶快看看这些工具长什么样、传什么参、返回什么，跳到 [13.5 调试台](#135-调试台在浏览器里挑工具填参看出参)：一条命令起一个界面，选工具、手填入参、看真实出参。

### 3.1 先校验配置

```bash
uv run erp-mock-mcp validate --instance instances/p2p-demo
```

通过时 stderr 输出一行，退出码 0：

```
INFO erp_mock.cli.validate: 全部校验通过：业务包 3 个，初始数据集 1 个，锚点日期 2026-10-14
```

这一步执行的检查与服务启动时完全相同：8 个加载步骤加交叉检查，并真的构建一次初始数据集的快照。配置有问题时 stderr 列出该步骤的全部错误，一行一条，退出码 1。改配置以后先跑它，比起服务起不来再看日志快得多。

### 3.2 用 stdio 接一个 MCP 客户端

stdio 是最省事的接法：一个进程一个沙箱，不监听端口，不需要令牌。

```bash
uv run erp-mock-mcp stdio --instance instances/p2p-demo --anchor-date 2026-06-15
```

进程的 stdin 和 stdout 是 MCP 的消息通道（JSON-RPC），日志全部走 stderr。手工敲 JSON 不现实，正常做法是把它配进 MCP 客户端。以 Kiro 或 Claude Desktop 一类的客户端配置为例：

```json
{
  "mcpServers": {
    "erp-mock": {
      "command": "uv",
      "args": [
        "run", "--directory", "/绝对路径/erp-mock-mcp",
        "erp-mock-mcp", "stdio",
        "--instance", "/绝对路径/erp-mock-mcp/instances/p2p-demo",
        "--anchor-date", "2026-06-15"
      ]
    }
  }
}
```

路径用绝对路径最稳。命令行里的相对路径是按进程的工作目录解析的，而客户端启动子进程时的工作目录通常不是你以为的那个。

连上以后，客户端会看到 22 个工具。建议模型的第一个调用是 `business_context_get`：它返回业务日期、会计期间、缺省组织和当前用户，后面建单时不填这些字段，服务会按它填。

进程收到 EOF 退出时，会把这个沙箱的 state diff 写成 `<data-dir>/sandboxes/<沙箱 ID>/diff.json`，这是 stdio 下拿到"这段对话改了什么"的方式。

### 3.3 起 HTTP 服务，用 Admin API 观测

```bash
uv run erp-mock-mcp serve --instance instances/p2p-demo
```

两个端口，都在回环地址上：MCP 端点 `127.0.0.1:8000`，Admin API `127.0.0.1:8001`。启动时会生成一个 Admin 令牌并写到 `<data-dir>/admin-token`（权限 0600）。

```bash
TOKEN=$(cat var/admin-token)

# 健康与版本
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/health

# 预建一个沙箱，拿到它的 ID 与 MCP 地址
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{}' http://127.0.0.1:8001/admin/sandboxes

# 列出全部沙箱
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/sandboxes
```

MCP 客户端连 `http://127.0.0.1:8000/mcp`（Streamable HTTP）时，每个 initialize 会自动创建一个新沙箱；连 `http://127.0.0.1:8000/s/<沙箱 ID>/mcp` 则绑定到你预建的那个沙箱，这样你能在会话前后分别取它的 diff 和 trace：

```bash
SB=sb_0123456789abcdef
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/sandboxes/$SB/diff
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/sandboxes/$SB/trace
curl -s -X POST -H "Authorization: Bearer $TOKEN" -d '{}' \
     http://127.0.0.1:8001/admin/sandboxes/$SB/reset
```

`Ctrl-C` 或 SIGTERM 停止服务。

## 4. 命令行参考

四个子命令：`validate`、`seed`、`serve`、`stdio`。

### 4.1 公共选项

| 选项 | 哪些命令 | 缺省 | 说明 |
|---|---|---|---|
| `--instance` | 四个都有，必填 | 无 | 实例配置目录，其中的 `instance.yaml` 声明业务包、profile 与初始数据集 |
| `--packs-dir` | 四个都有 | `./packs` | 业务包所在目录；只有 `instance.yaml` 引用的业务包参与校验 |
| `--data-dir` | 四个都有 | `./var` | 数据目录，快照缓存与沙箱目录都在其中 |
| `--anchor-date` | `validate`、`seed`、`stdio` | 按实例配置的 `clock` | 锚点日期，写 `YYYY-MM-DD` |

路径选项的相对路径按命令的工作目录解析一次，之后与工作目录无关。符号链接不展开，报错信息里出现的就是你写的那个路径。

`--anchor-date` 分两步校验，都在加载任何配置之前完成：先查形式（`2026-2-3`、`2026/10/09` 这类不通过），再查日历（`2026-02-30`、`2100-02-29` 不通过）。两种都是用法错误，退出码 2。

`serve` 没有 `--anchor-date`：它的锚点日期由实例配置的 `clock` 在每次创建沙箱时决定，需要固定日期时用 Admin API 创建沙箱并在请求体里给 `anchor_date`。

### 4.2 validate

```bash
erp-mock-mcp validate --instance DIR [--packs-dir DIR] [--data-dir DIR] [--anchor-date YYYY-MM-DD]
```

执行与服务启动相同的全部检查，包括真的构建一次初始数据集的快照。只读不改配置，会在 `--data-dir` 下留下快照缓存。CI 和改完配置后的第一道检查就用它。

### 4.3 seed

```bash
erp-mock-mcp seed --instance DIR [--dataset NAME] [--packs-dir DIR] [--data-dir DIR] [--anchor-date YYYY-MM-DD]
```

忽略缓存，强制重建初始数据集的快照。`--dataset` 只建这一个，缺省建 `instance.yaml` 的 `datasets` 里全部，顺序就是那里的顺序。

每建好一个数据集，stdout 输出一行，四段用制表符分隔：数据集、锚点日期、快照文件路径、别名映射文件路径。这是唯一走 stdout 的输出，可以直接管给别的程序。

什么时候需要它：改了生成器或数据集定义以后想立刻重建；想为某个固定锚点日期预热缓存，让后面的服务启动不必现场构建。

### 4.4 serve

```bash
erp-mock-mcp serve --instance DIR [--host ADDR] [--port N] [--admin-port N]
                   [--packs-dir DIR] [--data-dir DIR]
```

| 选项 | 缺省 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | MCP 端点监听的地址。不是回环地址时必须设置环境变量 `ERP_MOCK_MCP_TOKEN`，否则拒绝启动 |
| `--port` | `8000` | MCP 端点的端口，1 到 65535 |
| `--admin-port` | `8001` | Admin 接口的端口。Admin 接口始终只在 `127.0.0.1` 上监听，不受 `--host` 影响 |

一个进程多个沙箱。Admin 令牌取自 `ERP_MOCK_ADMIN_TOKEN`，没设就自动生成并写到 `<data-dir>/admin-token`。SIGINT 与 SIGTERM 是正常停止，退出码 0；端口绑不上退出码 1。

### 4.5 stdio

```bash
erp-mock-mcp stdio --instance DIR [--profile NAME] [--dataset NAME]
                   [--anchor-date YYYY-MM-DD] [--packs-dir DIR] [--data-dir DIR]
```

| 选项 | 缺省 | 说明 |
|---|---|---|
| `--profile` | `instance.yaml` 的 `default_profile` | 会话使用的工具面 |
| `--dataset` | `instance.yaml` 的 `default_dataset` | 沙箱使用的初始数据集 |

一个进程一个沙箱。stdout 只走 MCP 消息，日志全部在 stderr。stdin 收到 EOF 时退出，退出前把 diff 写成沙箱目录下的 `diff.json`。

### 4.6 退出码

| 码 | 含义 |
|---|---|
| 0 | 命令完成了要它做的事（被信号正常停止的 serve 也是 0） |
| 1 | 校验步骤报了错，或快照构建失败，或端口绑不上。要改的是配置或机器，不是命令行 |
| 2 | 用法错误：缺 `--instance`、选项不存在、选项值不合法、环境变量的值不合法、profile 或数据集实例配置里没有 |

校验失败时，stderr 是该步骤的全部错误，一行一条，前后不加任何修饰，可以直接 diff 或粘贴。

### 4.7 日志与标准输出

日志统一走 stderr，一行一条，格式 `级别 模块: 消息`，不带时间戳（启动它的那个东西会打时间戳，而服务自己关心的时间都在 trace 里）。

stdout 只有两种情况有内容：`stdio` 的 MCP 消息，`seed` 的每数据集一行。源码里禁用了 `print`（ruff 规则 T201），所以日志不会误落到 stdout。

## 5. 两种接入方式

### 5.1 选哪个

| | stdio | Streamable HTTP（serve） |
|---|---|---|
| 沙箱 | 一个进程一个 | 一个进程多个 |
| 端口与令牌 | 都不需要 | 两个端口，Admin 要令牌 |
| 拿 diff | 进程退出后读 `diff.json` | 随时经 Admin API 读 |
| 锚点日期 | `--anchor-date` 固定 | 建沙箱时给 `anchor_date` |
| 适合 | 本地接客户端、单轮评测 | 并发评测、需要中途观测或重置 |

两边的业务行为完全一致，同一请求序列的工具结果、diff 与 trace 逐字节相同。

### 5.2 MCP 端点的地址

serve 的 MCP 端口按路径决定给你哪个沙箱：

| 地址 | 沙箱 | profile | 初始数据集 |
|---|---|---|---|
| `/mcp` | 新建一个 | `default_profile` | `default_dataset` |
| `/p/<profile>/mcp` | 新建一个 | 路径里的 | `default_dataset` |
| `/d/<dataset>/mcp` | 新建一个 | `default_profile` | 路径里的 |
| `/p/<profile>/d/<dataset>/mcp` | 新建一个 | 路径里的 | 路径里的 |
| `/s/<沙箱 ID>/mcp` | 路径指定的那个 | 该沙箱自己的 | 该沙箱自己的 |

其余路径、实例配置里没有的 profile 或数据集、不是本进程预建且未冻结的沙箱 ID，一律 404，既不建会话也不建沙箱。

协议细节：只接受 POST 与 DELETE，其他方法 405 并带 `Allow: POST, DELETE` 头（GET 也不行，本服务不提供服务端主动推送的 SSE 流）。POST 带一条 JSON-RPC 消息，DELETE 终止会话。除 initialize 以外的请求都必须带 `Mcp-Session-Id` 头，这个头的值由 initialize 的响应给出。Content-Type 与 Accept 都是 `application/json`。

### 5.3 一次会话的典型顺序

1. `initialize`，拿到 `Mcp-Session-Id`，服务创建或绑定沙箱，写 `session_start` 事件。
2. `tools/list`，拿到该 profile 的工具清单（同一 profile 的清单是固定的）。
3. `business_context_get`，拿业务日期、会计期间、缺省组织、当前用户。
4. 若干次 `tools/call`。
5. DELETE 终止会话，写 `session_end` 事件。沙箱本身不随会话消失，预建沙箱保持原状，自动创建的沙箱转为 frozen。

## 6. 工具面

### 6.1 工具是怎么来的

工具不写在代码里，由业务包的实体、单据、下推、查询定义加上 profile 的暴露配置生成。命名规则是 `<实体>_<动作>`，下推工具是 `<目标单据>_create_from_<来源简称>`。只有一个工具是引擎自带、每个实例都有的：`business_context_get`。

`tools/list` 的输出顺序跟着 `instance.yaml` 的 `packs` 顺序，是稳定的。每个工具都有 inputSchema 与 outputSchema，描述文字来自业务包的描述文件。

### 6.2 p2p-demo 的 22 个工具

| 分组 | 工具 |
|---|---|
| 引擎内置 | `business_context_get` |
| 基础资料 | `material_search`、`material_get`、`supplier_search`、`supplier_get`、`warehouse_search` |
| 采购申请 | `purchase_requisition_search`、`purchase_requisition_create` |
| 采购订单 | `purchase_order_search`、`purchase_order_get`、`purchase_order_create`、`purchase_order_update`、`purchase_order_submit`、`purchase_order_approve`、`purchase_order_close` |
| 采购入库 | `purchase_receipt_submit`、`purchase_receipt_approve` |
| 下推 | `purchase_receipt_create_from_order`、`purchase_order_create_from_requisition` |
| 查询 | `purchase_price_query`、`inventory_balance_query` |
| 库存调整 | `stock_adjustment_create` |

注意采购入库单没有 `create` 工具：它只能由采购订单下推产生（`purchase_receipt_create_from_order`），这是业务包刻意的设计，让下推链条成为唯一入口。

一条完整业务流：查供货商和物料，建采购订单，提交，审批，下推入库单，提交，审批。入库单审批时反写订单的已入库数量，订单各分录入满后订单自动转 closed，同时写库存流水并更新即时库存。`inventory_balance_query` 能看到增量。

### 6.3 结果与错误

成功时返回一个文本内容项加一个 `structuredContent`，`isError` 为 false。

失败时 `isError` 为 true，文本内容是一个 JSON 对象：

```json
{"error": {"code": "INVALID_STATE", "message": "……", "details": [ ... ]}}
```

七个错误码：

| 错误码 | 什么时候 |
|---|---|
| `VALIDATION_FAILED` | 入参不满足 schema 或字段级校验 |
| `NOT_FOUND` | 引用的记录不存在 |
| `INVALID_STATE` | 当前状态不允许这个动作，例如重复提交 |
| `BUSINESS_RULE_VIOLATION` | 业务包声明的规则不通过，例如交货日期早于业务日期 |
| `CONFLICT` | 并发或唯一性冲突 |
| `LIMIT_EXCEEDED` | 触到资源上限（见第 11 节），或参数超过声明的上界 |
| `INTERNAL_ERROR` | 服务内部错误 |

错误消息是简体中文，不包含令牌、请求头或异常文本。

### 6.4 一条命令测全部工具

想快速确认这些工具都能从命令行走通，用仓库自带的冒烟脚本：

```bash
uv run python scripts/smoke_tools.py
```

它经 stdio 连一个新沙箱，串起一条采购到入库的业务链（查基础资料、按预置的已审核申请单下推、新建并提交审核订单、下推入库并审核、手工关闭一张订单、建库存调整单，中间穿插全部检索与查询），逐个工具打印 PASS 或 FAIL，最后给出覆盖率。`tools/list` 里每个工具都被调用过且没有返回 isError 时退出码 0，否则 1。末尾一行形如：

```
覆盖 22/22 个工具；未触达 []；失败 []
```

每次 `tools/call` 的工具名、入参与出参会按发送顺序写进一个 JSON 文件，缺省 `smoke-tools-result.json`，用 `--out` 改路径：

```bash
uv run python scripts/smoke_tools.py --out var/smoke.json
```

文件形如下面这样，`args` 是入参、`output` 是出参（失败的调用没有 `output`，改为 `error` 对象）：

```json
{
  "instance": ".../instances/p2p-demo",
  "anchor_date": "2026-06-15",
  "tools_called": ["business_context_get", "..."],
  "calls": [
    {
      "seq": 1,
      "tool": "business_context_get",
      "args": {},
      "is_error": false,
      "output": {"business_date": "2026-06-15", "current_period": "2026-06", "org": {"...": "..."}, "user": {"...": "..."}}
    },
    {
      "seq": 12,
      "tool": "purchase_order_create",
      "args": {"supplier_id": "sup_000001", "delivery_date": "2026-06-15", "lines": [{"material_id": "mat_000001", "qty": "3", "price": "10.08"}]},
      "is_error": false,
      "output": {"id": "po_000006", "status": "draft", "amount_with_tax": "30.24", "...": "..."}
    }
  ]
}
```

几点说明：

- 它面向 p2p-demo。流程里的工具名、部门内部 ID 和检索关键字都是这个实例专有的，指向别的实例多半会失败。
- 它会给被测子进程设一个很高的 `ERP_MOCK_MAX_CALLS_PER_SECOND`，否则一秒内的突发调用会触到每秒 20 次的上限（见第 11 节）。沙箱数据写进临时目录，结束时删除。
- 有几个工具靠数据集预置的单据来覆盖：`purchase_order_create_from_requisition` 需要已审核的申请单，`purchase_order_close` 只能关已审核的订单，而 M0 没有暴露申请单的提交、审核工具。脚本因此用检索发现 `preq_000001`（已审核申请）和 `po_000001`（已审核订单）这类预置单据，而不是写死内部 ID。

只想看有哪些工具、各自要什么入参，不必跑整条链：

```bash
uv run python - <<'PYEOF'
import anyio
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.session import ClientSession

async def main():
    p = StdioServerParameters(command="erp-mock-mcp",
        args=["stdio", "--instance", "instances/p2p-demo",
              "--anchor-date", "2026-06-15", "--data-dir", "/tmp/erp-list"])
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for t in (await s.list_tools()).tools:
                print(f"{t.name:<40} required={(t.inputSchema or {}).get('required', [])}")

anyio.run(main)
PYEOF
```

要带断言的回归而不只是冒烟，用现成的测试套件：`uv run pytest tests/packs -q`（各业务包的声明式 call/expect 用例）和 `uv run pytest tests/integration/test_protocol_flow.py -q`（真实 stdio 与 HTTP 跑完整流程）。

## 7. 会话与沙箱

### 7.1 沙箱的三个状态

| 状态 | 含义 | 怎么进去 |
|---|---|---|
| active | 正常接受调用 | 创建时；idle 沙箱来了新调用 |
| idle | 数据库已关闭，省资源，随时能被唤醒 | 距上次调用完成超过 `ERP_MOCK_IDLE_SECONDS`（缺省 300 秒） |
| frozen | 只读，不能再写入、重置、恢复或建命名快照 | 转 idle 后再超过 `ERP_MOCK_FREEZE_SECONDS`（缺省 1800 秒）；或会话结束的自动创建沙箱；或 Admin 主动 freeze |

frozen 之后再超过 `ERP_MOCK_RETENTION_SECONDS`（缺省 604800 秒，7 天）沙箱目录被删除。

frozen 沙箱的 trace 和 diff 仍然可以读，这正是"会话结束后再去看它改了什么"的依据。对 frozen 沙箱做 reset、restore 或建命名快照，Admin 返回 409。

`ERP_MOCK_MAX_SANDBOXES`（缺省 256）数的是非 frozen 的沙箱。满了之后 Admin 建沙箱返回 503，MCP 端点的 initialize 也建不出新沙箱。

### 7.2 重置与命名快照

- reset：把数据库恢复到初始数据集的快照，调用计数归零。锚点日期、trace、已有的命名快照都不变。
- 命名快照：给当前状态起个名字存下来。名称要匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，`initial` 是保留名（它是 diff 的缺省基准）。同名再存返回 409。
- restore：恢复到某个命名快照，调用计数恢复为保存快照时的值。

典型用法：把"订单已审批、尚未入库"这个状态存成一个命名快照，然后让模型反复尝试后半段，每次 restore 回去，起点完全一致。

### 7.3 确定性的前提

这五项相同时，工具结果、state diff 与 trace 都相同：

1. 实例配置（含版本）
2. profile
3. 初始数据集（含版本）
4. 锚点日期
5. 请求序列

跨进程、跨传输（stdio 与 HTTP）、不同 `--data-dir`、不同 `PYTHONHASHSEED`、与其他沙箱并发执行，结果都不变。比较 trace 时要排除 `ts`、`latency_ms`、`sandbox_id`、`session_id` 这四个随运行变化的字段；比较 diff 时把 `sandbox_id` 的值替换掉即可逐字节比较。

还有一个前提容易忽略：序列里的调用不能被资源上限拒绝。一个会话每秒最多 20 次调用（缺省），一个沙箱最多 500 次调用（缺省），超了会得到 `LIMIT_EXCEEDED`，那就不是同一个序列了。做长序列评测时记得调高（见第 11 节）。

## 8. Admin API

只在 `127.0.0.1` 上监听，端口由 `--admin-port` 指定（缺省 8001）。

### 8.1 认证

每个请求都要带 `Authorization: Bearer <令牌>`。令牌来自：

- 环境变量 `ERP_MOCK_ADMIN_TOKEN`，或者
- serve 启动时自动生成，写到 `<data-dir>/admin-token`，权限 0600

令牌不对或没带，401。

### 8.2 13 个接口

| 方法与路径 | 成功 | 做什么 |
|---|---|---|
| `GET /admin/health` | 200 | 版本与健康：`framework_version`、`meta_model_version`、`instance`、`packs`、`profiles`、`datasets` |
| `GET /admin/datasets/{name}/aliases` | 200 | 该初始数据集的别名映射（别名到内部 ID），与锚点日期无关 |
| `POST /admin/sandboxes` | 201 | 预建一个沙箱。请求体可给 `profile`、`dataset`、`anchor_date`，都可省。返回沙箱 ID 与 `mcp_url` |
| `GET /admin/sandboxes` | 200 | `{"items": [...]}`。查询参数 `status`、`dataset`、`created_from`、`created_to` |
| `GET /admin/sandboxes/{id}` | 200 | 该沙箱的元数据，含 `status`、`anchor_date`、`session_ids` |
| `POST /admin/sandboxes/{id}/freeze` | 200 | 立刻冻结，返回冻结后的元数据 |
| `DELETE /admin/sandboxes/{id}` | 204 | 删除沙箱，无响应体 |
| `POST /admin/sandboxes/{id}/reset` | 200 | 恢复到初始数据集，返回元数据 |
| `POST /admin/sandboxes/{id}/snapshots` | 201 | 存命名快照。请求体 `{"name": "..."}` |
| `POST /admin/sandboxes/{id}/restore` | 200 | 恢复到命名快照。请求体 `{"name": "..."}` |
| `GET /admin/sandboxes/{id}/trace` | 200 | 该沙箱的 trace，原样返回（JSON Lines） |
| `GET /admin/sandboxes/{id}/diff` | 200 | state diff。查询参数 `base`，缺省 `initial`，也可给命名快照名 |
| `POST /admin/exports` | 200 | 一个 zip，含各沙箱的 trace 与 diff。请求体可给 `created_from`、`created_to` |

`session_ids` 是曾经绑定过该沙箱的全部会话，按首次绑定顺序，会话结束不会被摘掉。

读类接口（trace、diff）不加沙箱锁，也不更新"上次调用完成时间"，所以取 trace 或 diff 不会影响沙箱转 idle 的计时。

### 8.3 错误状态码

| 码 | 什么时候 |
|---|---|
| 400 | 请求体有未定义的键、类型不对、不是 JSON 对象；profile 或数据集不在实例配置里；`anchor_date` 不是日历日期；`status` 不是三个取值之一；时间没有 UTC 偏移；命名快照的名字缺失、形式不对或用了保留名 `initial`。没有定义参数的接口（freeze、reset、DELETE）带了非空请求体也是 400 |
| 401 | 令牌不对或没带 |
| 404 | 方法与路径的组合不是这 13 个之一；沙箱 ID 不是本进程的沙箱；路径里的数据集不在实例配置里；restore 或 diff 指定的命名快照不存在 |
| 409 | 对 frozen 沙箱 reset、restore 或建命名快照；命名快照重名 |
| 413 | 请求体超过 `ERP_MOCK_MAX_BODY_BYTES`（缺省 256 KB） |
| 500 | 新沙箱的快照构建失败；trace 或 diff 读不出来 |
| 503 | 非 frozen 的沙箱数已达上限 |

所有错误响应都是一个 JSON 对象 `{"error": "<短码>", "message": "<中文说明>"}`，不含令牌、请求头或异常文本。

注意一点：路径存在但方法不对，这里返回 404 而不是常见的 405。这是刻意的，13 个接口以外的任何方法与路径组合都一视同仁。

## 9. 配置你自己的业务

换业务不改代码。你要写的是两样东西：业务包和实例配置。

### 9.1 实例配置

一个实例配置目录长这样：

```
instances/p2p-demo/
  instance.yaml              服务哪些包、暴露哪个工具面、用哪个初始数据集
  profiles/semantic.yaml     工具面：这个 profile 暴露哪些工具
  datasets/purchase-basic.yaml  初始数据集定义
```

`instance.yaml` 的字段：

| 字段 | 说明 |
|---|---|
| `instance`、`version` | 实例名与版本，服务里到处以 `<名称>@<版本>` 出现 |
| `title` | 多语言标题，`{zh: ..., en: ...}` |
| `packs` | 业务包到版本的映射。固定到具体版本，不写范围。这个顺序同时决定报错排序、`tools/list` 的输出顺序和共享基础数据的创建顺序 |
| `profiles`、`default_profile` | 可用的工具面，以及不指定时用哪个 |
| `datasets`、`default_dataset` | 可用的初始数据集，以及不指定时用哪个 |
| `current_user` | 当前用户，写内部 ID，不按别名或编码解析 |
| `clock` | 锚点日期怎么来，见下 |

`clock` 两种模式：

| `mode` | 行为 | 必填 |
|---|---|---|
| `today`（缺省） | 创建沙箱时读一次系统时间，取 `timezone` 下的当天日期 | 无 |
| `fixed` | 固定用 `anchor_date` | `anchor_date` |

`timezone` 是 IANA 时区名（如 `Asia/Shanghai`），大小写敏感，不写就用服务进程的本地时区。单次运行显式给的日期（Admin API 的 `anchor_date`、`--anchor-date`）优先于这两种模式，所以 `mode: today` 也允许同时写 `anchor_date`。

p2p-demo 的 `instance.yaml`：

```yaml
instance: p2p-demo
version: 1.0.0
title: { zh: 采购到付款示例, en: Procure to pay example }
packs:
  core: 1.0.0
  purchase: 1.0.0
  inventory: 1.0.0
profiles: [semantic]
default_profile: semantic
datasets: [purchase-basic]
default_dataset: purchase-basic
current_user: emp_000001
clock:
  mode: today
  timezone: Asia/Shanghai
```

实例名、profile 名、数据集名都要匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，因为它们会出现在 URL 路径和缓存目录名里。

### 9.2 业务包的目录结构

```
packs/<包名>/
  pack.yaml                   manifest：包名、版本、依赖
  entities/*.yaml             基础资料实体与单据
  conversions/*.yaml          下推（源单据到目标单据的字段映射与数量规则）
  writebacks/*.yaml           反写（目标单据状态变化回写源单据）
  linkages/*.yaml             联动（如入库单审批写库存流水与即时库存）
  rules/*.yaml                业务规则（表达式）
  tools/exposure.yaml         哪些操作暴露成工具、有哪些查询
  tools/descriptions/*.yaml   工具描述文字
  seed/shared/*.yaml          共享基础数据（组织、部门、员工、单位、仓库等）
  seed/generators.yaml        字段值生成器
  seed/examples/*.yaml        示例数据，只作说明，不参与数据集构建
  tests/*.yaml                包级测试（声明式用例）
  README.md
```

仓库自带三个人工建模的包：`core`（基础资料）、`purchase`（采购申请、订单、入库，含下推与反写）、`inventory`（库存调整与库存联动）。后两个依赖 `core`，三个一起组成 `instances/p2p-demo`。

评测实例`erp-benchmark`（随题集下载，见[题集下载](../datasets/README.md)）加载评测用业务包，具体包与版本见其`instance.yaml`。各题绑定的数据集、工具白名单及运行方式见[ERP MCP接入](../docs/mcp-integration.md)。

### 9.3 初始数据集

数据集定义在实例配置的 `datasets/<名称>.yaml` 里，决定沙箱创建时库里有什么：写死的记录、按生成器批量生成的背景记录（`bulk`）、会计期间、当前用户。

两条要点：

- 日期一律写成相对表达式，不要写死日期。这样任意锚点日期（2000-01-01 到 2099-12-31）都能构建成功，数据永远落在"本周"而不是某个过去的日期。
- 别名。数据集可以给记录起别名，构建后生成别名映射文件，用 `GET /admin/datasets/{name}/aliases` 能读到。测试里用别名引用记录，比用内部 ID 稳。别名映射与锚点日期无关，换日期不变。

快照按 (数据集@版本, 锚点日期) 缓存。同一组合第二次用直接命中缓存，不重新构建。相同输入在不同进程、不同数据目录、不同时区下构建出的快照文件逐字节相同。

### 9.4 从哪开始

想做自己的业务包，照着 guides/pack-tutorial.md 走：它从实体、单据、下推反写、库存联动、规则、工具描述一路写到初始数据集、包级测试和运行观测，用的是 `mockMcp工具业务实体包示例/` 下的两个例子。规范性定义在 specs/meta-model.md 与 specs/business-pack.md。

每改一步就跑一次 `validate`，错误信息会指出具体是哪个文件的哪个配置路径。

## 10. 数据目录布局

`--data-dir`（缺省 `./var`）下：

```
var/
  admin-token                              serve 生成的 Admin 令牌，权限 0600
  snapshots/
    <数据集>@<版本>/<锚点日期>/
      snapshot.sqlite                      初始数据集的快照数据库
      meta.json                            快照元数据
      aliases.json                         别名到内部 ID 的映射
  sandboxes/
    <沙箱 ID>/                              沙箱 ID 形如 sb_ 加 16 位十六进制
      db.sqlite                            沙箱的工作数据库（打开时旁边还有 -wal 与 -shm）
      base.sqlite                          创建时的基准，reset 与 diff 都以它为准
      trace.jsonl                          该沙箱的 trace，一行一个事件
      sandbox.json                          沙箱元数据
      diff.json                            stdio 进程退出时写的 state diff
      named/<名称>.sqlite                   命名快照
      named/<名称>.json                     命名快照的侧文件（保存时的调用计数）
```

数据目录可以整个删掉，下次运行会重建。快照缓存删了只是下次要重新构建，沙箱目录删了就是丢掉那些沙箱的状态。

## 11. 环境变量、资源限制与安全

### 11.1 全部环境变量

serve 与 stdio 在启动时读一次，之后进程存续期间不再变。值不合法时退出码 2，并在 stderr 指出是哪个变量、什么值。

| 变量 | 缺省 | 作用 |
|---|---|---|
| `ERP_MOCK_MAX_BODY_BYTES` | 262144（256 KB） | 两个端口的请求体上限，超了 413 |
| `ERP_MOCK_MAX_SANDBOX_CALLS` | 500 | 单个沙箱的调用次数上限，超了 `LIMIT_EXCEEDED` |
| `ERP_MOCK_MAX_DB_BYTES` | 67108864（64 MB） | 单个沙箱数据库大小上限 |
| `ERP_MOCK_MAX_CALLS_PER_SECOND` | 20 | 单个会话每秒调用次数上限 |
| `ERP_MOCK_ALLOWED_ORIGINS` | 只允许回环来源 | 额外允许的 Origin，逗号分隔，每项形如 `<协议>://<主机>[:<端口>]`，协议只能是 http 或 https |
| `ERP_MOCK_IDLE_SECONDS` | 300 | active 转 idle 的阈值 |
| `ERP_MOCK_FREEZE_SECONDS` | 1800 | idle 转 frozen 的阈值 |
| `ERP_MOCK_RETENTION_SECONDS` | 604800（7 天） | frozen 到删除的阈值 |
| `ERP_MOCK_MAX_SANDBOXES` | 256 | 非 frozen 沙箱的数量上限，满了 503 |
| `ERP_MOCK_ADMIN_TOKEN` | serve 自动生成 | Admin 端口的令牌 |
| `ERP_MOCK_MCP_TOKEN` | 不要求令牌 | MCP 端点的令牌 |

数字必须是不带前导 0 的正整数。`ERP_MOCK_ALLOWED_ORIGINS` 的每一项不做任何修剪，因为它要跟 Origin 头逐字符比较，带空格的项永远匹配不上，所以直接拒绝而不是悄悄修好。

### 11.2 做长序列评测时要调的两个值

缺省的 500 次每沙箱、20 次每秒是给交互式使用的。跑长序列或压测时：

```bash
ERP_MOCK_MAX_SANDBOX_CALLS=5000 ERP_MOCK_MAX_CALLS_PER_SECOND=2000 \
  uv run erp-mock-mcp serve --instance instances/p2p-demo
```

不调的话，第 501 次调用会得到 `LIMIT_EXCEEDED`，而每秒 20 次的限制会把测量拖成等待。

### 11.3 安全

- Admin 端口永远只在 `127.0.0.1` 上，没有任何选项能把它放到外网。
- MCP 端点的 `--host` 不是回环地址时，必须设置 `ERP_MOCK_MCP_TOKEN`，否则服务拒绝启动。
- Origin 检查：回环来源始终允许，其余要在 `ERP_MOCK_ALLOWED_ORIGINS` 里列出，否则 403。
- 令牌不会出现在任何输出里：stdout、stderr、trace、diff、导出的 zip、数据目录下除令牌文件以外的文件、两个端口的 HTTP 响应，都不含令牌的值。
- 运行期不访问回环地址以外的网络，也不调用任何大模型。结果只由业务配置、沙箱数据库、锚点日期和请求参数决定。

## 12. 观测：trace 与 state diff

### 12.1 trace

每个沙箱一个 `trace.jsonl`，一行一个事件，UTF-8，追加写。六种事件：

| 事件 | 什么时候 |
|---|---|
| `session_start` | 会话建立 |
| `tools_list` | 客户端列工具 |
| `tools_call` | 一次工具调用 |
| `sandbox_reset` | 经 Admin 重置 |
| `snapshot_restore` | 经 Admin 恢复命名快照 |
| `session_end` | 会话终止 |

每个事件都以这些公共字段开头，顺序固定：

```
seq, ts, sandbox_id, session_id, instance, profile, dataset, anchor_date, event
```

`seq` 从 1 开始逐一递增，`ts` 是带毫秒的本地时间戳。`tools_call` 在公共字段之后还有：

```
tool, args, coerced, known_tool, is_error, error_code, rule_id, result, writes, flags, latency_ms
```

`latency_ms` 是服务端处理时间（整毫秒），不含客户端往返。`writes` 是这次调用写了哪些记录，每项 `entity, op, id, cause`。`coerced` 是被类型强制转换过的参数名，`rule_id` 是被哪条业务规则拒绝的。

事件在响应发出之前写完，所以客户端收到结果时事件已经落盘。事件里永远没有裸换行，文件的换行就是事件的边界。

怎么拿：serve 用 `GET /admin/sandboxes/{id}/trace`，stdio 直接读沙箱目录下的 `trace.jsonl`。

### 12.2 state diff

diff 是相对一个基准的净变化，缺省基准是 `initial`（创建沙箱时的初始数据集），也可以指定命名快照。

顶层有 `instance`、`dataset`、`anchor_date`、`sandbox_id` 和 `changes`。每条 change 描述一条记录的净变化，并带 `cause_seqs`，即哪些调用（按 trace 的 `seq`）造成了它。

三个地方拿到的 diff 是同一份字节：`GET /admin/sandboxes/{id}/diff`、stdio 退出时写的 `diff.json`、导出 zip 里的 `diff.json`。

用法上两点经验：

- 判定一段对话的结果，比对 diff 比去查数据库可靠得多，它已经把中间过程抵消掉了（建了又删的记录不会出现）。
- 要逐字节比较两次运行的 diff，先把 `sandbox_id` 的值替换成同一个占位串，其余部分可以直接比。

格式的字段级定义见 specs/integration.md 第 5、6 节。

## 13. 开发与自检

### 13.1 本地检查命令

```bash
uv run --locked python scripts/check.py
```

九步，顺序执行，遇错即停，全过时退出码 0。整条命令约 30 分钟，大头是两次跑测试。

| 步 | 命令 | 查什么 |
|---|---|---|
| 1 | `uv lock --check` | 锁文件与 `pyproject.toml` 一致 |
| 2 | `ruff format --check` | 格式 |
| 3 | `ruff check` | lint |
| 4 | `mypy` | 类型（`erp_mock.domain` 开严格模式） |
| 5 | `erp-mock-mcp validate --instance instances/p2p-demo` | 示例实例配置能通过全部加载校验 |
| 6 | `pytest tests/packs` | 各业务包自己 `tests/` 下的声明式用例 |
| 7 | `pytest tests/` | 全套测试 |
| 8 | `pytest tests/unit tests/property --cov=erp_mock.domain --cov-fail-under=90` | domain 层行覆盖率不低于 90% |
| 9 | `python scripts/req_trace.py` | 需求 1 到 24 的每条验收标准都有对应测试 |

### 13.2 测试目录

```
tests/
  probes/        对第三方库行为的探针，结论决定了实现方式
  unit/          单元测试，按模块分目录
  property/      属性测试（Hypothesis），32 个属性
  contract/      契约测试，tools/list 与线上格式的快照
  static/        对脚本与源码本身的检查
  packs/         包级声明式用例的执行器
  integration/   集成测试，起真实子进程
  helpers/       跨目录共享的测试设施
```

契约快照需要更新时：`uv run pytest tests/contract --update-snapshots`。这个选项只注册在 `tests/contract/conftest.py`，命令行里不带 `tests/contract` 就会报参数无法识别。

### 13.3 需求追踪

每个测试用 `@pytest.mark.req("12.3")` 这样的标记声明它覆盖哪条验收标准。追踪检查比对两份清单：

- `tests/req_index.json`：需求条目清单，已提交到仓库，所以全新检出不依赖 `.kiro` 目录
- 一次 `pytest --collect-only` 导出的全部 req 标记

```bash
# 追踪检查
uv run --locked python scripts/req_trace.py

# 需求文档改了以后重新生成清单
uv run --locked python scripts/req_trace.py index

# 只比较不写入
uv run --locked python scripts/req_trace.py index --check

# 单独导出标记看看
uv run pytest tests/ --collect-only -q --export-req-markers=var/req-markers.json
```

`.kiro` 在场时追踪检查会顺带校验清单与需求文档一致，不一致就失败；`.kiro` 不在场时只用仓库里的清单。

### 13.4 性能测量

```bash
uv run --locked python scripts/perf.py --instance instances/p2p-demo
```

输出三项指标，各一行在 stdout：服务启动耗时、100 次 initialize 的沙箱创建耗时 p95、1000 次工具调用的服务端处理时间 p95（取自 trace 的 `latency_ms`）。`--initializes` 与 `--calls` 可以调小。

它不是检查，没有阈值，也不属于 `scripts/check.py` 的任何一步。测量结果以当前机器和实例的实际输出为准。

它会自己把被测进程的资源上限调高，否则 1000 次调用根本跑不完。

### 13.5 调试台：在浏览器里挑工具、填参、看出参

配好一个实例以后想立刻摸一遍它的工具面，用调试台：

```bash
uv run python scripts/playground.py --instance ../datasets/environments/erp-mock-mcp/instances/erp-benchmark \
  --packs-dir ../datasets/environments/erp-mock-mcp/packs
```

它起一个真实的 `serve` 子进程（两个端口都在回环地址，端口号自动挑空闲的，Admin 令牌自动生成并只在进程环境里），预建一个沙箱，然后在 <http://127.0.0.1:8099> 上开一个单页界面并自动打开浏览器。界面上能做的事：

- 左栏按名字或描述搜工具，选中后中栏按它的 `inputSchema` 生成表单：枚举给下拉框，`format: date` 给日期控件，分录那样的对象数组给可增删的行编辑器，其余给 JSON 文本框。必填项标星号，参数描述直接显示在输入框下面，空输入不发送该参数。
- 切到 JSON 模式可以直接编辑整个 `arguments`，能发任何东西——类型写错的、多余的字段、不存在的工具——看到的是服务端真实的 `VALIDATION_FAILED` 与错误明细，不是客户端拦下来的。「按 schema 生成模板」把必填项按类型铺好骨架。
- 右栏是真实出参：`structuredContent`、失败时的 `error` 对象、这次调用的 `writes`、被强转的参数路径、服务端处理时间，以及完整的 JSON-RPC 响应与对应的那条 `tools_call` 事件。
- 另外四个页签：整个沙箱的 Trace（可逐条展开）、相对 `initial` 或某个命名快照的 state diff、本次沙箱的调用历史（点一行把入参回填回编辑器）、`serve` 子进程的日志。
- 顶栏能换实例、重启服务、新建沙箱（可指定 profile、初始数据集、锚点日期）、重置、存命名快照、恢复快照。改完 YAML 点一下「重新加载」就是一次真实的加载校验，不通过时逐行的报错原样显示在界面上。

几点要知道的：

- 它是开发工具，和本节其他脚本一样不进 wheel、不进交付镜像、不是 `scripts/check.py` 的任何一步。
- 整个调试过程复用一个沙箱与一个 MCP 会话，所以 trace 是一串连续的 `tools_call`，和一个 agent 跑一段对话看到的形状一样。
- 它把资源上限调高了（每沙箱调用数、每秒调用数、idle 与 freeze 阈值），否则点快一点就会撞上缺省的每秒 20 次，读会儿文档回来沙箱已经冻结。想看上限本身的行为就直接用 `serve`。
- 三个端口全部绑在回环地址上。界面能任意调用写工具、能读 trace 与 diff，所以别把 `--host` 改成对外地址。
- 常用选项：`--port` 换界面端口，`--instances-root` 换实例下拉框扫描的根目录，`--data-dir` 换子进程的数据目录（缺省 `var`，沿用它能复用已有的快照缓存），`--no-browser` 不自动开浏览器。

## 14. 排错

| 现象 | 原因与处理 |
|---|---|
| 退出码 2，stderr 说某环境变量的值不合法 | 数字要是不带前导 0 的正整数，Origin 要是 `http(s)://主机[:端口]` 且不带空格、路径、查询串 |
| 退出码 1，stderr 一堆配置错误 | 加载校验失败。每行指出文件与配置路径，从第一行开始改，改完再跑 `validate` |
| 服务起不来，日志提到端口 | 端口被占。换 `--port` 或 `--admin-port` |
| 启动被拒，说 `--host` 不是回环地址 | 对外监听必须设 `ERP_MOCK_MCP_TOKEN` |
| Admin 返回 401 | 令牌不对。确认用的是 `cat <data-dir>/admin-token` 的内容，或你设的 `ERP_MOCK_ADMIN_TOKEN` |
| Admin 返回 404，但路径看着对 | 可能是方法不对（本服务对方法与路径的错误组合统一回 404，不回 405）；也可能沙箱 ID 不是本进程的 |
| Admin 返回 409 | 沙箱已 frozen，不能再 reset、restore 或建命名快照；或命名快照重名 |
| Admin 返回 413 | 请求体超过 `ERP_MOCK_MAX_BODY_BYTES` |
| Admin 返回 503 | 非 frozen 沙箱数已达 `ERP_MOCK_MAX_SANDBOXES`。删掉用完的沙箱，或调高上限 |
| MCP 端点返回 404 | profile 或数据集名字不在实例配置里；或 `/s/<ID>/mcp` 的沙箱不存在、已冻结、不是本进程预建的 |
| MCP 端点返回 405 | 只接受 POST 与 DELETE |
| 工具返回 `LIMIT_EXCEEDED` | 触到调用次数、速率或数据库大小上限。调 `ERP_MOCK_MAX_SANDBOX_CALLS`、`ERP_MOCK_MAX_CALLS_PER_SECOND`、`ERP_MOCK_MAX_DB_BYTES` |
| 工具返回 `INVALID_STATE` | 当前状态不允许这个动作，比如对已提交的单据再提交一次。先用对应的 get 工具看状态 |
| 工具返回 `BUSINESS_RULE_VIOLATION` | 业务包的规则不通过。trace 事件里的 `rule_id` 指出是哪条 |
| MCP 客户端连上了但看不到工具 | 看客户端对该服务进程的 stderr。多半是 `--instance` 的相对路径在客户端的工作目录下解析不到，改绝对路径 |
| 两次运行的 diff 不一致 | 核对五个前提（实例配置、profile、数据集、锚点日期、请求序列）。最常见的是锚点日期：`clock.mode` 为 today 时跨过了午夜。要可复现就显式给日期 |
| 快照构建失败 | 多半是数据集里写了绝对日期，在某些锚点日期下越界。用 `seed --anchor-date <那个日期>` 单独重建，看报的是哪条记录 |
| 某个秒级阈值的测试偶发超时 | 机器负载高时会发生（那些测试用的是真实的秒）。单独重跑该文件确认 |
| 调试台说端口绑不上 | 界面端口被占。换 `--port` |
| 调试台界面上一片报错，说 serve 以退出码 1 结束 | 实例配置没过加载校验。报错正文就是子进程的 stderr，逐行改完点「重新加载」 |
| 调试台里调用工具说「当前没有沙箱」 | 服务没起来（看界面上的错误或「日志」页签），或者沙箱被删了。修好实例后点「重新加载」，或点「新建沙箱」 |

## 15. 延伸阅读

| 文档 | 什么时候看 |
|---|---|
| 01-solution.md | 想了解背景、目标与非目标、选型理由 |
| 02-flowcharts.md | 想看架构、加载流程、会话与沙箱生命周期、调用流水线的图 |
| guides/pack-tutorial.md | 要动手做业务包 |
| specs/meta-model.md | 要查实体、单据、状态机、下推反写、规则的准确语义 |
| specs/business-pack.md | 要查业务包与实例配置的字段定义和加载校验规则 |
| specs/seed-data.md | 要查初始数据集、生成器、相对日期、快照的定义 |
| specs/tool-design.md | 要查工具命名、入参返回、错误、profile 的规则 |
| specs/integration.md | 要查 Admin API、trace、state diff 的字段级格式 |
| specs/engineering.md | 要查技术栈、分层约束、测试要求、性能目标 |
| packs/example-p2p.md | 想了解 p2p-demo 这个示例业务包本身 |
