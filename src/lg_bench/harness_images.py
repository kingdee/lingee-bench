"""Harness 只读挂载镜像：锁文件解析、制品下载与校验、构建、状态、compose 片段。

设计见 docs/harness-images.md。要点：
- 锁文件 ``docker/harness/harness-lock.json`` 与 ``docker/harness/<name>/`` 下所有文件共同决定
  完整 lockhash（SHA-256）；镜像标签取其前 12 位，镜像 label 与 MANIFEST 记录完整值。
- 制品在宿主机下载并按 sha256 / npm integrity 校验，缓存在 ``.cache/lg-harness/blobs/``；
  Dockerfile 构建阶段再校验一次，任何不一致都让构建失败。
- 每次构建在镜像之外写一条构建记录（``.cache/lg-harness/builds.json``：镜像 ID、完整
  lockhash、平台），用于发现本地标签被改指到别的镜像。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

# 与 configs/jobs 等相对路径一致：以当前目录（仓库根）为准，可用 LG_BENCH_ROOT 覆盖。
ROOT = Path(os.environ.get("LG_BENCH_ROOT") or Path.cwd()).resolve()
HARNESS_DIR = ROOT / "docker" / "harness"
LOCK_PATH = HARNESS_DIR / "harness-lock.json"
CACHE_DIR = ROOT / ".cache" / "lg-harness"
BLOB_DIR = CACHE_DIR / "blobs"
RECORD_PATH = CACHE_DIR / "builds.json"
COMPOSE_DIR = ROOT / "configs" / "compose" / "harness"
IMAGE_PREFIX = "lg-bench/harness-"
MOUNT_ROOT = "/opt/lg-harness"
TAG_HASH_LEN = 12
ARCH_TO_PLATFORM = {"aarch64": "linux/arm64", "arm64": "linux/arm64",
                    "x86_64": "linux/amd64", "amd64": "linux/amd64"}
DEB_ARCH = {"linux/arm64": "arm64", "linux/amd64": "amd64"}


class HarnessImageError(RuntimeError):
    pass


# ---------------------------------------------------------------- 锁与哈希
def load_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def harness_names(lock: dict | None = None) -> list[str]:
    return sorted((lock or load_lock())["harness"])


def _entry(name: str, lock: dict | None = None) -> dict[str, Any]:
    lock = lock or load_lock()
    if name not in lock["harness"]:
        raise HarnessImageError(f"unknown harness {name!r}; known: {', '.join(harness_names(lock))}")
    return lock["harness"][name]


def _context_files(name: str) -> dict[str, str]:
    base = HARNESS_DIR / name
    if not base.is_dir():
        raise HarnessImageError(f"missing build context {base}")
    files = {}
    for path in sorted(p for p in base.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        files[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def lockhash(name: str, lock: dict | None = None) -> str:
    """完整 lockhash：锁条目（全部平台）+ base + 构建上下文所有文件内容。"""
    lock = lock or load_lock()
    entry = json.loads(json.dumps(_entry(name, lock)))
    for spec in entry["platforms"].values():
        spec.pop("tree_sha256", None)  # 构建产物的哈希，不是构建输入
    # 只纳入该 harness 实际使用的 base 字段，避免改动其他类型的基础镜像牵连所有标签。
    keys = ("min_glibc", "node", "node_version") if entry.get("kind") == "node" \
        else ("builder", "min_glibc")
    base = {k: lock["base"][k] for k in keys if k in lock["base"]}
    payload = {"schema": lock["schema"], "base": base, "name": name,
               "harness": entry, "files": _context_files(name)}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def image_tag(name: str, lock: dict | None = None) -> str:
    lock = lock or load_lock()
    return f"{IMAGE_PREFIX}{name}:{_entry(name, lock)['version']}-{lockhash(name, lock)[:TAG_HASH_LEN]}"


def mount_path(name: str) -> str:
    return f"{MOUNT_ROOT}/{name}"


def expected_binary(name: str, lock: dict | None = None) -> dict[str, dict[str, Any]]:
    """{平台: {path, sha256, tree_sha256}}；供 adapter 在容器内对照仓库锁文件校验。

    尚未锁定的平台（二进制或 Node 系的文件树哈希为空）不返回；在该平台上安装会失败。
    """
    entry = _entry(name, lock)
    out = {}
    for platform, spec in entry["platforms"].items():
        binary = spec["binary"]
        if not binary.get("sha256"):
            continue
        if entry.get("kind") == "node" and not spec.get("tree_sha256"):
            continue
        out[platform] = {"path": binary["path"], "sha256": binary["sha256"],
                         "tree_sha256": spec.get("tree_sha256")}
    return out


# ---------------------------------------------------------------- docker
def _docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True,
                          capture_output=capture, timeout=None)


def docker_available() -> bool:
    try:
        return _docker("version", "--format", "{{.Server.APIVersion}}", check=False).returncode == 0
    except FileNotFoundError:
        return False


def host_platform() -> str:
    arch = _docker("info", "--format", "{{.Architecture}}").stdout.strip()
    if arch not in ARCH_TO_PLATFORM:
        raise HarnessImageError(f"unsupported docker architecture {arch!r}")
    return ARCH_TO_PLATFORM[arch]


def inspect_image(ref: str) -> dict[str, Any] | None:
    result = _docker("image", "inspect", ref, check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)[0]


def _read_image_file(ref: str, path: str) -> str:
    """从 FROM scratch 镜像中读出一个小文件（docker create + cp，不运行容器）。"""
    cid = _docker("create", ref, "/none").stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _docker("cp", f"{cid}:{path}", f"{tmp}/f")
            return Path(tmp, "f").read_text()
    finally:
        _docker("rm", cid, check=False)


def _load_records() -> dict[str, Any]:
    return json.loads(RECORD_PATH.read_text()) if RECORD_PATH.exists() else {}


def _save_records(records: dict[str, Any]) -> None:
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORD_PATH.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- 制品下载
def _digest_spec(artifact: dict[str, Any]) -> tuple[str, str]:
    """返回 (算法, 十六进制摘要)；npm integrity 转成 sha512 十六进制。"""
    if artifact.get("sha256"):
        return "sha256", artifact["sha256"].lower()
    integrity = artifact.get("integrity", "")
    algo, _, b64 = integrity.partition("-")
    if algo != "sha512" or not b64:
        raise HarnessImageError(f"{artifact.get('file')}: need sha256 or sha512 integrity")
    return "sha512", base64.b64decode(b64).hex()


def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_artifact(artifact: dict[str, Any], *, log=print) -> Path:
    algo, digest = _digest_spec(artifact)
    target = BLOB_DIR / f"{algo}-{digest}"
    if target.exists():
        if _hash_file(target, algo) == digest:
            return target
        target.unlink()
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    log(f"downloading {artifact['url']}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(artifact["url"], timeout=60) as resp, partial.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
        actual = _hash_file(partial, algo)
        if actual != digest:
            raise HarnessImageError(f"{artifact['file']}: {algo} mismatch (expected {digest}, got {actual})")
        if artifact.get("size") and partial.stat().st_size != artifact["size"]:
            raise HarnessImageError(f"{artifact['file']}: size mismatch")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    log(f"  verified {algo} in {time.monotonic() - started:.0f}s")
    return target


def seed_artifact(path: Path, *, lock: dict | None = None) -> str:
    """把本机已有文件按哈希放进缓存（例如已下载的 CC 二进制）；哈希不在锁文件中则拒绝。"""
    lock = lock or load_lock()
    for name in harness_names(lock):
        for spec in _entry(name, lock)["platforms"].values():
            for artifact in spec["artifacts"]:
                algo, digest = _digest_spec(artifact)
                if _hash_file(path, algo) == digest:
                    BLOB_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, BLOB_DIR / f"{algo}-{digest}")
                    return f"{name}:{artifact['file']}"
    raise HarnessImageError(f"{path}: hash not found in lock file")


# ---------------------------------------------------------------- 构建
def build(name: str, platform: str | None = None, *, log=print) -> dict[str, Any]:
    lock = load_lock()
    entry = _entry(name, lock)
    platform = platform or host_platform()
    if platform not in entry["platforms"]:
        raise HarnessImageError(f"{name}: platform {platform} not locked")
    spec = entry["platforms"][platform]
    full_hash = lockhash(name, lock)
    tag = image_tag(name, lock)

    with tempfile.TemporaryDirectory(prefix=f"lg-harness-{name}-") as tmp:
        ctx = Path(tmp)
        shutil.copytree(HARNESS_DIR / name, ctx, dirs_exist_ok=True)
        (ctx / "artifacts").mkdir()
        sums: dict[str, list[str]] = {"sha256": [], "sha512": []}
        for artifact in spec["artifacts"]:
            blob = fetch_artifact(artifact, log=log)
            dest = ctx / "artifacts" / artifact["file"]
            try:
                os.link(blob, dest)  # 大文件用硬链接，避免复制
            except OSError:
                shutil.copyfile(blob, dest)
            algo, digest = _digest_spec(artifact)
            sums[algo].append(f"{digest}  {artifact['file']}")
        for algo, lines in sums.items():
            if lines:
                (ctx / "artifacts" / f"{algo.upper()}SUMS").write_text("\n".join(lines) + "\n")
        deb_arch = DEB_ARCH[platform]
        deb_lines, deb_pins = [], []
        for deb in spec.get("debs", []):
            fname = f"{deb['package']}_{deb['version'].replace(':', '%3a')}_{deb_arch}.deb"
            deb_lines.append(f"{deb['sha256']}  {fname}")
            deb_pins.append(f"{deb['package']}={deb['version']}")
        (ctx / "debs.sha256").write_text("\n".join(deb_lines) + ("\n" if deb_lines else ""))
        manifest = {"name": name, "version": entry["version"], "lockhash": full_hash,
                    "platform": platform, "license": entry["license"],
                    "binary": spec["binary"], "artifacts": spec["artifacts"],
                    "debs": spec.get("debs", []), "builder": lock["base"]["builder"]}
        (ctx / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

        build_args = {"BUILDER": lock["base"]["builder"], "DEB_PINS": " ".join(deb_pins)}
        if entry.get("kind") == "node":
            build_args.update(NODE_IMAGE=lock["base"]["node"], NAME=name, CLI=entry["cli"],
                              NODE_SHA256=spec["binary"]["sha256"])
        cmd = ["buildx", "build", "--platform", platform, "--load", "-t", tag,
               *[a for k, v in build_args.items() for a in ("--build-arg", f"{k}={v}")],
               "--label", f"org.lgbench.harness={name}",
               "--label", f"org.lgbench.version={entry['version']}",
               "--label", f"org.lgbench.lockhash={full_hash}",
               "--label", f"org.lgbench.platform={platform}",
               str(ctx)]
        log(f"docker {' '.join(cmd[:6])} … -t {tag}")
        started = time.monotonic()
        _docker(*cmd, capture=False)
        elapsed = time.monotonic() - started

    info = inspect_image(tag)
    if not info:
        raise HarnessImageError(f"built image {tag} not found")
    tree = None
    if entry.get("kind") == "node":
        tree = _read_image_file(tag, "/TREE.sha256").strip()
        locked = spec.get("tree_sha256")
        if locked is None:
            # 首次构建（维护者）：把产物文件树哈希写回锁文件；不影响 lockhash 与标签。
            fresh = load_lock()
            fresh["harness"][name]["platforms"][platform]["tree_sha256"] = tree
            LOCK_PATH.write_text(json.dumps(fresh, indent=2, ensure_ascii=False) + "\n")
            log(f"locked {name} {platform} tree_sha256={tree}（请提交 harness-lock.json）")
        elif locked != tree:
            raise HarnessImageError(f"{name} {platform}: built tree {tree} != locked {locked}；"
                                    "构建不可复现，检查上游依赖或 node 镜像")
    record = {"image_id": info["Id"], "lockhash": full_hash, "platform": platform,
              "version": entry["version"], "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "build_seconds": round(elapsed, 1), "tree_sha256": tree}
    records = _load_records()
    records[tag] = record
    _save_records(records)
    write_compose(name, lock)
    return {"tag": tag, **record}


# ---------------------------------------------------------------- 状态与校验
def check_local(name: str, lock: dict | None = None) -> dict[str, Any]:
    """检查本机镜像：存在、label 中完整 lockhash 与仓库一致、镜像 ID 与构建记录一致。"""
    lock = lock or load_lock()
    tag = image_tag(name, lock)
    expected = lockhash(name, lock)
    out: dict[str, Any] = {"harness": name, "tag": tag, "lockhash": expected,
                           "exists": False, "errors": []}
    info = inspect_image(tag)
    if not info:
        out["errors"].append(f"image {tag} not found; run `lg-bench harness build {name}`")
        return out
    out["exists"] = True
    out["image_id"] = info["Id"]
    labels = (info.get("Config") or {}).get("Labels") or {}
    if labels.get("org.lgbench.lockhash") != expected:
        out["errors"].append("image label lockhash does not match repository lock")
    record = _load_records().get(tag)
    if record is None:
        out["errors"].append("no local build record for this tag (built outside lg-bench?)")
    elif record["image_id"] != info["Id"]:
        out["errors"].append("tag now points to a different image than the recorded build")
    out["platform"] = labels.get("org.lgbench.platform")
    return out


def compose_fragment_path(name: str) -> Path:
    return COMPOSE_DIR / f"{name}.yaml"


def compose_fragment_ref(name: str) -> str:
    return compose_fragment_path(name).relative_to(ROOT).as_posix()


def render_compose(name: str, lock: dict | None = None) -> str:
    lock = lock or load_lock()
    return (f"# 由 `lg-bench harness compose {name}` 生成，勿手改；随 docker/harness/harness-lock.json 更新。\n"
            f"# 把预构建的 {name} 只读镜像挂到 {mount_path(name)}（镜像内文件位于根目录）。\n"
            "services:\n"
            "  main:\n"
            "    volumes:\n"
            "      - type: image\n"
            f"        source: {image_tag(name, lock)}\n"
            f"        target: {mount_path(name)}\n"
            "        read_only: true\n")


def write_compose(name: str, lock: dict | None = None) -> Path:
    path = compose_fragment_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_compose(name, lock))
    return path


def compose_is_current(name: str, lock: dict | None = None) -> bool:
    path = compose_fragment_path(name)
    return path.exists() and path.read_text() == render_compose(name, lock)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lg-bench harness",
                                     description="构建与检查 harness 只读挂载镜像（见 docs/harness-images.md）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="下载并校验制品，构建本机镜像（仅此步联网）")
    p_build.add_argument("names", nargs="+", help="harness 名称或 all")
    p_build.add_argument("--platform", help="linux/arm64 或 linux/amd64；默认本机平台")
    p_status = sub.add_parser("status", help="检查本机镜像、构建记录与 compose 片段")
    p_status.add_argument("names", nargs="*")
    p_compose = sub.add_parser("compose", help="按锁文件重新生成 compose 片段")
    p_compose.add_argument("names", nargs="*")
    p_seed = sub.add_parser("seed", help="把本机已有制品按哈希放入缓存（哈希必须在锁文件中）")
    p_seed.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)

    lock = load_lock()

    def expand(names: list[str]) -> list[str]:
        return harness_names(lock) if not names or names == ["all"] else names

    try:
        if args.cmd == "build":
            for name in expand(args.names):
                print(json.dumps(build(name, args.platform, log=lambda m: print(m, file=sys.stderr)),
                                 ensure_ascii=False))
        elif args.cmd == "status":
            failed = False
            for name in expand(args.names):
                row = check_local(name, lock)
                row["compose_current"] = compose_is_current(name, lock)
                failed |= bool(row["errors"]) or not row["compose_current"]
                print(json.dumps(row, ensure_ascii=False))
            return 1 if failed else 0
        elif args.cmd == "compose":
            for name in expand(args.names):
                print(write_compose(name, lock).relative_to(ROOT))
        elif args.cmd == "seed":
            for path in args.paths:
                print(f"{path} -> {seed_artifact(path, lock=lock)}")
    except (HarnessImageError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0
