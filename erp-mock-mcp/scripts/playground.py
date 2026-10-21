"""调试台：在浏览器里挑工具、手填入参、看真实出参（开发工具）。

在仓库根 ``erp-mock-mcp/`` 下运行::

    uv run python scripts/playground.py --instance instances/p2p-demo

它做三件事：

1. 用你给的 ``--instance`` 起一个真实的 ``erp-mock-mcp serve`` 子进程（两个端口都在回环地址，
   端口号由本脚本挑空闲的，Admin 令牌由本脚本生成并经环境变量注入）；
2. 经 Admin API 预建一个沙箱，再在 ``/s/<沙箱 ID>/mcp`` 上开一个 MCP 会话，整个调试过程复用
   这一个沙箱与会话，所以 trace 里看到的就是一串连续的 ``tools_call``；
3. 在 ``--port``（缺省 8099）上提供一个单页界面与几个 JSON 接口，页面里选工具、填参、调用，
   出参、trace 事件与 state diff 原样显示。

为什么是子进程而不是进程内直调 :class:`~erp_mock.tools.layer.ToolLayer`：子进程走的是真实的
MCP streamable-http 管线，界面上看到的出参与 agent 看到的逐字节相同，而且实例配置改完点一下
「重新加载」就是一次真实的 serve 重启，加载校验失败时的 stderr 能原样贴到界面上。

浏览器为什么不能直接连 8000/8001：那是跨源请求，两个端口都不发 CORS 头、也不接 OPTIONS，所以
本脚本既是静态页服务器也是这两个端口的代理，页面只与自己的源通信。

与交付的关系：本文件是开发工具，和 ``scripts/smoke_tools.py``、``scripts/perf.py`` 一样不进
wheel、不进 ``docker/erp-mock-mcp/Dockerfile`` 的镜像，也不是 ``scripts/check.py`` 的任何一步。

资源上限（需求 23.12）被本脚本调高：调试时一个沙箱会被反复调用几百次、点得又快，缺省的每沙箱
500 次与每秒 20 次会让界面莫名返回 ``LIMIT_EXCEEDED``；idle、freeze 的阈值也调高，否则你去读一
会儿文档回来沙箱已经冻结、会话已被回收（需求 15.5、15.7）。想看上限本身的行为就直接用 serve。

安全：三个端口全部绑在回环地址上，Admin 令牌只存在于本进程与子进程的环境里，页面拿不到它。界面
能任意调用写工具、能读 trace 与 diff，所以不要把 ``--host`` 改成对外地址。
"""

import argparse
import asyncio
import atexit
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from erp_mock.cli.options import (
    ADMIN_TOKEN_VAR,
    FREEZE_SECONDS_VAR,
    IDLE_SECONDS_VAR,
    MAX_CALLS_PER_SECOND_VAR,
    MAX_SANDBOX_CALLS_VAR,
    RETENTION_SECONDS_VAR,
)
from erp_mock.gateway.router import sandbox_path
from erp_mock.gateway.serve import ADMIN_HOST, bind_socket

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# 被调试的东西：缺省是仓库自带的示例实例与业务包根目录。评测实例 erp-benchmark 随题集下载，
# 用 --instance 与 --packs-dir 指向 ../datasets/environments/erp-mock-mcp/ 下的对应目录。
DEFAULT_INSTANCE: Final = REPO_ROOT / "instances" / "p2p-demo"
DEFAULT_INSTANCES_ROOT: Final = REPO_ROOT / "instances"
DEFAULT_PACKS_DIR: Final = REPO_ROOT / "packs"
DEFAULT_DATA_DIR: Final = REPO_ROOT / "var"

# 被启动的命令：和本解释器同目录的 console script，也就是部署时敲的那个命令。
COMMAND: Final = Path(sys.executable).parent / "erp-mock-mcp"

# 单页界面的文件，每次请求重新读一遍，这样改完 HTML 刷新浏览器就生效。
HTML_FILE: Final = Path(__file__).with_name("playground_ui.html")

# 调试台自己监听的地址。回环，和它代理的两个端口一样。
DEFAULT_PORT: Final = 8099
LOOPBACK: Final = ADMIN_HOST

# 实例配置目录的标志文件（business-pack.md）：有它才算一个实例配置目录。
INSTANCE_FILE: Final = "instance.yaml"

