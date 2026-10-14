"""依赖计划：把「缺什么」变成「装什么、从哪装、多大、什么风险」，并保留待确认计划。

流水线：
    dep_scan（需求） → dep_status（本机满足度） → build_plan（干跑成计划）
    → 用户确认 → dep_apply（执行，见 handlers）→ dep_verify（回代码层复验）

本模块只做只读干跑：`apt-get install -s --no-remove`、`pip install --dry-run --report`。
计划带 plan_id + 文件指纹 + TTL，执行前要重新校验，避免「确认的和执行的不是一回事」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from bibit_intent.core.dependency_scan import (
    KIND_C_HEADER,
    KIND_COMMAND,
    KIND_NODE,
    KIND_PKGCONFIG,
    KIND_PYTHON,
    KIND_SHARED_LIB,
    Requirement,
    check_requirements,
    header_apt_candidates,
    install_target_python,
    project_fingerprint,
    requirements_from_dicts,
    shared_lib_apt_candidates,
)
from bibit_intent.core.pip_index import pip_index_args
from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command

PLAN_TTL_SECONDS = 900
_MAX_DRY_RUNS = 8
_MAX_PLAN_ITEMS = 40

_PLANS: dict[str, dict] = {}
_PLANS_LOCK = threading.Lock()

# 进程内指标：计划数 / 确认率 / 装后验证通过率（供 /metrics 观测，重启归零）。
_STATS: dict[str, int] = {
    "plans_built": 0,
    "apply_runs": 0,
    "apply_failures": 0,
    "items_installed": 0,
    "items_verified": 0,
}
_STATS_LOCK = threading.Lock()

_CHANNEL_LABELS = {
    "pip": "pip（用户 Python 环境）",
    "apt": "apt（系统包，需 sudo）",
    "manual": "需人工处理",
    "unsupported": "当前环境不支持",
    "satisfied": "已满足",
}
_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


def channel_label(channel: str) -> str:
    """安装渠道的口径说明（计划卡片、应用前置提示共用同一份文案）。

    单独暴露成函数是为了让别的模块（例如应用的前置缺失提示）不用复制这套口径：
    「apt 需 sudo」「pip 装用户 venv」这两句话只在一个地方维护。
    """
    return _CHANNEL_LABELS.get(channel, channel)


def _run(argv: list[str], *, timeout: float = 60) -> tuple[int, str, str]:
    env = {**os.environ, "LC_ALL": "C"}
    try:
        p = run_bounded_command(argv, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", f"命令不存在：{argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时：{' '.join(argv)}"
    except OutputLimitExceeded:
        return 125, "", "命令输出超过上限"
    return p.returncode, p.stdout, p.stderr


# ── apt 干跑 ──────────────────────────────────────────────────────────

def _apt_candidates(requirement: Requirement) -> list[str]:
    if requirement.kind == KIND_C_HEADER:
        return header_apt_candidates(requirement.name)
    if requirement.kind == KIND_PKGCONFIG:
        name = requirement.name
        return [name if name.endswith("-dev") else f"lib{name}-dev", name]
    if requirement.kind == KIND_COMMAND:
        return [requirement.name]
    if requirement.kind == KIND_SHARED_LIB:
        return shared_lib_apt_candidates(requirement.name)
    return []


def _apt_facts(pkg: str) -> dict:
    """只读解析一个 apt 包：候选版本、下载体积、是否会移除其它包。"""
    rc, out, err = _run(["apt-cache", "policy", pkg], timeout=30)
    if rc != 0:
        detail = (err or out).strip() or "apt-cache 查询失败"
        return {"resolvable": False, "detail": detail, "failure": classify_failure(detail)}
    candidate = ""
    source = ""
    for line in out.splitlines():
        if line.strip().startswith("Candidate:"):
            candidate = line.split(":", 1)[1].strip()
        parts = line.split()
        if not source and len(parts) >= 2 and "://" in parts[1]:
            source = parts[1]
    rc, sim, sim_err = _run(
        ["apt-get", "install", "-s", "--no-remove", "--", pkg], timeout=60
    )
    if rc != 0 or "Unable to locate package" in (sim_err + sim):
        detail_lines = [line.strip() for line in (sim_err or "").strip().splitlines() if line.strip()]
        detail = detail_lines[-1] if detail_lines else "apt 找不到该包"
        return {"resolvable": False, "detail": detail, "failure": classify_failure(detail)}
    removes = [
        line.split()[1] for line in sim.splitlines()
        if line.startswith("Remv ") and len(line.split()) > 1
    ]
    will_install = [
        line.split()[1] for line in sim.splitlines()
        if line.startswith("Inst ") and len(line.split()) > 1
    ]
    size = None
    rc, show, _ = _run(["apt-cache", "show", pkg], timeout=30)
    if rc == 0:
        for line in show.splitlines():
            if line.startswith("Size:"):
                try:
                    size = int(line.split(":", 1)[1].strip())
                except ValueError:
                    size = None
                break
    if not candidate or candidate == "(none)":
        detail = f"没有可安装的候选版本（包 {pkg}）"
        return {"resolvable": False, "detail": detail, "failure": classify_failure(detail)}
    return {
        "resolvable": True,
        "version": candidate,
        "source": source,
        "size_bytes": size,
        "removes": removes,
        "will_install": will_install,
        "detail": "",
    }


# ── pip 干跑 ──────────────────────────────────────────────────────────

def pip_environment(root: Path, python: str = "") -> tuple[str, str]:
    """Python 依赖的安装/验证目标：项目 venv → 用户共享 venv（可能待创建）。"""
    return install_target_python(root, python)


def _pip_dry_run(python: str, spec: str, *, timeout: float = 120) -> dict:
    handle = tempfile.NamedTemporaryFile(prefix="bibit-dep-report-", suffix=".json", delete=False)
    handle.close()
    report = handle.name
    try:
        rc, out, err = _run(
            # --ignore-installed：干跑要回答「装到目标环境需要什么」，
            # 不能因为借用来的解释器恰好已装该包就报告「没有可安装项」。
            [python, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps",
             "--disable-pip-version-check", "--no-input",
             "--report", report, *pip_index_args(), spec],
            timeout=timeout,
        )
        if rc != 0:
            detail = _tail(err or out)
            return {"resolvable": False, "detail": detail, "failure": classify_failure(detail)}
        raw = json.loads(Path(report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"resolvable": False, "detail": f"pip 干跑结果无法解析：{exc}"}
    finally:
        try:
            os.unlink(report)
        except OSError:
            pass
    entries = raw.get("install") or []
    if not entries:
        return {"resolvable": False, "detail": "pip 干跑没有返回可安装项"}
    entry = entries[0]
    metadata = entry.get("metadata") or {}
    url = ((entry.get("download_info") or {}).get("url")) or ""
    artifact = urlparse(url).path.rsplit("/", 1)[-1]
    host = urlparse(url).hostname or ""
    needs_build = _needs_build(artifact)
    return {
        "resolvable": True,
        "package": str(metadata.get("name") or spec),
        "version": str(metadata.get("version") or ""),
        "artifact": artifact,
        "source": host,
        "needs_build": needs_build,
        "is_direct": bool(entry.get("is_direct")),
        "requires_dist": list(metadata.get("requires_dist") or []),
        "detail": "",
    }


def _tail(text: str, limit: int = 300) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    joined = " / ".join(lines[-3:])
    return joined[:limit]


# ── 失败归类与离线降级（M3-3）───────────────────────────────────────

# 断网时的降级文案要回答两个问题：哪个源不可达、能不能离线装。
# 这里只做「识别 + 复用本地缓存」，不在计划阶段触碰网络以外的任何状态。
_NETWORK_HINTS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection reset",
    "connection timed out",
    "read timed out",
    "failed to establish a new connection",
    "max retries exceeded",
    "tunnel connection failed",
    "ssl: certificate_verify_failed",
    "proxyerror",
    "service unavailable",
    "unable to connect to",
)
_NOT_FOUND_HINTS = (
    "unable to locate package",
    "has no installation candidate",
    "no matching distribution found",
    "could not find a version that satisfies",
    "no such package",
)

_APT_SOURCES = (Path("/etc/apt/sources.list"), Path("/etc/apt/sources.list.d"))
_APT_ARCHIVES = (Path("/var/cache/apt/archives"),)

_REACHABLE: dict[str, tuple[float, bool]] = {}
_REACH_LOCK = threading.Lock()
_REACH_TTL_SECONDS = 30.0


def classify_failure(text: str) -> str:
    """把干跑失败文本归类：network（源不可达）/ not_found（源里没有）/ unknown。"""
    lowered = (text or "").lower()
    if any(hint in lowered for hint in _NETWORK_HINTS):
        return "network"
    if any(hint in lowered for hint in _NOT_FOUND_HINTS):
        return "not_found"
    return "unknown"


def _host_reachable(host: str, *, port: int = 443, timeout: float = 2.0) -> bool:
    """只读探测源站是否可达（结果缓存 30 秒，避免每个条目都等一次超时）。"""
    if not host:
        return False
    key = f"{host}:{port}"
    now = time.time()
    with _REACH_LOCK:
        cached = _REACHABLE.get(key)
        if cached is not None and now - cached[0] < _REACH_TTL_SECONDS:
            return cached[1]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            reachable = True
    except OSError:
        reachable = False
    with _REACH_LOCK:
        _REACHABLE[key] = (now, reachable)
    return reachable


def reset_reachability_cache() -> None:
    """清空可达性缓存（测试用）。"""
    with _REACH_LOCK:
        _REACHABLE.clear()


def pip_index_hosts() -> list[str]:
    """计划实际使用的 pip 索引主机（用于「哪个源不可达」）。"""
    args = pip_index_args()
    hosts: list[str] = []
    for index, token in enumerate(args):
        if token in {"-i", "--index-url", "--extra-index-url"} and index + 1 < len(args):
            host = urlparse(args[index + 1]).hostname or ""
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def apt_source_hosts() -> list[str]:
    """本机 apt 源的地址主机（sources.list 与传统 .list / deb822 .sources 都支持）。"""
    files: list[Path] = []
    for path in _APT_SOURCES:
        if path.is_dir():
            files.extend(sorted(path.glob("*.list")))
            files.extend(sorted(path.glob("*.sources")))
        elif path.is_file():
            files.append(path)
    texts: list[str] = []
    for file in files[:20]:
        try:
            texts.append(file.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    hosts: list[str] = []
    for match in re.findall(r"https?://([^/\s]+)", "\n".join(texts)):
        host = match.split("@")[-1].split(":")[0].strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts[:5]


def apt_cached_deb(package: str) -> str:
    """本地 apt 归档里已有的 .deb（断网时 apt-get install 会直接复用）。"""
    for directory in _APT_ARCHIVES:
        if not directory.is_dir():
            continue
        hits = sorted(directory.glob(f"{package}_*.deb"))
        if hits:
            return hits[-1].name
    return ""


def _pip_cache_dir(python: str, *, timeout: float = 30) -> str:
    rc, out, _ = _run([python, "-m", "pip", "cache", "dir"], timeout=timeout)
    if rc != 0:
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def pip_cached_wheel(python: str, name: str, *, timeout: float = 30) -> dict:
    """本地 pip wheel 缓存里可离线复用的产物（只查询，不安装）。"""
    rc, out, _ = _run([python, "-m", "pip", "cache", "list", name], timeout=timeout)
    if rc != 0:
        return {}
    artifact = ""
    for line in out.splitlines():
        # 每行形如 " - blis-1.3.3-cp314-cp314-linux_riscv64.whl (3.4 MB)"
        token = line.strip().lstrip("-").strip().split(" ", 1)[0]
        if token.endswith(".whl"):
            artifact = token
    if not artifact:
        return {}
    cache_dir = _pip_cache_dir(python, timeout=timeout)
    wheel_path = ""
    if cache_dir and Path(cache_dir).is_dir():
        matches = sorted(Path(cache_dir).glob(f"**/{artifact}"))
        wheel_path = str(matches[-1]) if matches else ""
    directory = str(Path(wheel_path).parent) if wheel_path else cache_dir
    command = f"{python} -m pip install --no-index"
    if directory:
        command += f" --find-links {directory}"
    return {"artifact": artifact, "path": wheel_path, "command": f"{command} {name}"}


def offline_facts(kind: str, *, python: str = "", package: str = "", name: str = "") -> dict:
    """该项在断网时能不能装：查本地 apt 归档 / pip wheel 缓存。"""
    if kind == "pip":
        wheel = pip_cached_wheel(python or "python3", name or package)
        if not wheel:
            return {"available": False, "artifact": "", "command": "", "detail": "本地 pip wheel 缓存里没有可复用的产物"}
        return {"available": True, "detail": f"本地 wheel 缓存有 {wheel['artifact']}", **wheel}
    if kind == "apt":
        deb = apt_cached_deb(package)
        if not deb:
            return {"available": False, "artifact": "", "command": "", "detail": "本地 apt 归档里没有该包的 .deb"}
        return {
            "available": True,
            "artifact": deb,
            "command": f"sudo apt-get install --no-download --no-remove -- {package}",
            "detail": f"本地 apt 归档有 {deb}",
        }
    return {"available": False, "artifact": "", "command": "", "detail": ""}


def _unreachable_hosts(hosts: list[str], *, port: int = 443) -> list[str]:
    return [host for host in hosts if not _host_reachable(host, port=port)]


def _apt_sources_unreachable() -> bool:
    """所有 apt 源都连不上（http/https 任一端口都试一次）。"""
    hosts = apt_source_hosts()
    if not hosts:
        return False
    return all(
        not (_host_reachable(host, port=80) or _host_reachable(host, port=443))
        for host in hosts
    )


def _needs_build(artifact: str) -> bool:
    return artifact.endswith((".tar.gz", ".zip", ".tar.bz2", ".tar.xz"))


# ── 源码构建工具 ──────────────────────────────────────────────────────

# sdist 构建的最小工具集：命令 → 缺失时用哪个 apt 包补、为什么要它。
# C++ 扩展另外需要 g++、Debian 源码构建需要 dpkg-dev，都不放进「必需集合」
# （避免为一个纯 C 扩展多装一堆包），计划里按需提示。
_BUILD_TOOL_SPECS: tuple[dict[str, str], ...] = (
    {"tool": "gcc", "apt": "gcc", "why": "编译 C 扩展"},
    {"tool": "make", "apt": "make", "why": "驱动构建脚本"},
    {"tool": "pkg-config", "apt": "pkg-config", "why": "探测系统库"},
    {"tool": "python3-dev", "apt": "python3-dev", "why": "Python 头文件（Python.h）",
     "probe": "python_headers"},
)
_BUILD_TOOL_SPEC = {spec["tool"]: spec for spec in _BUILD_TOOL_SPECS}


def _python_headers_present() -> bool:
    try:
        include = Path(sysconfig.get_paths()["include"])
    except (KeyError, OSError):
        return False
    return (include / "Python.h").is_file()


def _tool_present(spec: dict) -> bool:
    if spec.get("probe") == "python_headers":
        return _python_headers_present()
    return shutil.which(spec["tool"]) is not None


def _build_tool_missing() -> list[str]:
    """缺哪些源码构建工具（只探测，不改动系统）。"""
    return [spec["tool"] for spec in _BUILD_TOOL_SPECS if not _tool_present(spec)]


def build_tool_status() -> dict:
    """源码构建工具齐备度：缺哪些、缺的用哪个 apt 包装、补齐后能否离线构建。"""
    missing = _build_tool_missing()
    return {
        "ok": not missing,
        "tools": [spec["tool"] for spec in _BUILD_TOOL_SPECS],
        "missing": [
            {
                "tool": name,
                "apt": _BUILD_TOOL_SPEC[name]["apt"],
                "why": _BUILD_TOOL_SPEC[name]["why"],
            }
            for name in missing if name in _BUILD_TOOL_SPEC
        ],
        "install": [_BUILD_TOOL_SPEC[name]["apt"] for name in missing if name in _BUILD_TOOL_SPEC],
    }


# ── 计划构建 ──────────────────────────────────────────────────────────

def _item_for(
    requirement: Requirement, status: dict, *, scope: str, root: Path,
    python: str, budget: dict,
) -> dict:
    item = {
        "requirement": requirement.key(),
        "kind": requirement.kind,
        "name": requirement.name,
        "spec": requirement.spec,
        "detail": requirement.detail,
        "evidence": [entry.__dict__ for entry in requirement.evidence],
        "status": "satisfied" if status.get("satisfied") else "missing",
        "status_detail": status.get("detail", ""),
        "channel": "satisfied",
        "package": "",
        "version": "",
        "artifact": "",
        "source": "",
        "size_bytes": None,
        "risk": "low",
        "removes": [],
        "notes": [],
        "target_env": "",
        "verify": status.get("check", ""),
    }
    if status.get("satisfied") is True:
        return item
    if status.get("satisfied") is None:
        item["notes"].append(status.get("detail") or "状态未知，需要人工确认")

    if requirement.kind == KIND_PYTHON:
        if scope == "system":
            item.update(channel="manual", notes=["scope=system 不含 Python 包；请用 pip 作用域"])
            return item
        if requirement.detail == "来源非包索引，需人工确认":
            item.update(
                channel="manual",
                risk="high",
                notes=["依赖来源不是包索引（git/URL/本地路径），不自动安装"],
            )
            return item
        if budget["dry_runs"] <= 0:
            item.update(channel="pip", package=requirement.name,
                        notes=["干跑次数已达上限，未验证版本与产物"], risk="medium")
            return item
        target_python, target_env = pip_environment(root, python)
        spec = requirement.spec or requirement.name
        budget["dry_runs"] -= 1
        # 共享 venv 可能尚未创建：干跑借用可用解释器解析版本与产物，
        # 安装与验证仍以 target_python 为准（安装时才创建该 venv）。
        dry_run_python = target_python if Path(target_python).is_file() else (
            shutil.which("python3") or sys.executable
        )
        facts = _pip_dry_run(dry_run_python, spec)
        if not facts.get("resolvable"):
            detail = facts.get("detail") or "未知原因"
            failure = facts.get("failure") or classify_failure(detail)
            unreachable = ""
            offline: dict = {}
            manual_steps: list[str] = []
            if failure == "network":
                hosts = pip_index_hosts()
                unreachable = (_unreachable_hosts(hosts) or hosts or [""])[0]
                notes = [f"pip 镜像不可达（{unreachable or '未知源'}）：{_tail(detail, 160)}"]
                offline = offline_facts("pip", python=dry_run_python, name=requirement.name)
                if offline["available"]:
                    notes.append(f"{offline['detail']}，可离线安装（见手动步骤）")
                    manual_steps = [offline["command"]]
                else:
                    notes.append(
                        f"{offline['detail']}；恢复网络或换用可达镜像后重新生成计划"
                    )
            else:
                hosts = pip_index_hosts()
                notes = [
                    f"pip 源没有可用产物（{hosts[0] if hosts else '未知源'}）：{_tail(detail, 200)}"
                ]
                if failure == "not_found":
                    notes.append(
                        f"该源里没有适配本机（cp{sys.version_info.major}"
                        f"{sys.version_info.minor}/riscv64）的产物——可能本就没有这个包，"
                        "也可能只有其它平台/其它 Python 版本的 wheel；"
                        "可换回上游源，或确认源码构建工具齐备后手工安装"
                    )
            item.update(
                channel="manual", risk="high", notes=notes, failure=failure,
                offline=offline, unreachable_source=unreachable, manual_steps=manual_steps,
            )
            return item
        needs_build = bool(facts.get("needs_build"))
        notes = []
        tools = build_tool_status() if needs_build else {"ok": True, "tools": [], "missing": [], "install": []}
        missing_tools = [entry["tool"] for entry in tools["missing"]]
        if needs_build:
            notes.append("没有 riscv64 wheel，需要源码构建")
            if missing_tools:
                notes.append(
                    "缺少构建工具：" + "、".join(missing_tools)
                    + "（执行时先用 apt 补齐 " + " ".join(tools["install"])
                    + "，仍需二次确认）"
                )
            else:
                notes.append("构建工具齐备：" + "、".join(tools["tools"]))
        item.update(
            channel="pip",
            package=facts.get("package") or requirement.name,
            version=facts.get("version") or "",
            artifact=facts.get("artifact") or "",
            source=facts.get("source") or "",
            risk="high" if (needs_build and missing_tools) else ("medium" if needs_build else "low"),
            target_env=target_env,
            verify=f"import {requirement.name}",
            notes=notes,
            needs_build=needs_build,
            build_tools=tools if needs_build else {},
        )
        return item

    if requirement.kind == KIND_NODE:
        has_npm = shutil.which("npm") is not None
        item.update(
            channel="unsupported" if not has_npm else "manual",
            risk="high" if not has_npm else "medium",
            notes=["本机没有 node/npm，Node 依赖需先安装 nodejs" if not has_npm
                   else "Node 依赖暂需人工确认（npm 安装未纳入自动计划）"],
        )
        return item

    # 系统类：头文件 / pkg-config / 动态库 / 命令 → apt
    if scope == "user":
        item.update(channel="manual", notes=["scope=user 不安装系统包；请用 system 作用域"])
        return item
    if requirement.kind == KIND_COMMAND:
        if shutil.which(requirement.name):
            item.update(channel="satisfied", status="satisfied")
            return item
    candidates = _apt_candidates(requirement)
    last_failure: dict = {}
    for candidate in candidates:
        facts = _apt_facts(candidate)
        if not facts.get("resolvable"):
            last_failure = facts
            continue
        removes = facts.get("removes") or []
        notes = []
        if removes:
            notes.append("会移除其它包：" + "、".join(removes[:5]))
        if facts.get("will_install") and len(facts["will_install"]) > 1:
            notes.append(f"将新增 {len(facts['will_install'])} 个包")
        item.update(
            channel="apt",
            package=candidate,
            version=facts.get("version") or "",
            source=facts.get("source") or "",
            size_bytes=facts.get("size_bytes"),
            risk="high" if removes else "medium",
            removes=removes,
            notes=notes,
            verify=status.get("check", ""),
        )
        if removes:
            item["channel"] = "manual"
            item["notes"].append("需要移除其它包，请人工确认后再装")
            item["manual_steps"] = [f"sudo apt-get install --no-remove {candidate}"]
        return item
    failure = str(last_failure.get("failure") or "unknown") if last_failure else "unknown"
    if failure != "network" and candidates and _apt_sources_unreachable():
        # apt 索引陈旧时也会报 "Unable to locate package"，源确实不可达就改口径
        failure = "network"
    source = ""
    offline: dict = {}
    manual_steps = [f"apt-cache search {requirement.name}"]
    if failure == "network":
        hosts = apt_source_hosts()
        source = (hosts or [""])[0]
        detail = _tail(str(last_failure.get("detail") or ""), 160)
        notes = [
            f"apt 源不可达（{source or '未知源'}）：{detail or '无法连接'}",
            "先执行 sudo apt-get update，或把源切回可达镜像后重新生成计划",
        ]
        cached = next((pkg for pkg in candidates if apt_cached_deb(pkg)), "")
        offline = offline_facts("apt", package=cached) if cached else {
            "available": False, "artifact": "", "command": "", "detail": ""
        }
        if offline["available"]:
            notes.append(f"{offline['detail']}，可 --no-download 复用（见手动步骤）")
            manual_steps = [offline["command"]]
        else:
            notes.append("本地 apt 归档里没有对应 .deb，断网时无法安装")
    else:
        notes = ["无法从 apt 源解析出提供该依赖的包，请人工指定包名"]
    item.update(
        channel="manual",
        risk="high" if failure == "network" else "medium",
        notes=notes,
        manual_steps=manual_steps,
        failure=failure,
        offline=offline,
        unreachable_source=source,
    )
    return item


def build_plan(
    root: Path, requirements: list[Requirement], statuses: dict[str, dict], *,
    scope: str = "auto", python: str = "", max_dry_runs: int = _MAX_DRY_RUNS,
) -> dict:
    """合成依赖计划（只读干跑），并返回可展示、可确认的计划对象。"""
    budget = {"dry_runs": max_dry_runs}
    verify_python, _ = pip_environment(root, python)
    items = [
        _item_for(item, statuses.get(item.key(), {}), scope=scope, root=root,
                  python=python, budget=budget)
        for item in requirements[:_MAX_PLAN_ITEMS]
    ]
    for index, item in enumerate(items, start=1):
        item["index"] = index
    missing = [item for item in items if item["status"] != "satisfied"]
    actionable = [item for item in missing if item["channel"] in {"apt", "pip"}]
    fingerprint = project_fingerprint(root)
    seed = "|".join([str(root), fingerprint, scope, ",".join(item["requirement"] for item in items)])
    plan_id = "dep-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    sizes = [item["size_bytes"] for item in actionable if item.get("size_bytes")]
    plan = {
        "ok": True,
        "plan_id": plan_id,
        "project_root": str(root),
        "scope": scope,
        "python": python,
        "verify_python": verify_python,
        "created_at": time.time(),
        "expires_at": time.time() + PLAN_TTL_SECONDS,
        "ttl_seconds": PLAN_TTL_SECONDS,
        "files_hash": fingerprint,
        "items": items,
        "summary": {
            "total": len(items),
            "missing": len(missing),
            "satisfied": len(items) - len(missing),
            "actionable": len(actionable),
            "manual": len([item for item in missing if item["channel"] not in {"apt", "pip"}]),
            "download_bytes": sum(sizes) if sizes else None,
            "needs_sudo": any(item["channel"] == "apt" for item in actionable),
            "needs_network": bool(actionable),
            "needs_build": any(item.get("needs_build") for item in items),
            "offline_reusable": len([
                item for item in items if (item.get("offline") or {}).get("available")
            ]),
            "sources_unreachable": sorted({
                str(item.get("unreachable_source") or "")
                for item in items if item.get("unreachable_source")
            }),
            "build_tools_missing": sorted({
                entry["apt"]
                for item in items
                for entry in (item.get("build_tools") or {}).get("missing", [])
            }),
        },
    }
    return plan


# ── 指标 ──────────────────────────────────────────────────────────────

def _bump(**deltas: int) -> None:
    with _STATS_LOCK:
        for key, value in deltas.items():
            _STATS[key] = _STATS.get(key, 0) + int(value)


def record_apply_outcome(results: list[dict], *, overall: bool) -> None:
    """累计一次 dep_apply 的结果（执行次数、装了几项、验过几项）。"""
    installed = [item for item in results if item.get("ok")]
    verified = [item for item in installed if item.get("verified")]
    _bump(
        apply_runs=1,
        apply_failures=0 if overall else 1,
        items_installed=len(installed),
        items_verified=len(verified),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def dependency_metrics() -> dict[str, object]:
    """依赖计划的运行指标：计划数、确认率、装后验证通过率。"""
    with _STATS_LOCK:
        snapshot = dict(_STATS)
    return {
        **snapshot,
        # 「确认率」= 生成过的计划里真正被确认执行的比例
        "confirmation_rate": _rate(snapshot["apply_runs"], snapshot["plans_built"]),
        "verify_pass_rate": _rate(snapshot["items_verified"], snapshot["items_installed"]),
    }


def reset_metrics() -> None:
    """清空进程内指标（测试用）。"""
    with _STATS_LOCK:
        for key in _STATS:
            _STATS[key] = 0


# ── 计划存储与失效 ────────────────────────────────────────────────────

def save_plan(plan: dict) -> None:
    _bump(plans_built=1)
    with _PLANS_LOCK:
        now = time.time()
        for key in [key for key, value in _PLANS.items() if value.get("expires_at", 0) <= now]:
            _PLANS.pop(key, None)
        _PLANS[plan["plan_id"]] = plan


def get_plan(plan_id: str) -> tuple[dict | None, str]:
    key = (plan_id or "").strip()
    if not key:
        return None, "缺少 plan_id（先用 dep_plan 生成计划）"
    with _PLANS_LOCK:
        plan = _PLANS.get(key)
        if plan is None:
            return None, "计划不存在或服务已重启，请重新生成依赖计划"
        if plan.get("expires_at", 0) <= time.time():
            _PLANS.pop(key, None)
            return None, "计划已过期，请重新生成依赖计划"
    return plan, ""


def plan_is_current(plan: dict) -> tuple[bool, str]:
    root = Path(plan.get("project_root") or "")
    if not root.is_dir():
        return False, f"项目目录已不存在：{root}"
    current = project_fingerprint(root)
    if current != plan.get("files_hash"):
        return False, "项目文件已变更，计划失效，请重新生成依赖计划"
    return True, ""


def plan_requirements(plan: dict) -> list[Requirement]:
    return requirements_from_dicts(plan.get("items") or [])


def rollback_command(item: dict, python: str = "") -> str:
    """该项装成之后把它卸掉需要执行的命令（交给人工执行，不自动回滚）。

    apt 只卸本次装的那个包，不动它带进来的依赖（`--no-remove` 装进去的依赖
    可能是别的包在用的），所以这里刻意不写 `--auto-remove`；pip 用计划解析出
    的目标解释器，保证「装到哪个环境就从哪个环境卸」。
    """
    channel = item.get("channel")
    package = str(item.get("package") or "").strip()
    if channel == "apt" and package:
        return f"sudo apt-get remove -- {package}"
    if channel == "pip":
        name = package or str(item.get("name") or "").strip()
        if not name:
            return ""
        interpreter = python or item.get("target_env") or "python3"
        return f"{interpreter} -m pip uninstall -y {name}"
    return ""


def verify_plan(plan: dict, *, python: str = "") -> list[dict]:
    """回代码层复验计划里的每一项依赖。"""
    requirements = plan_requirements(plan)
    python = python or plan.get("verify_python") or ""
    statuses = check_requirements(requirements, root=Path(plan["project_root"]), python=python)
    results = []
    for item in plan.get("items") or []:
        status = statuses.get(item.get("requirement") or "", {})
        results.append({
            "index": item.get("index"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "channel": item.get("channel"),
            "satisfied": bool(status.get("satisfied")),
            "check": status.get("check", ""),
            "detail": status.get("detail", ""),
        })
    return results


# ── 展示 ──────────────────────────────────────────────────────────────

def _evidence_text(item: dict) -> str:
    parts = []
    for entry in (item.get("evidence") or [])[:2]:
        snippet = (entry.get("snippet") or "").strip()
        parts.append(f"{entry.get('file')}:{entry.get('line')} `{snippet}`")
    return "；".join(parts)


def format_plan_message(plan: dict) -> str:
    summary = plan["summary"]
    lines = [
        f"依赖计划 {plan['plan_id']}（项目 {plan['project_root']}）："
        f"共 {summary['total']} 项依赖，缺失 {summary['missing']} 项，"
        f"已满足 {summary['satisfied']} 项。"
    ]
    for item in plan["items"]:
        if item["status"] == "satisfied":
            lines.append(f"  {item['index']}. [{item['kind']}] {item['name']} → 已满足")
            continue
        label = _CHANNEL_LABELS.get(item["channel"], item["channel"])
        target = item.get("package") or item["name"]
        version = f" {item['version']}" if item.get("version") else ""
        if item["channel"] in {"manual", "unsupported"}:
            lines.append(
                f"  {item['index']}. [{item['kind']}] {item['name']} → {label}；"
                f"风险 {_RISK_LABELS.get(item['risk'], item['risk'])}"
            )
        else:
            artifact = ""
            if item.get("artifact"):
                artifact = "（源码包，需要编译）" if _needs_build(item["artifact"]) \
                    else "（wheel，无需编译）"
            size = ""
            if item.get("size_bytes"):
                megabytes = item["size_bytes"] / (1024 * 1024)
                size = (f"，下载约 {megabytes:.1f} MB" if megabytes >= 1
                        else f"，下载约 {item['size_bytes'] // 1024} KB")
            origin = f"，来源 {item['source']}" if item.get("source") else ""
            lines.append(
                f"  {item['index']}. [{item['kind']}] {item['name']} → {label} 安装 "
                f"{target}{version}{artifact}{size}{origin}；"
                f"风险 {_RISK_LABELS.get(item['risk'], item['risk'])}"
            )
        build = item.get("build_tools") or {}
        if item.get("needs_build"):
            if build.get("missing"):
                tools = "、".join(entry["tool"] for entry in build["missing"])
                packages = " ".join(build.get("install") or [])
                lines.append(
                    f"     构建：源码包要用 {'、'.join(build.get('tools') or [])} 编译，"
                    f"当前缺 {tools}；确认后会先执行 sudo apt-get install -y {packages} 补齐再构建"
                )
            else:
                lines.append(
                    "     构建：构建工具齐备（" + "、".join(build.get("tools") or []) + "），可直接编译"
                )
        evidence = _evidence_text(item)
        if evidence:
            lines.append(f"     证据：{evidence}")
        extra = [note for note in item.get("notes", []) if note]
        if extra:
            lines.append("     注意：" + "；".join(extra))
        for step in item.get("manual_steps", []):
            lines.append(f"     手动步骤：{step}")
    unreachable = summary.get("sources_unreachable") or []
    if unreachable:
        reusable = summary.get("offline_reusable") or 0
        if reusable:
            lines.append(
                "离线降级：源不可达（" + "、".join(unreachable) + "）；其中 "
                + str(reusable) + " 项可用本地缓存离线安装，命令见各项「手动步骤」"
            )
        else:
            lines.append(
                "离线降级：源不可达（" + "、".join(unreachable)
                + "）；本地缓存里没有可复用的产物，恢复网络前无法安装这些项"
            )
    if summary["missing"] == 0:
        lines.append("所有依赖都已满足，无需安装。")
        return "\n".join(lines)
    if summary["actionable"] == 0:
        lines.append("没有可自动安装的项，请按上面的说明人工处理。")
        return "\n".join(lines)
    lines.append(
        f"确认后回复要安装的编号（例如「安装 1,3」），"
        f"计划 {plan['ttl_seconds'] // 60} 分钟内有效；项目文件变更后需要重新生成。"
    )
    return "\n".join(lines)


def format_verify_message(results: list[dict]) -> str:
    if not results:
        return "没有可验证的依赖项。"
    ok = [item for item in results if item["satisfied"]]
    lines = [f"依赖验证：{len(ok)}/{len(results)} 项通过。"]
    for item in results:
        mark = "通过" if item["satisfied"] else "未通过"
        line = f"  - [{mark}] [{item['kind']}] {item['name']}"
        if item.get("check"):
            line += f"（{item['check']}）"
        if not item["satisfied"] and item.get("detail"):
            line += f"：{item['detail']}"
        lines.append(line)
    return "\n".join(lines)
