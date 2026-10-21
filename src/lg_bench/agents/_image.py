"""harness 只读挂载镜像的安装模式（``install_mode``），供各 adapter 复用。

- ``image``：要求 ``/opt/lg-harness/<name>`` 已挂载；容器内校验 MANIFEST 中的完整 lockhash、
  按**仓库锁文件**校验实际二进制 sha256、检查 glibc 版本，然后由子类做软链。失败即报错，不回退。
- ``auto``：已挂载则同 ``image``，否则走在线安装。
- ``online``：沿用原有在线安装。

每次安装都在 trial 的 agent 日志目录写 ``harness-provenance.json``。
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Sequence
from typing import Any

from harbor.environments.base import BaseEnvironment

from lg_bench import harness_images as hi

INSTALL_MODES = ("image", "auto", "online")
PROVENANCE_FILE = "harness-provenance.json"


class HarnessImageMixin:
    """子类需定义 ``HARNESS_ID``，并实现 ``_link_from_image()`` 与 ``_online_install()``。"""

    def _init_install_mode(self, install_mode: str | None) -> None:
        mode = install_mode or "auto"
        if mode not in INSTALL_MODES:
            raise ValueError(f"{self.HARNESS_ID}: install_mode must be one of {INSTALL_MODES}")
        self.install_mode = mode

    # ---- 校验脚本（在容器内执行，只用 POSIX 工具）---------------------------
    def _image_verify_command(self) -> str:
        name = self.HARNESS_ID
        root = hi.mount_path(name)
        expected = hi.lockhash(name)
        binaries = hi.expected_binary(name)
        min_glibc = hi.load_lock()["base"]["min_glibc"]
        cases = []
        for platform, spec in binaries.items():
            uname = {"linux/arm64": "aarch64", "linux/amd64": "x86_64"}[platform]
            cases.append(f"{uname}) bin={shlex.quote(spec['path'])}; want={spec['sha256']}; "
                         f"wanttree={spec.get('tree_sha256') or ''};;")
        return "\n".join([
            "set -e",
            f"root={root}",
            'test -f "$root/MANIFEST.json" || { echo "lg-harness: $root not mounted" >&2; exit 3; }',
            f'grep -q \'"lockhash": "{expected}"\' "$root/MANIFEST.json" || '
            '{ echo "lg-harness: MANIFEST lockhash mismatch" >&2; exit 4; }',
            'case "$(uname -m)" in',
            *cases,
            '*) echo "lg-harness: unsupported arch $(uname -m)" >&2; exit 5;;',
            "esac",
            'got=$(sha256sum "$root/$bin" | cut -d" " -f1)',
            '[ "$got" = "$want" ] || { echo "lg-harness: binary sha256 $got != lock $want" >&2; exit 6; }',
            # Node 系：lib/ 文件树聚合哈希现算一遍对照仓库锁文件（不信任镜像自带的 TREE.sha256）
            'if [ -n "$wanttree" ]; then '
            'gottree=$(cd "$root/lib" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d" " -f1); '
            '[ "$gottree" = "$wanttree" ] || { echo "lg-harness: lib tree $gottree != lock $wanttree" >&2; exit 8; }; fi',
            'glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null | cut -d" " -f2)',
            f'[ -n "$glibc" ] && [ "$(printf "%s\\n%s\\n" "{min_glibc}" "$glibc" | sort -V | head -1)" = "{min_glibc}" ] || '
            f'{{ echo "lg-harness: need glibc >= {min_glibc}, found ${{glibc:-none}}" >&2; exit 7; }}',
            'echo "lg-harness: verified $bin $got glibc=$glibc tree=${wanttree:-n/a}"',
        ])

    async def _mounted(self, environment: BaseEnvironment) -> bool:
        result = await environment.exec(
            command=f"test -f {hi.mount_path(self.HARNESS_ID)}/MANIFEST.json")
        return result.return_code == 0

    # ---- 入口 ----------------------------------------------------------------
    async def _install_with_mode(self, environment: BaseEnvironment) -> None:
        started = time.monotonic()
        mode = self.install_mode
        if mode == "auto":
            mode = "image" if await self._mounted(environment) else "online"
        record: dict[str, Any] = {"harness": self.HARNESS_ID, "install_mode": mode,
                                  "requested_install_mode": self.install_mode,
                                  "pinned_version": self._version}
        if mode == "image":
            tag = hi.image_tag(self.HARNESS_ID)
            record.update(lockhash=hi.lockhash(self.HARNESS_ID), image_ref=tag,
                          image_id=self._host_image_id(tag))
            try:
                result = await self.exec_as_agent(environment, command=self._image_verify_command())
            except Exception as exc:
                raise RuntimeError(
                    f"{self.HARNESS_ID}: harness image check failed ({exc}). "
                    f"Run `lg-bench harness build {self.HARNESS_ID}` and include "
                    f"{hi.compose_fragment_ref(self.HARNESS_ID)} in environment.extra_docker_compose."
                ) from exc
            record["verify"] = (result.stdout or "").strip().splitlines()[-1:]
            await self._link_from_image(environment)
        else:
            await self._online_install(environment)
        version = await environment.exec(command=self.get_version_command())
        record["cli_version_output"] = (version.stdout or "").strip().splitlines()[-1:]
        record["install_seconds"] = round(time.monotonic() - started, 1)
        (self.logs_dir / PROVENANCE_FILE).write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n")

    # ---- lg-bench preflight 钩子 ----------------------------------------------
    def preflight_checks(self, compose: Sequence[str] = (), *,
                         check_local_image: bool = True) -> tuple[list[str], list[str]]:
        """``install_mode`` 为 image/auto 时检查 compose 片段与本机镜像（不启动容器）。

        ``compose`` 由调用方统一传入，这里不需要。
        """
        errors: list[str] = []
        warnings: list[str] = []
        if self.install_mode == "online":
            warnings.append("install_mode=online：每个 trial 在线安装，不用于官方成绩")
            return errors, warnings
        name = self.HARNESS_ID
        if not hi.compose_is_current(name):
            errors.append(f"{hi.compose_fragment_ref(name)} 与锁文件不一致；运行 `lg-bench harness compose {name}`")
        if not check_local_image:
            warnings.append("已显式跳过本机 harness 镜像检查；compose 锁文件一致性仍检查")
            return errors, warnings
        if not hi.docker_available():
            warnings.append("docker 不可用，跳过本机 harness 镜像检查")
            return errors, warnings
        problems = hi.check_local(name)["errors"]
        if problems:
            target = errors if self.install_mode == "image" else warnings
            target += [f"harness image: {p}" for p in problems]
        return errors, warnings

    @staticmethod
    def _host_image_id(tag: str) -> str | None:
        try:
            info = hi.inspect_image(tag)
        except Exception:
            return None
        return info["Id"] if info else None