# Admin 接口（需求 19.1）。
HEALTH_PATH: Final = "/admin/health"
SANDBOXES_PATH: Final = "/admin/sandboxes"

# MCP 端点要求的两个头（README 第 5.2 节）：serve 的 transport 以 JSON 响应模式运行，所以
# Accept 只需要 application/json，不需要 text/event-stream。
MCP_HEADERS: Final = {"Content-Type": "application/json", "Accept": "application/json"}
SESSION_HEADER: Final = "mcp-session-id"

# 握手里报的协议版本与客户端名字。版本取 SDK 支持列表里的一个，服务端不认时会回它自己的最新版。
PROTOCOL_VERSION: Final = "2025-06-18"
CLIENT_NAME: Final = "erp-mock-playground"
CLIENT_VERSION: Final = "0.1.0"

# 等待与轮询（和 scripts/perf.py 同一套做法）。
POLL_SECONDS: Final = 0.05
START_TIMEOUT_S: Final = 180.0
STOP_TIMEOUT_S: Final = 30.0
HTTP_TIMEOUT_S: Final = 300.0

# 子进程被调高的上限（模块文档）。都是不带前导 0 的正整数，否则 serve 以退出码 2 拒绝启动。
RAISED_LIMITS: Final = {
    MAX_SANDBOX_CALLS_VAR: "1000000",
    MAX_CALLS_PER_SECOND_VAR: "100000",
    IDLE_SECONDS_VAR: "86400",
    FREEZE_SECONDS_VAR: "86400",
    RETENTION_SECONDS_VAR: "604800",
}

# 传给子进程的宿主环境变量：够它跑起来，且不含任何 ERP_MOCK_*，否则本进程环境里的上限会盖掉
# RAISED_LIMITS（需求 23.9）。
PASSED_THROUGH: Final = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TZ")

# trace 事件里本脚本读的那一种（需求 17.3）。
TOOLS_CALL_EVENT: Final = "tools_call"


def _say(message: str) -> None:
    """本脚本自己的一行输出，走 stderr（ruff T201 禁用 print）。"""
    sys.stderr.write(f"==> {message}\n")
    sys.stderr.flush()


class PlaygroundError(Exception):
    """一次操作没做成，``message`` 与 ``detail`` 直接显示在界面上。

    ``detail`` 是给人看的长文本：serve 的 stderr、HTTP 响应体之类。接口层把它序列化成
    ``{"error": message, "detail": detail}``，页面原样展示，所以这里不做任何裁剪。
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


# --- 命令行 -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Options:
    """一次运行被要求调试什么、在哪儿提供界面。

    四个路径都是绝对路径：:func:`parse_args` 按本进程的工作目录解析一次，之后子进程拿到的就
    与它自己的工作目录无关（需求 22.1 对 CLI 路径选项的同一读法）。
    """

    instance: Path
    instances_root: Path
    packs_dir: Path
    data_dir: Path
    host: str
    port: int
    open_browser: bool


def parse_args(argv: Sequence[str] | None = None) -> Options:
    """一次运行的选项；用法错误以退出码 2 结束进程。"""
    parser = argparse.ArgumentParser(
        prog="scripts/playground.py",
        description=(
            "ERP-Mock-MCP 调试台：起一个真实的 serve 子进程，在浏览器里选工具、"
            "手填入参、看真实出参与 trace / diff。开发工具，不属于交付内容。"
        ),
    )
    parser.add_argument(
        "--instance",
        type=Path,
        default=DEFAULT_INSTANCE,
        help=f"要调试的实例配置目录，缺省 {DEFAULT_INSTANCE.relative_to(REPO_ROOT)}。",
    )
    parser.add_argument(
        "--instances-root",
        type=Path,
        default=DEFAULT_INSTANCES_ROOT,
        help="界面上实例下拉框扫描的根目录，缺省 instances。",
    )
    parser.add_argument(
        "--packs-dir",
        type=Path,
        default=DEFAULT_PACKS_DIR,
        help="业务包根目录，缺省 packs。",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="serve 子进程的 --data-dir，缺省 var；沿用它能复用已有的快照缓存。",
    )
    parser.add_argument(
        "--host",
        default=LOOPBACK,
        help=f"调试台监听的地址，缺省 {LOOPBACK}；界面能任意调用写工具，不要对外监听。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"调试台监听的端口，缺省 {DEFAULT_PORT}。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不要自动打开浏览器。",
    )
    args = parser.parse_args(argv)
    return Options(
        instance=Path(args.instance).resolve(),
        instances_root=Path(args.instances_root).resolve(),
        packs_dir=Path(args.packs_dir).resolve(),
        data_dir=Path(args.data_dir).resolve(),
        host=str(args.host),
        port=int(args.port),
        open_browser=not bool(args.no_browser),
    )


# --- serve 子进程 -------------------------------------------------------------------------


@dataclass(slots=True)
class Running:
    """正在跑的那个 serve 子进程，以及怎么找到它的两个端口。"""

    process: subprocess.Popen[bytes]
    instance: Path
    mcp_port: int
    admin_port: int
    log: Path

    @property
    def stderr(self) -> str:
        """子进程写到日志文件里的东西，也就是它的日志（日志统一走 stderr）。"""
        return _read_text(self.log)


def _read_text(path: Path) -> str:
    """``path`` 的文本；读不出来就是空串，这里的用途只是显示给人看。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def free_port() -> int:
    """一个刚刚没人占的回环端口，用 serve 绑 socket 的同一种方式取得后立刻释放。"""
    with closing(bind_socket(LOOPBACK, 0)) as sock:
        return int(sock.getsockname()[1])


def subprocess_env(token: str) -> dict[str, str]:
    """serve 子进程的环境：调高的上限、Admin 令牌，以及宿主环境里少数几个变量。"""
    inherited = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    return {**inherited, **RAISED_LIMITS, ADMIN_TOKEN_VAR: token}


def instance_candidates(root: Path, current: Path) -> list[dict[str, str]]:
    """``root`` 下的实例配置目录，加上当前这个（它可能不在 root 下）。

    判定标准是目录里有 ``instance.yaml``；排序按路径字符串，所以下拉框的顺序是稳定的。当前这个
    无条件在列表里，哪怕它根本不是实例配置目录：下拉框要显示你刚才试着加载的那个，而不是跳回
    别的一项。

    每项是 ``{"path": 绝对路径, "label": 短名}``：下拉框显示短名，因为绝对路径在选择框里会被
    截断成看不出是哪个实例，而选中的值仍是绝对路径。
    """
    found: set[Path] = {current}
    if root.is_dir():
        found.update(
            child.resolve() for child in root.iterdir() if (child / INSTANCE_FILE).is_file()
        )
    return [{"path": str(path), "label": _label(path)} for path in sorted(found)]


def _label(path: Path) -> str:
    """实例目录在下拉框里的短名：在仓库下就用相对路径，否则用目录名加父目录名。"""
    with contextlib.suppress(ValueError):
        return str(path.relative_to(REPO_ROOT))
    return str(Path(path.parent.name) / path.name)


# --- 后端：子进程、沙箱、MCP 会话 ----------------------------------------------------------


@dataclass(slots=True)
class CallRecord:
    """一次工具调用在界面历史里的样子。"""

    seq: int
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    duration_ms: float


class Supervisor:
    """调试台后端：一个 serve 子进程、一个沙箱、一个 MCP 会话，以及对它们的操作。

    并发由一把锁挡住：界面是单人用的，但「重新加载」与「调用工具」撞上会让会话指向一个已经没
    了的进程，所以改变后端状态的操作（启动、停止、换实例、建沙箱、开会话）都在
    :attr:`_lock` 里串行。工具调用本身不占这把锁——同一沙箱的请求由 Sandbox_Manager 自己排队
    （需求 15.4）——只在需要重开会话时才进去。
    """

    def __init__(self, options: Options) -> None:
        self._options = options
        self._token = secrets.token_hex(16)
        self._lock = asyncio.Lock()
        self._running: Running | None = None
        self._admin: httpx.AsyncClient | None = None
        self._mcp: httpx.AsyncClient | None = None
        self._instance = options.instance
        self._health: dict[str, Any] = {}
        self._sandbox: dict[str, Any] | None = None
        self._session_id: str | None = None
        self._snapshots: list[str] = []
        self._history: list[CallRecord] = []
        self._next_rpc_id = 0
        self._failure: dict[str, str] | None = None
        # 兜底：正常退出路径是 lifespan 的 finally，但本进程若以别的方式结束（未捕获异常、
        # uvicorn 绑不上端口时的 sys.exit），这一步保证不留一个还在监听的 serve 子进程。
        atexit.register(self._kill_leftover)

    def _kill_leftover(self) -> None:
        """进程退出时还活着的子进程，SIGTERM 掉；已经停过的话什么都不做。"""
        running = self._running
        if running is None or running.process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            running.process.send_signal(signal.SIGTERM)
        _wait_quietly(running.process)

    # --- 状态 --------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """界面开局与每次操作后读的那一份状态。"""
        running = self._running
        return {
            "running": running is not None,
            "instance": str(self._instance),
            "instance_name": self._instance.name,
            "instances": instance_candidates(self._options.instances_root, self._instance),
            "packs_dir": str(self._options.packs_dir),
            "data_dir": str(self._options.data_dir),
            "mcp_port": None if running is None else running.mcp_port,
            "admin_port": None if running is None else running.admin_port,
            "health": self._health,
            "sandbox": self._sandbox,
            "session_id": self._session_id,
            "snapshots": list(self._snapshots),
            "history": [
                {
                    "seq": item.seq,
                    "tool": item.tool,
                    "arguments": item.arguments,
                    "is_error": item.is_error,
                    "duration_ms": item.duration_ms,
                }
                for item in self._history
            ],
            "failure": self._failure,
            "log": "" if running is None else running.stderr,
        }

    # --- 启停 --------------------------------------------------------------------------

    async def start(self, instance: Path | None = None) -> None:
        """（重）启动 serve 子进程，预建沙箱并开一个 MCP 会话。

        ``instance`` 为 None 时重启当前实例，这就是界面上的「重新加载」：改完 YAML 点一下，
        走的是一次真实的加载校验。失败时抛 :class:`PlaygroundError`，``detail`` 是子进程的
        stderr，也就是加载校验逐行报的那些错。
        """
        async with self._lock:
            await self._start_locked(self._instance if instance is None else instance)

    async def stop(self) -> None:
        """停掉子进程并丢掉沙箱与会话；子进程留下的沙箱目录不动。"""
        async with self._lock:
            await self._stop_locked()

    async def _start_locked(self, instance: Path) -> None:
        await self._stop_locked()
        self._instance = instance
        self._failure = None
        if not (instance / INSTANCE_FILE).is_file():
            failure = PlaygroundError(f"{instance} 下没有 {INSTANCE_FILE}，不是实例配置目录")
            self._remember(failure)
            raise failure
        mcp_port, admin_port = free_port(), free_port()
        log = self._options.data_dir / "playground-serve.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        args = [
            str(COMMAND),
            "serve",
            "--instance",
            str(instance),
            "--packs-dir",
            str(self._options.packs_dir),
            "--data-dir",
            str(self._options.data_dir),
            "--host",
            LOOPBACK,
            "--port",
            str(mcp_port),
            "--admin-port",
            str(admin_port),
        ]
        _say(f"启动服务：{' '.join(args)}")
        with log.open("wb") as sink:
            process = subprocess.Popen(
                args,
                cwd=REPO_ROOT,
                env=subprocess_env(self._token),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
        running = Running(
            process=process, instance=instance, mcp_port=mcp_port, admin_port=admin_port, log=log
        )
        self._running = running
        self._admin = httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{admin_port}",
            timeout=HTTP_TIMEOUT_S,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        self._mcp = httpx.AsyncClient(
            base_url=f"http://{LOOPBACK}:{mcp_port}", timeout=HTTP_TIMEOUT_S
        )
        try:
            self._health = await self._wait_ready(running)
            await self._create_sandbox_locked({})
        except PlaygroundError as failure:
            self._remember(failure)
            await self._stop_locked(keep_failure=True)
            raise
        _say(f"实例 {instance.name} 就绪：MCP {mcp_port}，Admin {admin_port}")

    def _remember(self, failure: PlaygroundError) -> None:
        """记住最后一次失败，让界面在刷新后仍然看得到它。"""
        self._failure = {"error": failure.message, "detail": failure.detail}

    async def _wait_ready(self, running: Running) -> dict[str, Any]:
        """轮询到 Admin 端口答 200 为止，返回 ``GET /admin/health`` 的内容（需求 19.9）。"""
        client = self._require_admin()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            code = running.process.poll()
            if code is not None:
                raise PlaygroundError(
                    f"serve 进程以退出码 {code} 结束，没有开始监听"
                    f"（退出码 1 是加载校验失败，2 是用法或环境变量错误）",
                    running.stderr,
                )
            try:
                response = await client.get(HEALTH_PATH)
            except httpx.TransportError:
                await anyio.sleep(POLL_SECONDS)
                continue
            if response.status_code == httpx.codes.OK:
                return dict(response.json())
            await anyio.sleep(POLL_SECONDS)
        raise PlaygroundError(f"等待 serve 开始监听超时（{START_TIMEOUT_S} 秒）", running.stderr)

    async def _stop_locked(self, keep_failure: bool = False) -> None:
        running, self._running = self._running, None
        self._sandbox = None
        self._session_id = None
        self._snapshots = []
        self._history = []
        self._health = {}
        if not keep_failure:
            self._failure = None
        for client in (self._admin, self._mcp):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()
        self._admin = None
        self._mcp = None
        if running is None:
            return
        if running.process.poll() is None:
            with contextlib.suppress(OSError):
                running.process.send_signal(signal.SIGTERM)
        await anyio.to_thread.run_sync(_wait_quietly, running.process)

    # --- Admin API ---------------------------------------------------------------------

    def _require_admin(self) -> httpx.AsyncClient:
        if self._admin is None:
            raise PlaygroundError("服务没有在运行，先重新加载实例")
        return self._admin

    def _require_mcp(self) -> httpx.AsyncClient:
        if self._mcp is None:
            raise PlaygroundError("服务没有在运行，先重新加载实例")
        return self._mcp

    def _require_sandbox(self) -> dict[str, Any]:
        if self._sandbox is None:
            raise PlaygroundError("当前没有沙箱，先新建一个")
        return self._sandbox

    async def admin_request(
        self, method: str, path: str, *, json_body: Any = None, params: Any = None
    ) -> httpx.Response:
        """一次 Admin 请求；状态码不是 2xx 时抛 :class:`PlaygroundError`，带响应体。"""
        client = self._require_admin()
        try:
            response = await client.request(method, path, json=json_body, params=params)
        except httpx.TransportError as error:
            raise PlaygroundError(f"Admin 接口 {path} 连不上：{error}") from error
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise PlaygroundError(
                f"Admin 接口 {method} {path} 返回 {response.status_code}", response.text
            )
        return response

    # --- 沙箱 --------------------------------------------------------------------------

    async def create_sandbox(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """新建一个沙箱并切到它上面（Admin ``POST /admin/sandboxes``，需求 19.3）。"""
        async with self._lock:
            return await self._create_sandbox_locked(body)

    async def _create_sandbox_locked(self, body: Mapping[str, Any]) -> dict[str, Any]:
        wanted = {key: value for key, value in body.items() if value not in (None, "")}
        response = await self.admin_request("POST", SANDBOXES_PATH, json_body=wanted)
        created = response.json()
        self._session_id = None
        self._snapshots = []
        self._history = []
        self._sandbox = await self._metadata(str(created["sandbox_id"]))
        return self._sandbox

    async def _metadata(self, sandbox_id: str) -> dict[str, Any]:
        response = await self.admin_request("GET", f"{SANDBOXES_PATH}/{sandbox_id}")
        return dict(response.json())

    async def refresh_sandbox(self) -> dict[str, Any]:
        """重新读一遍当前沙箱的元数据（需求 19.12）。"""
        sandbox = self._require_sandbox()
        self._sandbox = await self._metadata(str(sandbox["sandbox_id"]))
        return self._sandbox

    async def reset_sandbox(self) -> dict[str, Any]:
        """恢复到初始数据集，调用计数归零；trace 与命名快照不变（需求 19.4）。"""
        sandbox = self._require_sandbox()
        await self.admin_request(
            "POST", f"{SANDBOXES_PATH}/{sandbox['sandbox_id']}/reset", json_body={}
        )
        self._history = []
        return await self.refresh_sandbox()

    async def save_snapshot(self, name: str) -> list[str]:
        """把当前状态存成命名快照（需求 19.5）。"""
        sandbox = self._require_sandbox()
        await self.admin_request(
            "POST", f"{SANDBOXES_PATH}/{sandbox['sandbox_id']}/snapshots", json_body={"name": name}
        )
        if name not in self._snapshots:
            self._snapshots.append(name)
        return list(self._snapshots)

    async def restore_snapshot(self, name: str) -> dict[str, Any]:
        """恢复到某个命名快照（需求 19.6）。"""
        sandbox = self._require_sandbox()
        await self.admin_request(
            "POST", f"{SANDBOXES_PATH}/{sandbox['sandbox_id']}/restore", json_body={"name": name}
        )
        return await self.refresh_sandbox()

    async def trace(self) -> list[dict[str, Any]]:
        """当前沙箱的 trace，一行一个事件（需求 19.7）。"""
        sandbox = self._require_sandbox()
        response = await self.admin_request(
            "GET", f"{SANDBOXES_PATH}/{sandbox['sandbox_id']}/trace"
        )
        return _parse_jsonl(response.text)

    async def diff(self, base: str | None) -> dict[str, Any]:
        """当前沙箱相对基准的净变化（需求 19.8）。"""
        sandbox = self._require_sandbox()
        params = None if base in (None, "") else {"base": base}
        response = await self.admin_request(
            "GET", f"{SANDBOXES_PATH}/{sandbox['sandbox_id']}/diff", params=params
        )
        return dict(response.json())

    async def aliases(self, dataset: str) -> dict[str, Any]:
        """初始数据集的别名映射，别名到内部 ID（需求 19.10）。"""
        response = await self.admin_request("GET", f"/admin/datasets/{dataset}/aliases")
        return dict(response.json())

    # --- MCP 会话 ----------------------------------------------------------------------

    async def _open_session(self) -> str:
        """在 ``/s/<沙箱 ID>/mcp`` 上开一个会话，返回 ``Mcp-Session-Id``（需求 14.2）。"""
        sandbox = self._require_sandbox()
        client = self._require_mcp()
        path = sandbox_path(str(sandbox["sandbox_id"]))
        handshake = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        }
        try:
            response = await client.post(path, json=handshake, headers=MCP_HEADERS)
        except httpx.TransportError as error:
            raise PlaygroundError(f"MCP 端点 {path} 连不上：{error}") from error
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise PlaygroundError(
                f"initialize 返回 {response.status_code}（沙箱可能已被冻结或删除）", response.text
            )
        session_id = response.headers.get(SESSION_HEADER)
        if not session_id:
            raise PlaygroundError("initialize 的响应没有带 Mcp-Session-Id", response.text)
        # 真实客户端会发的那条通知，发完才算握手结束。
        await client.post(
            path,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**MCP_HEADERS, SESSION_HEADER: session_id},
        )
        self._session_id = session_id
        return session_id

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id
        async with self._lock:
            if self._session_id is not None:
                return self._session_id
            return await self._open_session()

    def _rpc_id(self) -> int:
        self._next_rpc_id += 1
        return self._next_rpc_id

    async def rpc(self, method: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """发一条 JSON-RPC 请求，返回整条响应（``result`` 或 ``error`` 都原样给出）。

        会话被回收时（沙箱冻结、服务重启过）MCP 端点回 404，这里重开一次会话再发一遍；第二次
        还是 404 就抛错，让界面提示去新建沙箱。
        """
        sandbox = self._require_sandbox()
        client = self._require_mcp()
        path = sandbox_path(str(sandbox["sandbox_id"]))
        for attempt in (1, 2):
            session_id = await self._ensure_session()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._rpc_id(), "method": method}
            if params is not None:
                payload["params"] = dict(params)
            try:
                response = await client.post(
                    path, json=payload, headers={**MCP_HEADERS, SESSION_HEADER: session_id}
                )
            except httpx.TransportError as error:
                raise PlaygroundError(f"MCP 端点 {path} 连不上：{error}") from error
            if response.status_code == httpx.codes.NOT_FOUND and attempt == 1:
                self._session_id = None
                continue
            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise PlaygroundError(f"{method} 返回 {response.status_code}", response.text)
            return dict(response.json())
        raise PlaygroundError("MCP 会话无法重建，请新建一个沙箱")

    async def tools(self) -> list[dict[str, Any]]:
        """当前 profile 暴露的全部工具，顺序就是 ``tools/list`` 的顺序（需求 14.5）。"""
        envelope = await self.rpc("tools/list", {})
        error = envelope.get("error")
        if error is not None:
            raise PlaygroundError("tools/list 失败", json.dumps(error, ensure_ascii=False))
        result = envelope.get("result") or {}
        return [dict(item) for item in result.get("tools", [])]

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """调一次工具，把界面要显示的东西一起返回。

        界面需要的不只是 ``structuredContent``：业务错误对象在 ``content[0].text`` 里
        （需求 13.6），而 ``writes``、``coerced``、``rule_id``、``latency_ms`` 只有 trace 事件
        里有（需求 17.3），所以调用后顺手把这次调用对应的那条 ``tools_call`` 事件捞出来。
        """
        started = time.perf_counter()
        envelope = await self.rpc("tools/call", {"name": tool, "arguments": dict(arguments)})
        duration_ms = (time.perf_counter() - started) * 1000
        rpc_error = envelope.get("error")
        result = envelope.get("result") or {}
        is_error = bool(rpc_error is not None or result.get("isError"))
        text = _first_text(result)
        answer: dict[str, Any] = {
            "tool": tool,
            "arguments": dict(arguments),
            "is_error": is_error,
            "structured": result.get("structuredContent"),
            "text": text,
            "error": _business_error(text) if result.get("isError") else None,
            "rpc_error": rpc_error,
            "raw": envelope,
            "duration_ms": duration_ms,
            "event": None,
        }
        with contextlib.suppress(PlaygroundError):
            answer["event"] = _last_call_event(await self.trace())
        self._history.append(
            CallRecord(
                seq=len(self._history) + 1,
                tool=tool,
                arguments=dict(arguments),
                is_error=is_error,
                duration_ms=duration_ms,
            )
        )
        with contextlib.suppress(PlaygroundError):
            await self.refresh_sandbox()
        return answer


def _wait_quietly(process: subprocess.Popen[bytes]) -> None:
    """等子进程退出；超时就强杀，调试台不该因为它赖着不走而卡住。"""
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=STOP_TIMEOUT_S)


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    """JSON Lines 文本里的事件；读不动的行跳过，界面要的是能看的那些。"""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _first_text(result: Mapping[str, Any]) -> str:
    """``content`` 里第一个文本项，没有就空串（需求 13.1：成功与失败都只有一项）。"""
    for item in result.get("content") or []:
        if isinstance(item, Mapping) and item.get("type") == "text":
            return str(item.get("text", ""))
    return ""


def _business_error(text: str) -> dict[str, Any] | None:
    """失败调用的 ``{"error": {...}}`` 里的那个 error 对象（需求 13.6）。"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, Mapping) and isinstance(parsed.get("error"), Mapping):
        return dict(parsed["error"])
    return None


def _last_call_event(events: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """trace 里最后一条 ``tools_call`` 事件，也就是刚刚那次调用的（需求 17.3）。"""
    for event in reversed(events):
        if event.get("event") == TOOLS_CALL_EVENT:
            return dict(event)
    return None


# --- HTTP 接口 ----------------------------------------------------------------------------


def _ok(payload: Any) -> JSONResponse:
    return JSONResponse(payload)


def _failed(error: PlaygroundError) -> JSONResponse:
    return JSONResponse({"error": error.message, "detail": error.detail}, status_code=400)


async def _body(request: Request) -> dict[str, Any]:
    """请求体，空体当成 ``{}``；不是 JSON 对象就是 400。"""
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PlaygroundError(f"请求体不是 JSON：{error}") from error
    if not isinstance(parsed, dict):
        raise PlaygroundError("请求体必须是 JSON 对象")
    return parsed


def build_app(supervisor: Supervisor, options: Options) -> Starlette:
    """调试台的 ASGI 应用：一张页面加几个 JSON 接口。"""

    async def page(request: Request) -> Response:
        if not HTML_FILE.is_file():
            return Response(f"缺少界面文件 {HTML_FILE}", status_code=500, media_type="text/plain")
        return HTMLResponse(HTML_FILE.read_text(encoding="utf-8"))

    async def state(request: Request) -> Response:
        return _ok(supervisor.state())

    async def reload(request: Request) -> Response:
        try:
            body = await _body(request)
            wanted = body.get("instance")
            instance = None if not wanted else Path(str(wanted)).resolve()
            await supervisor.start(instance)
        except PlaygroundError as error:
            return _failed(error)
        return _ok(supervisor.state())

    async def tools(request: Request) -> Response:
        try:
            return _ok({"tools": await supervisor.tools()})
        except PlaygroundError as error:
            return _failed(error)

    async def call(request: Request) -> Response:
        try:
            body = await _body(request)
            tool = str(body.get("tool") or "")
            if not tool:
                raise PlaygroundError("缺少 tool")
            arguments = body.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise PlaygroundError("arguments 必须是 JSON 对象")
            return _ok(await supervisor.call(tool, arguments))
        except PlaygroundError as error:
            return _failed(error)

    async def sandbox_create(request: Request) -> Response:
        try:
            return _ok(await supervisor.create_sandbox(await _body(request)))
        except PlaygroundError as error:
            return _failed(error)

    async def sandbox_reset(request: Request) -> Response:
        try:
            return _ok(await supervisor.reset_sandbox())
        except PlaygroundError as error:
            return _failed(error)

    async def snapshot_save(request: Request) -> Response:
        try:
            body = await _body(request)
            name = str(body.get("name") or "")
            if not name:
                raise PlaygroundError("缺少快照名")
            return _ok({"snapshots": await supervisor.save_snapshot(name)})
        except PlaygroundError as error:
            return _failed(error)

    async def snapshot_restore(request: Request) -> Response:
        try:
            body = await _body(request)
            name = str(body.get("name") or "")
            if not name:
                raise PlaygroundError("缺少快照名")
            return _ok(await supervisor.restore_snapshot(name))
        except PlaygroundError as error:
            return _failed(error)

    async def trace(request: Request) -> Response:
        try:
            return _ok({"events": await supervisor.trace()})
        except PlaygroundError as error:
            return _failed(error)

    async def diff(request: Request) -> Response:
        try:
            return _ok(await supervisor.diff(request.query_params.get("base")))
        except PlaygroundError as error:
            return _failed(error)

    async def aliases(request: Request) -> Response:
        try:
            dataset = request.query_params.get("dataset") or ""
            if not dataset:
                raise PlaygroundError("缺少 dataset")
            return _ok(await supervisor.aliases(dataset))
        except PlaygroundError as error:
            return _failed(error)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # 启动失败不该让调试台也起不来：实例配置不过加载校验时，界面要能显示那些错误，改完
        # YAML 点一下「重新加载」就是下一次尝试。
        try:
            await supervisor.start()
        except PlaygroundError as error:
            _say(f"实例没有加载成功：{error.message}")
            if error.detail:
                sys.stderr.write(error.detail)
                sys.stderr.flush()
        url = f"http://{options.host}:{options.port}/"
        _say(f"调试台： {url}")
        if options.open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            yield
        finally:
            await supervisor.stop()

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/state", state),
            Route("/api/reload", reload, methods=["POST"]),
            Route("/api/tools", tools),
            Route("/api/call", call, methods=["POST"]),
            Route("/api/sandbox", sandbox_create, methods=["POST"]),
            Route("/api/sandbox/reset", sandbox_reset, methods=["POST"]),
            Route("/api/sandbox/snapshot", snapshot_save, methods=["POST"]),
            Route("/api/sandbox/restore", snapshot_restore, methods=["POST"]),
            Route("/api/trace", trace),
            Route("/api/diff", diff),
            Route("/api/aliases", aliases),
        ],
        lifespan=lifespan,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """起调试台，直到 Ctrl-C；返回进程的退出码。"""
    options = parse_args(argv)
    if not COMMAND.is_file():
        _say(f"找不到命令 {COMMAND}，先在 erp-mock-mcp/ 下执行 uv sync --locked")
        return 1
    # 端口先试着绑一下再往下走：uvicorn 绑不上时才报错的话，serve 子进程已经为一次起不来的
    # 调试台白跑了一遍加载校验与快照构建。
    try:
        bind_socket(options.host, options.port).close()
    except OSError as error:
        _say(f"{options.host}:{options.port} 绑不上（{error}），换一个 --port")
        return 1
    options.data_dir.mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor(options)
    config = uvicorn.Config(
        build_app(supervisor, options),
        host=options.host,
        port=options.port,
        log_config=None,
        access_log=False,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
