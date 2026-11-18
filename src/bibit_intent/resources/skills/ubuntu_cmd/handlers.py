"""
skills/ubuntu_cmd/handlers.py — Ubuntu 命令查询 Skill 的执行实现

依据 wiki.ubuntu.org.cn/UbuntuSkills 提炼的只读运维命令：
  - 软件包：dpkg -L / apt-cache search|show|depends|rdepends / dpkg-query -W / dpkg -S
  - 系统：uname -a / lsb_release / lspci / lsusb / uptime
  - 硬盘：df -h / du -hs / lsblk
  - 进程：ps aux / 按内存、CPU 排序 / pstree
  - 网络：ip addr / ss -tulpn
  - 服务：systemctl status / service --status-all
  - 用户：last / w

安全约束（对照 design.md §12）：
  - 参数化 subprocess（列表形式），绝不 shell 字符串拼接
  - 全部只读命令，无破坏性操作
  - 命令缺失/超时返回明确错误，不影响主流程
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bibit_intent.execution.cancellation import run_command
from bibit_intent.execution.md_format import code_block as _code_block, md_table as _md_table
from bibit_intent.core.package_state import query_package_state
from bibit_intent.core.package_preview import format_preview_message, preview_packages
from bibit_intent.core.package_checks import (
    package_candidate,
    package_operation_preview,
    software_delivery_assess as assess_software_delivery,
    verify_package,
)


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    # 需要转云端：非 None 表示本地结果不足以作答，应升级云端。
    escalate: str | None = None
    detail_format: Literal["text", "markdown"] = "markdown"


def _run(cmd: list[str], timeout: int = 20) -> ExecResult:
    """参数化执行，cmd 必须是列表，杜绝 shell 注入。"""
    try:
        p = run_command(cmd, timeout=timeout)
        if p.returncode == 0:
            return ExecResult(True, "ok", p.stdout, detail_format="text")
        return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")
    except FileNotFoundError:
        return ExecResult(False, f"未安装命令 {cmd[0]}：请先 apt-get install 对应软件包")
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")


def _ps_table(text: str) -> str:
    """把 ps aux 输出渲染成 Markdown 表格，COMMAND 保留空格。"""
    lines = text.splitlines()
    if not lines:
        return _md_table(["USER", "PID", "%CPU", "%MEM", "COMMAND"], [])
    headers = ["USER", "PID", "%CPU", "%MEM", "VSZ", "RSS", "TTY", "STAT", "START", "TIME", "COMMAND"]
    rows: list[list[str]] = []
    for line in lines[1:]:
        cols = line.split(None, 10)
        if len(cols) < 11:
            continue
        rows.append(cols)
    return _md_table(headers, rows)


def _lshw_short_table(text: str) -> str:
    """把 lshw -short 固定列输出渲染成 Markdown 表格。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("H/W path") or set(line.strip()) == {"="}:
            continue
        path = line[:10].strip()
        device = line[10:20].strip()
        class_name = line[20:32].strip()
        description = line[32:].strip()
        rows.append([
            path or "-",
            device or "-",
            class_name or "-",
            description or "-",
        ])
    return _md_table(["路径", "设备", "类别", "描述"], rows)


# ── 软件包管理（wiki 安装升级 节）────────────────────────────────────

_PKG_NOT_FOUND_HINTS = (
    # 中文 apt/dpkg 输出
    "没有发现匹配", "无法定位", "没有找到", "未安装软件包",
    # 英文 apt/dpkg 输出
    "no packages found", "no package found", "unable to locate",
    "package .* is not installed", "not found",
)


def _is_pkg_not_found(stderr: str) -> bool:
    """apt/dpkg 失败是否为「包/文件不存在」，而非其它系统错误。"""
    low = (stderr or "").lower()
    return any(re.search(h, low) for h in _PKG_NOT_FOUND_HINTS)


def _pkg_miss(name: str, subject: str, detail: str = "") -> ExecResult:
    """包不存在 → 友好提示并升级云端（与 pkg_search 的 no_pkg_match 一致）。"""
    return ExecResult(
        False,
        f"未找到软件包 {name or '该名称'} 的{subject}：包名可能有误或不在软件源中",
        detail,
        escalate="pkg_not_found",
        detail_format="text",
    )


def pkg_files(pkg: str = "", **_) -> ExecResult:
    """查看软件 xxx 安装内容：dpkg -L xxx"""
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    r = _run(["dpkg", "-L", pkg], timeout=30)
    if not r.ok and _is_pkg_not_found(r.detail):
        return _pkg_miss(pkg, "安装文件清单", r.detail)
    return r


def pkg_candidate(pkg: str = "", **_) -> ExecResult:
    result = package_candidate(pkg)
    return ExecResult(result["ok"], result["message"], json.dumps(result, ensure_ascii=False))


def software_delivery_assess(target: str = "", version: str = "", scope: str = "", **_) -> ExecResult:
    result = assess_software_delivery(target, version, scope)
    return ExecResult(result["ok"], result["message"], json.dumps(result, ensure_ascii=False))


def pkg_operation_preview(pkg: str = "", operation: str = "", **_) -> ExecResult:
    result = package_operation_preview(pkg, operation)
    return ExecResult(result["ok"], result["message"], json.dumps(result, ensure_ascii=False))


def pkg_verify(pkg: str = "", expected_version: str = "", executable: str = "", **_) -> ExecResult:
    result = verify_package(pkg, expected_version, executable)
    return ExecResult(result["ok"], result["message"], json.dumps(result, ensure_ascii=False))


def pkg_match_preview(pattern: str = "", mode: str = "", scope: str = "", limit: int = 20, **_) -> ExecResult:
    preview = preview_packages(pattern, mode, scope, limit)
    return ExecResult(preview["ok"], format_preview_message(preview), json.dumps(preview, ensure_ascii=False))


def pkg_search(query: str = "", **_) -> ExecResult:
    """查找软件库中的软件：apt-cache search 正则表达式"""
    if not query:
        return ExecResult(False, "缺少搜索关键词")
    r = _run(["apt-cache", "search", "--", query], timeout=60)
    if r.ok and not r.detail.strip():
        return ExecResult(False, "未找到匹配的软件包", "", escalate="no_pkg_match")
    return r


def pkg_info(pkg: str = "", **_) -> ExecResult:
    """显示包的信息：apt-cache show xxx"""
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    r = _run(["apt-cache", "show", pkg], timeout=30)
    if not r.ok and _is_pkg_not_found(r.detail):
        return _pkg_miss(pkg, "详细信息", r.detail)
    if not r.ok:
        return r
    records = [record for record in re.split(r"\n\s*\n", r.detail.strip()) if record.strip()]
    if not records:
        return _pkg_miss(pkg, "详细信息", r.detail)
    fields = {}
    for line in records[0].splitlines():
        if line[:1].isspace():
            continue
        key, separator, value = line.partition(":")
        if separator:
            fields[key] = value.strip()
    r.message = (
        f"软件源中找到 {len(records)} 条软件包记录（不代表已安装）。\n"
        f"首条记录：{fields.get('Package', pkg)}\n"
        f"版本：{fields.get('Version', '未知')}\n"
        f"架构：{fields.get('Architecture', '未知')}"
    )
    state = query_package_state(pkg)
    installed = {
        "installed": f"已安装 {state.version}" + ("（hold）" if state.held else ""),
        "absent": "未安装",
        "config_only": "未安装，仅保留配置",
        "incomplete": "未配置完成或状态异常",
        "unknown": "查询失败或目标不唯一，不能确定是否已安装",
    }[state.status]
    r.message += f"\n本机状态：{installed}"
    return r


def pkg_installed(filter: str = "", **_) -> ExecResult:
    """查看已经安装了哪些包：dpkg -l（用 dpkg-query -W 取已装列表）"""
    r = _run(
        ["dpkg-query", "-W", "-f=${binary:Package} ${Version}\n"],
        timeout=30,
    )
    if not r.ok:
        return r
    lines = r.detail.splitlines()
    if filter:
        lines = [l for l in lines if filter in l]
    msg = f"共 {len(lines)} 个软件包" + (f"（过滤 {filter}）" if filter else "")
    if not lines:
        return ExecResult(True, msg, "(无匹配)")
    return ExecResult(True, msg, _code_block("\n".join(lines), 3000))


def pkg_owns_file(file: str = "", **_) -> ExecResult:
    """查找文件属于哪个包：dpkg -S filename"""
    if not file:
        return ExecResult(False, "缺少文件路径")
    return _run(["dpkg", "-S", file], timeout=30)


def pkg_depends(pkg: str = "", **_) -> ExecResult:
    """查询软件 xxx 依赖哪些包：apt-cache depends xxx"""
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    r = _run(["apt-cache", "depends", pkg], timeout=30)
    if not r.ok and _is_pkg_not_found(r.detail):
        return _pkg_miss(pkg, "依赖信息", r.detail)
    return r


def pkg_rdepends(pkg: str = "", **_) -> ExecResult:
    """查询软件 xxx 被哪些包依赖：apt-cache rdepends xxx"""
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    r = _run(["apt-cache", "rdepends", pkg], timeout=30)
    if not r.ok and _is_pkg_not_found(r.detail):
        return _pkg_miss(pkg, "反向依赖信息", r.detail)
    return r


# ── 系统信息（wiki 系统 节）──────────────────────────────────────────

def sys_kernel(**_):
    """查看内核：uname -a"""
    return _run(["uname", "-a"])


def sys_version(**_):
    """查看系统版本：lsb_release -a，缺 lsb_release 时回退 /etc/os-release"""
    if subprocess.run(["which", "lsb_release"], capture_output=True).returncode == 0:
        return _run(["lsb_release", "-a"], timeout=15)
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
        return ExecResult(True, "ok", text, detail_format="text")
    except OSError as e:
        return ExecResult(False, f"读取 /etc/os-release 失败: {e}")


_OS_RELEASE_LABELS = {
    "NAME": "系统名称",
    "VERSION": "版本",
    "ID": "发行版 ID",
    "VERSION_ID": "版本号",
    "PRETTY_NAME": "完整名称",
    "VERSION_CODENAME": "代号",
}


def _read_meminfo_kb() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(":")
            parts = value.strip().split()
            if parts:
                try:
                    info[key] = int(parts[0])
                except ValueError:
                    pass
    except OSError:
        pass
    return info


def _gib_from_kb(kb: int) -> str:
    return f"{kb / (1024 * 1024):.2f} GiB"


def _format_uptime_seconds(seconds: float) -> str:
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if secs and not parts:
        parts.append(f"{secs} 秒")
    return " ".join(parts) or "0 秒"


def _system_version_table() -> str:
    rows: list[list[str]] = []
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            label = _OS_RELEASE_LABELS.get(key)
            if label:
                rows.append([label, value])
    except OSError:
        pass
    if rows:
        return _md_table(["项目", "数值"], rows)
    r = _run(["lsb_release", "-a"], timeout=15)
    if r.ok:
        return _code_block(r.detail)
    return _md_table(["项目", "数值"], [["状态", "未获取到系统版本"]])


def _kernel_version_table() -> str:
    r = _run(["uname", "-r"], timeout=10)
    if r.ok:
        return _md_table(["项目", "数值"], [["内核版本", r.detail.strip()]])
    return _md_table(["项目", "数值"], [["状态", (r.detail or r.message).strip() or "未获取到内核版本"]])


def _cpu_overview_table() -> str:
    cpu = sys_cpu()
    if cpu.ok:
        return cpu.detail
    return _md_table(["项目", "数值"], [["状态", (cpu.detail or cpu.message).strip() or "未获取到 CPU 信息"]])


def _memory_size_table() -> str:
    info = _read_meminfo_kb()
    total_kb = info.get("MemTotal", 0)
    available_kb = info.get("MemAvailable", info.get("MemFree", 0))
    if total_kb:
        return _md_table(
            ["项目", "数值"],
            [
                ["总内存", _gib_from_kb(total_kb)],
                ["可用内存", _gib_from_kb(available_kb)],
            ],
        )
    memory = sys_mem_size()
    if memory.ok:
        return memory.detail
    return _md_table(["项目", "数值"], [["状态", (memory.detail or memory.message).strip() or "未获取到内存信息"]])


def _uptime_table() -> str:
    r = _run(["uptime", "-p"], timeout=10)
    if r.ok:
        value = r.detail.strip()
    else:
        try:
            value = _format_uptime_seconds(float(Path("/proc/uptime").read_text().split()[0]))
        except (OSError, ValueError, IndexError):
            value = "未获取到开机时长"
    return _md_table(["项目", "数值"], [["开机时长", value]])


def _arch_table() -> str:
    rows: list[list[str]] = []
    arch = _run(["uname", "-m"], timeout=10)
    if arch.ok:
        rows.append(["机器架构", arch.detail.strip()])
    bits = _run(["getconf", "LONG_BIT"], timeout=10)
    if bits.ok:
        rows.append(["位数", bits.detail.strip() + " bit"])
    if rows:
        return _md_table(["项目", "数值"], rows)
    return _md_table(["项目", "数值"], [["状态", "未获取到系统位数"]])


def _disk_overview_table() -> str:
    disk = disk_usage()
    if disk.ok:
        return disk.detail
    return _md_table(["项目", "数值"], [["状态", (disk.detail or disk.message).strip() or "未获取到磁盘信息"]])


def _temperature_overview_table() -> str:
    rows: list[list[str]] = []
    for tz in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            value = int(Path(tz, "temp").read_text(encoding="utf-8").strip()) / 1000
        except (OSError, ValueError):
            continue
        label = tz.name
        type_path = Path(tz, "type")
        try:
            sensor_type = type_path.read_text(encoding="utf-8").strip()
        except OSError:
            sensor_type = ""
        if sensor_type:
            label = f"{label} ({sensor_type})"
        rows.append([label, f"{value:.1f}°C"])
    if rows:
        return _md_table(["传感器", "温度"], rows)
    return _md_table(["项目", "数值"], [["状态", "未获取到温度信息"]])


def sys_overview(**_):
    """聚合查询系统版本、内核、CPU、内存与开机时长，输出 Markdown 概览。"""
    sections = [
        "## 系统概览",
        "### 系统版本\n\n" + _system_version_table(),
        "### 内核版本\n\n" + _kernel_version_table(),
        "### 系统位数\n\n" + _arch_table(),
        "### CPU\n\n" + _cpu_overview_table(),
        "### 内存\n\n" + _memory_size_table(),
        "### 磁盘\n\n" + _disk_overview_table(),
        "### 温度\n\n" + _temperature_overview_table(),
        "### 开机时长\n\n" + _uptime_table(),
    ]
    return ExecResult(True, "已获取系统概览", "\n\n".join(sections))


def sys_pci(**_):
    """查看 PCI 设备：lspci"""
    return _run(["lspci"], timeout=15)


# lsusb 一行：`Bus 001 Device 002: ID 1a40:0101 Terminus Technology Inc. Hub`
_USB_LINE_RE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)$"
)


def usb_list(**_):
    """查看 USB 设备：lsusb（输出 Markdown 表格）"""
    r = _run(["lsusb"], timeout=15)
    if not r.ok:
        return r
    rows: list[list[str]] = []
    unparsed: list[str] = []
    for line in r.detail.splitlines():
        if not line.strip():
            continue
        m = _USB_LINE_RE.match(line.strip())
        if not m:
            unparsed.append(line.strip())
            continue
        bus, dev, ids, desc = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        kind = "根集线器" if "root hub" in desc.lower() else "设备"
        rows.append([str(len(rows) + 1), bus, dev, ids, kind, desc or "-"])
    if not rows:
        return ExecResult(True, "未检测到 USB 设备", r.detail, detail_format=r.detail_format)
    roots = sum(1 for row in rows if row[4] == "根集线器")
    detail = "## USB 设备\n\n" + _md_table(
        ["#", "总线", "设备号", "VID:PID", "类型", "厂商/设备描述"], rows
    )
    if unparsed:
        detail += "\n\n### 未能解析的输出\n" + _code_block("\n".join(unparsed), 1000)
    return ExecResult(True, f"检测到 {len(rows)} 个 USB 设备（其中 {roots} 个根集线器）", detail)


def usb_tree(**_):
    """USB 设备树形拓扑：lsusb -t（等宽代码块，保留层级对齐）"""
    r = _run(["lsusb", "-t"], timeout=15)
    if not r.ok:
        return r
    if not r.detail.strip():
        return ExecResult(True, "未检测到 USB 设备", r.detail, detail_format=r.detail_format)
    lines = [ln for ln in r.detail.splitlines() if ln.strip()]
    return ExecResult(True, f"USB 设备树形结构（{len(lines)} 行）", _code_block(r.detail))


def usb_detail(**_):
    """USB 设备详细描述符：lsusb -v（非 root 下部分设备无权限，属正常）"""
    r = _run(["lsusb", "-v"], timeout=30)
    # 非 root 时 lsusb -v 会把 "Couldn't open device" 写到 stderr，
    # 但仍以 0 退出并输出可读部分；失败分支同样滤掉这些告警行再判空。
    text = r.detail if r.ok else "\n".join(
        ln for ln in r.detail.splitlines()
        if ln.strip() and not ln.startswith("Couldn't open device")
    )
    if not text.strip():
        return r
    note = "" if r.ok else "（部分设备需 root 权限，已跳过）"
    return ExecResult(True, f"USB 设备详细信息{note}：", text, detail_format="text")


def usb_reset(device: str = "", **_):
    """重置 USB 设备：usbreset /dev/bus/usb/<bus>/<dev>

    device 留空时自动挑最后一个非 root hub 设备。
    """
    if not device:
        r = _run(["lsusb"], timeout=15)
        if not r.ok:
            return ExecResult(
                False, "无法列出 USB 设备，请手动指定路径（如 /dev/bus/usb/003/002）"
            )
        bus = dev = ""
        for line in reversed(r.detail.splitlines()):
            if not line.strip() or "root hub" in line.lower():
                continue
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "Bus" and parts[2] == "Device":
                bus, dev = parts[1], parts[3].rstrip(":")
                break
        if not bus or not dev:
            return ExecResult(False, "未找到可重置的 USB 设备（只有 root hub）")
        device = f"/dev/bus/usb/{bus}/{dev}"

    if not os.path.exists(device):
        return ExecResult(False, f"USB 设备不存在：{device}")

    if not shutil.which("usbreset"):
        return ExecResult(False, "usbreset 命令不存在：请先 apt-get install usbutils")

    r = _run(["usbreset", device], timeout=15)
    if r.ok:
        return ExecResult(True, f"已重置 USB 设备：{device}", r.detail, detail_format=r.detail_format)
    return ExecResult(
        False,
        f"无法重置 {device}：设备可能正被占用，或需要 root 权限",
        r.detail,
        detail_format=r.detail_format,
    )


def sys_uptime(**_):
    """显示系统运行时间：uptime"""
    return _run(["uptime"], timeout=10)


# ── 硬盘（wiki 硬盘 节）──────────────────────────────────────────────

def disk_usage(**_):
    """查看硬盘剩余空间：df -h，渲染成 Markdown 表格。"""
    r = _run(["df", "-h"], timeout=15)
    if not r.ok:
        return r
    rows: list[list[str]] = []
    for line in r.detail.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6:
            continue
        rows.append([cols[0], cols[1], cols[2], cols[3], cols[4], " ".join(cols[5:])])
    if not rows:
        return ExecResult(True, "未获取到磁盘信息", r.detail, detail_format=r.detail_format)
    table = _md_table(
        ["文件系统", "容量", "已用", "可用", "使用率", "挂载点"], rows
    )
    return ExecResult(True, "磁盘使用情况", table)


def disk_dir_usage(path: str = ".", **_) -> ExecResult:
    """查看目录占用空间：du -hs 目录名"""
    if not path:
        path = "."
    return _run(["du", "-hs", path], timeout=60)


def disk_lsblk(**_):
    """查看块设备：lsblk"""
    return _run(["lsblk"], timeout=15)


# ── 进程（wiki 进程 节）──────────────────────────────────────────────

def proc_list(name: str = "", **_) -> ExecResult:
    """查看当前有哪些进程：ps aux（可选按名称过滤），渲染成表格。"""
    r = _run(["ps", "aux"], timeout=15)
    if not r.ok:
        return r
    lines = r.detail.splitlines()
    if name:
        filtered = [l for l in lines if name in l]
        if len(filtered) <= 1:
            return ExecResult(True, f"无匹配进程：{name}", _md_table(["USER", "PID", "COMMAND"], []))
        text = "\n".join(lines[:1] + filtered[1:])
        return ExecResult(True, f"匹配进程 {len(filtered) - 1} 行", _ps_table(text))
    return ExecResult(True, "进程列表", _ps_table(r.detail))


def proc_top_mem(**_):
    """列出前十个最耗内存的进程：ps aux --sort=-rss，渲染成表格。"""
    r = _run(["ps", "aux", "--sort=-rss"], timeout=15)
    if not r.ok:
        return r
    lines = r.detail.splitlines()
    return ExecResult(True, "内存占用 TOP10", _ps_table("\n".join(lines[:11])))


def proc_top_cpu(**_):
    """按 cpu 利用率从大到小排列进程：ps aux --sort=-pcpu，渲染成表格。"""
    r = _run(["ps", "aux", "--sort=-pcpu"], timeout=15)
    if not r.ok:
        return r
    lines = r.detail.splitlines()
    return ExecResult(True, "CPU 占用 TOP10", _ps_table("\n".join(lines[:11])))


def proc_tree(**_):
    """查看当前进程树：ps --forest，渲染成父子关系表格。"""
    r = _run(["ps", "-eo", "pid=,ppid=,stat=,args=", "--forest"], timeout=15)
    if not r.ok:
        return r
    rows: list[list[str]] = []
    for line in r.detail.splitlines():
        cols = line.split(None, 3)
        if len(cols) < 4:
            continue
        rows.append([cols[0], cols[1], cols[2], cols[3]])
    if not rows:
        return ExecResult(True, "未获取到进程", r.detail, detail_format=r.detail_format)
    return ExecResult(True, "当前进程树", _md_table(["PID", "PPID", "STAT", "COMMAND"], rows))


# ── 网络（wiki 网络 节）──────────────────────────────────────────────

def _parse_ip_json(text: str) -> list[dict] | None:
    """解析 ``ip -j addr`` 的 JSON 输出。"""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None

    facts: list[dict] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("ifname"):
            continue
        ipv4: list[str] = []
        ipv6: list[str] = []
        for addr in item.get("addr_info") or []:
            if not isinstance(addr, dict) or not addr.get("local"):
                continue
            prefix = addr.get("prefixlen")
            value = str(addr["local"])
            if prefix is not None:
                value = f"{value}/{prefix}"
            if addr.get("family") == "inet":
                ipv4.append(value)
            elif addr.get("family") == "inet6":
                ipv6.append(value)
        state = str(item.get("operstate") or item.get("state") or "-").upper()
        facts.append({
            "name": str(item["ifname"]),
            "state": state,
            "mac": str(item.get("address") or "-"),
            "ipv4": ipv4,
            "ipv6": ipv6,
        })
    return facts or None


def _parse_ip_text(text: str) -> list[dict]:
    """解析 ``ip addr`` 的文本输出，作为无 ``-j`` 支持时的兜底。"""
    facts: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        head = re.match(r"^\d+:\s+([^:@]+)[:@]", line)
        if head:
            current = {
                "name": head.group(1),
                "state": "-",
                "mac": "-",
                "ipv4": [],
                "ipv6": [],
            }
            state = re.search(r"state\s+(\S+)", line)
            if state:
                current["state"] = state.group(1).upper()
            facts.append(current)
            continue
        if current is None:
            continue
        link = re.match(r"\s+link/(?:ether|loopback)\s+(\S+)", line)
        if link:
            current["mac"] = link.group(1)
            continue
        inet4 = re.match(r"\s+inet\s+(\S+)", line)
        if inet4:
            current["ipv4"].append(inet4.group(1))
            continue
        inet6 = re.match(r"\s+inet6\s+(\S+)", line)
        if inet6:
            current["ipv6"].append(inet6.group(1))
    return facts


def _parse_ifconfig_text(text: str) -> list[dict]:
    """解析 ``ifconfig`` 的文本输出，作为最坏情况的兜底。"""
    facts: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            name = line.split(":", 1)[0].split()[0] if ":" in line else line.split()[0]
            current = {"name": name, "state": "-", "mac": "-", "ipv4": [], "ipv6": []}
            facts.append(current)
            flags = re.search(r"flags=\d+<(.+?)>", line)
            if flags:
                current["state"] = "UP" if "UP" in flags.group(1).upper() else "DOWN"
            continue
        if current is None:
            continue
        if "HWaddr" in line or ("ether" in line and "addr" in line):
            m = re.search(r"(?:HWaddr|ether)\s+(\S+)", line)
            if m:
                current["mac"] = m.group(1)
        m4 = re.search(r"inet\s+(?:addr:)?(\S+)", line)
        if m4 and "127.0.0.1" not in m4.group(1):
            current["ipv4"].append(m4.group(1))
        m6 = re.search(r"inet6\s+(?:addr:)?(\S+)", line)
        if m6:
            current["ipv6"].append(m6.group(1))
    return facts


def _collect_ip_facts() -> tuple[list[dict], ExecResult | None]:
    """收集网卡/IP 结构化事实，优先 ``ip -j addr``，逐级回退。"""
    r = _run(["ip", "-j", "addr"], timeout=15)
    if r.ok:
        facts = _parse_ip_json(r.detail)
        if facts is not None:
            return facts, None

    r2 = _run(["ip", "addr"], timeout=15)
    if r2.ok:
        facts = _parse_ip_text(r2.detail)
        if facts:
            return facts, None
        return [], ExecResult(False, "无法解析网卡信息", r2.detail, detail_format=r2.detail_format)

    r3 = _run(["ifconfig"], timeout=15)
    if r3.ok:
        facts = _parse_ifconfig_text(r3.detail)
        if facts:
            return facts, None
        return [], ExecResult(False, "无法解析网卡信息", r3.detail, detail_format=r3.detail_format)
    return [], r3


def _dns_servers() -> list[str]:
    """读取 DNS：优先 nmcli 活动连接，取不到再回退 resolv.conf。"""
    servers: list[str] = []
    nm = _run(["nmcli", "-t", "-f", "IP4.DNS,IP6.DNS", "device", "show"], timeout=10)
    if nm.ok:
        for line in nm.detail.splitlines():
            if ":" not in line:
                continue
            value = line.split(":", 1)[1].strip()
            if value and value not in servers:
                servers.append(value)
    if servers:
        return servers
    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("nameserver"):
                parts = line.split()
                if len(parts) < 2:
                    continue
                value = parts[1].strip()
                if value and value not in servers:
                    servers.append(value)
    return servers


def _format_ip_markdown(facts: list[dict], dns: list[str] | None = None) -> str:
    """把网卡事实渲染成前端可直接展示的 Markdown 表格。"""
    lines = ["## 网络信息", "", "| 网卡 | 状态 | IPv4 | IPv6 | MAC |", "|---|---|---|---|---|"]

    def cell(values: list[str]) -> str:
        if not values:
            return "-"
        return "<br>".join(str(v).replace("|", "\\|") for v in values)

    for item in facts:
        name = str(item.get("name") or "-").replace("|", "\\|")
        state = str(item.get("state") or "-").replace("|", "\\|")
        mac = str(item.get("mac") or "-").replace("|", "\\|")
        lines.append(
            f"| {name} | {state} | {cell(item.get('ipv4') or [])} | "
            f"{cell(item.get('ipv6') or [])} | {mac} |"
        )

    lines.append("")
    if dns:
        lines.append("**DNS 地址**：" + "、".join(f"`{d}`" for d in dns))
    else:
        lines.append("**DNS 地址**：未获取到")
    return "\n".join(lines)


def _facts_ipv4_addresses(facts: list[dict]) -> list[str]:
    return [str(v).split("/", 1)[0] for item in facts for v in (item.get("ipv4") or [])]


def net_ip(**_):
    """查看网卡/IP/DNS 地址，输出 Markdown 表格。"""
    facts, err = _collect_ip_facts()
    if err is not None:
        return err
    return ExecResult(True, "已获取网络信息", _format_ip_markdown(facts, _dns_servers()))


def net_conn(**_):
    """查看网络连接与监听端口：ss -tulpn，缺 ss 时回退 netstat"""
    r = _run(["ss", "-tulpn"], timeout=15)
    if r.ok:
        return r
    return _run(["netstat", "-tulpn"], timeout=15)


# ── 服务（wiki 服务 节）──────────────────────────────────────────────

_SYSTEM_UPDATE_STATE = Path('/var/lib/bibit-intent/system-update.status')
_SYSTEM_UPDATE_DETAIL = _SYSTEM_UPDATE_STATE.with_suffix('.json')
_SYSTEM_UPDATE_LABELS = {
    'refreshing': '正在刷新软件源',
    'checking': '正在检查更新',
    'awaiting_confirmation': '检测到降级，等待确认',
    'cancelled': '已取消系统更新',
    'updating': '正在更新系统',
    'system_succeeded': '系统软件包更新完成，正在准备升级 Bibit 组件',
    'agent_upgrading': '正在升级 Bibit 组件（Runtime 将短暂断开）',
    'succeeded': '系统更新已完成',
    'succeeded_with_skips': '系统更新已完成，已跳过降级软件包',
    'failed': '系统更新失败',
}


def _read_system_update_json(path: Path) -> dict:
    """读取系统更新详情 JSON；文件缺失或格式异常时返回空字典。"""
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def system_update_status(**_):
    """查询系统更新状态：状态文件 + 详情 + bibit-system-update.service。"""
    try:
        state = _SYSTEM_UPDATE_STATE.read_text(encoding='utf-8').strip()
    except OSError:
        state = ''
    detail = _read_system_update_json(_SYSTEM_UPDATE_DETAIL)

    try:
        p = run_command(['systemctl', 'is-active', 'bibit-system-update.service'], timeout=5)
        service_state = p.stdout.strip() or 'unknown'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        service_state = 'unknown'

    if not state:
        try:
            timer = run_command(['systemctl', 'is-active', 'bibit-system-update.timer'], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            timer = None
        if timer is not None and timer.stdout.strip() == 'active':
            return ExecResult(True, '系统更新任务已提交，等待后台启动；尚未完成升级。')
        return ExecResult(
            True,
            '暂无系统更新任务记录',
            '当前没有检测到系统更新状态文件。',
            detail_format='text',
        )

    label = _SYSTEM_UPDATE_LABELS.get(state, state)
    rows: list[list[str]] = [['当前状态', label], ['systemd 服务', service_state]]
    downgrades = detail.get('downgrades')
    if isinstance(downgrades, dict) and downgrades:
        items = '；'.join(f"{name}：{old} -> {new}" for name, (old, new) in sorted(downgrades.items()))
        rows.append(['涉及降级', items])
    error = detail.get('error')
    if error:
        rows.append(['错误信息', str(error)])
    return ExecResult(True, label, _md_table(['项目', '数值'], rows))

def service_status(name: str = "", **_) -> ExecResult:
    """查看服务状态：有名字用 systemctl status；无则列运行中的服务。"""
    if name:
        r = _run(["systemctl", "status", name, "--no-pager", "-l"], timeout=20)
        rows: list[list[str]] = []
        if r.ok:
            for line in r.detail.splitlines():
                key, sep, value = line.partition(":")
                if not sep or key.strip() not in {"Loaded", "Active", "Main PID", "Tasks", "Memory", "CGroup"}:
                    continue
                rows.append([key.strip(), value.strip()])
        if not rows:
            rows.append(["状态", (r.detail or r.message).strip() or "查询失败"])
        return ExecResult(True, f"服务状态：{name}", _md_table(["项目", "数值"], rows))
    r = _run(
        ["systemctl", "list-units", "--type=service", "--state=running",
         "--no-pager", "--plain"],
        timeout=20,
    )
    if not r.ok:
        return r
    rows: list[list[str]] = []
    for line in r.detail.splitlines()[1:]:
        if not line.strip():
            continue
        cols = line.split(None, 4)
        if len(cols) >= 5:
            rows.append(cols)
    return ExecResult(True, f"运行中的服务 {len(rows)} 个", _md_table(["UNIT", "LOAD", "ACTIVE", "SUB", "DESCRIPTION"], rows))


# ── 用户（wiki 用户管理 节）──────────────────────────────────────────

def login_records(**_):
    """查看系统的登录情况：last"""
    return _run(["last", "-20"], timeout=15)


def online_users(**_):
    """查看当前在线用户：w"""
    return _run(["w"], timeout=15)



# ── 软件包-统计/缓存/备份 ────────────────────────────────────────────

def pkg_stats(**_):
    """显示系统安装包的统计信息：apt-cache stats"""
    return _run(["apt-cache", "stats"], timeout=30)


def pkg_archives(**_):
    """查看安装软件时下载包的临时存放目录：ls /var/cache/apt/archives"""
    r = _run(["ls", "-lh", "/var/cache/apt/archives"], timeout=15)
    if not r.ok:
        return r
    lines = [l for l in r.detail.splitlines() if l.strip()]
    return ExecResult(True, f"共 {len(lines)} 项", _code_block(r.detail, 2000))


def pkg_selections(**_):
    """备份当前系统安装的所有包的列表：dpkg --get-selections"""
    return _run(["dpkg", "--get-selections"], timeout=30)


# ── 系统信息补充 ─────────────────────────────────────────────────────

def sys_arch(**_):
    """查看系统 32/64 位：uname -m / getconf LONG_BIT，渲染成表格。"""
    r = _run(["uname", "-m"], timeout=10)
    if not r.ok:
        return r
    rows = [[r.detail.strip()]]
    long_bit = _run(["getconf", "LONG_BIT"], timeout=10)
    if long_bit.ok:
        rows.append([long_bit.detail.strip() + " bit"])
    return ExecResult(True, "系统位数", _md_table(["架构/位数"], rows))


def sys_modules(**_):
    """查看内核加载的模块：lsmod，渲染成表格。"""
    r = _run(["lsmod"], timeout=15)
    if not r.ok:
        return r
    rows: list[list[str]] = []
    for line in r.detail.splitlines()[1:]:
        cols = line.split()
        if not cols:
            continue
        rows.append([cols[0], cols[1] if len(cols) > 1 else "-", " ".join(cols[2:]) if len(cols) > 2 else "-"])
    if not rows:
        return ExecResult(True, "未加载内核模块", r.detail, detail_format=r.detail_format)
    return ExecResult(True, f"已加载内核模块 {len(rows)} 个", _md_table(["模块", "大小", "使用方"], rows))


def sys_cpu(**_):
    """查看 CPU 信息：从 /proc/cpuinfo 提取型号和核心数。"""
    try:
        text = open("/proc/cpuinfo", encoding="utf-8").read().strip()
    except OSError as e:
        return ExecResult(False, f"读取 /proc/cpuinfo 失败: {e}")
    models: dict[str, int] = {}
    cores = 0
    for line in text.splitlines():
        lower = line.lower()
        if lower.startswith("processor"):
            cores += 1
        if lower.startswith("model name") or lower.startswith("hardware"):
            model = line.partition(":")[2].strip()
            if model:
                models[model] = models.get(model, 0) + 1
    rows: list[list[str]] = []
    if models:
        rows.append(["处理器", "、".join(f"{m} × {n}" if n > 1 else m for m, n in models.items())])
    rows.append(["核心数", str(cores)])
    return ExecResult(True, "CPU 信息", _md_table(["项目", "数值"], rows))


def sys_hw(**_):
    """显示当前硬件信息：lshw -short，渲染成表格。"""
    r = _run(["lshw", "-short"], timeout=60)
    if not r.ok:
        return r
    table = _lshw_short_table(r.detail)
    return ExecResult(True, "硬件信息", table)


def sys_mem_type(**_):
    """查看内存型号：优先 dmidecode，无 DMI 时回退 lshw -short。"""
    r = _run(["dmidecode", "-t", "memory"], timeout=30)
    if r.ok:
        rows: list[list[str]] = []
        block: dict[str, str] = {}
        for line in r.detail.splitlines():
            if line.startswith("Memory Device"):
                if block:
                    rows.append([block.get("locator", "-"), block.get("size", "-"), block.get("type", "-"), block.get("speed", "-"), block.get("manufacturer", "-"), block.get("part", "-")])
                block = {}
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key == "Locator":
                block["locator"] = value
            elif key == "Size":
                block["size"] = value
            elif key == "Type":
                block["type"] = value
            elif key == "Speed":
                block["speed"] = value
            elif key == "Manufacturer":
                block["manufacturer"] = value
            elif key == "Part Number":
                block["part"] = value
        if block:
            rows.append([block.get("locator", "-"), block.get("size", "-"), block.get("type", "-"), block.get("speed", "-"), block.get("manufacturer", "-"), block.get("part", "-")])
        if rows:
            return ExecResult(True, "内存型号信息", _md_table(["槽位", "大小", "类型", "速度", "厂商", "型号"], rows))
    r2 = _run(["lshw", "-short", "-class", "memory"], timeout=30)
    if r2.ok:
        return ExecResult(True, "内存设备", _lshw_short_table(r2.detail))
    return ExecResult(True, "未获取到内存型号信息", _md_table(["项目", "数值"], [["状态", (r.detail or r.message).strip() or "无 DMI/硬件信息"]]))


def sys_mem_size(**_):
    """显示当前内存大小：free -m 的 Mem 行，渲染成表格。"""
    r = _run(["free", "-m"], timeout=15)
    if not r.ok:
        return r
    rows: list[list[str]] = []
    for line in r.detail.splitlines():
        if not line.strip():
            continue
        cols = line.split()
        if not cols or cols[0] in {"total", "类型"}:
            continue
        row = [cols[0]]
        row += [cols[i] if i < len(cols) else "-" for i in range(1, 7)]
        rows.append(row)
    if not rows:
        return ExecResult(True, "未获取到内存信息", r.detail, detail_format=r.detail_format)
    return ExecResult(True, "内存使用情况", _md_table(["类型", "总量(MB)", "已用(MB)", "可用(MB)", "共享(MB)", "缓存(MB)", "可用缓存(MB)"], rows))


def sys_limits(**_):
    """查看系统限制：ulimit -a"""
    return _run(["ulimit", "-a"], timeout=10)


def sys_kernel_limits(**_):
    """查看内核限制：ipcs -l"""
    return _run(["ipcs", "-l"], timeout=15)


def _wayland_display_info():
    """Wayland 会话下的当前分辨率/刷新率表格；不适用时返回 None。

    复用 display_tools.current_display_info（同一套会话探测 + wlr-randr 解析）。
    只在「没有桌面会话」或「该设备没有 wlr-randr」时返回 None，让调用方回退 xrandr。
    """
    try:
        from bibit_intent.execution.display_tools import current_display_info
    except Exception:  # 包结构异常时不影响只读查询的兜底路径
        return None
    info = current_display_info()
    if info is None or (not info.ok and "命令不存在" in (info.message or "")):
        return None
    return info


def _no_display_reason(raw: str) -> str:
    """把 xrandr 的英文报错换成用户能看懂的说明。"""
    text = (raw or "").strip()
    low = text.lower()
    if not text:
        return "未检测到显示输出"
    if "can't open display" in low or "authorization required" in low or "no authorization" in low:
        return "未找到图形桌面会话（无 Wayland/X 显示），读不到屏幕分辨率"
    return text


def sys_resolution(**_):
    """查看当前屏幕分辨率：优先 Wayland（wlr-randr），无 Wayland 会话时回退 xrandr。

    Agent 进程环境里没有 DISPLAY，xrandr 必然报 Can't open display；桌面是
    labwc(Wayland) 时必须走 wlr-randr + 桌面会话环境。
    """
    info = _wayland_display_info()
    if info is not None:
        return info
    r = _run(["xrandr"], timeout=15)
    if not r.ok:
        return ExecResult(True, "屏幕分辨率", _md_table([["项目", "数值"]], [["状态", _no_display_reason(r.detail or r.message)]]))
    rows: list[list[str]] = []
    current = None
    modes: list[str] = []
    current_mode = "-"
    for line in r.detail.splitlines():
        head = re.match(r"^(\S+)\s+(connected|disconnected)", line)
        if head:
            if current is not None:
                rows.append([current, current_mode, "、".join(modes) if modes else "-"])
            current = head.group(1)
            modes = []
            current_mode = "未连接" if head.group(2) == "disconnected" else "-"
            continue
        if current is None:
            continue
        mode = re.match(r"^\s+(\S+)\s+\d", line)
        if mode:
            modes.append(mode.group(1))
            if "*" in line:
                current_mode = mode.group(1)
    if current is not None:
        rows.append([current, current_mode, "、".join(modes) if modes else "-"])
    if not rows:
        rows = [["未检测到显示器", "-", "-"]]
    return ExecResult(True, "屏幕分辨率", _md_table(["输出", "当前分辨率", "可用分辨率"], rows))


# ── 硬盘补充 ─────────────────────────────────────────────────────────

def disk_partitions(**_):
    """查看硬盘的分区：fdisk -l"""
    return _run(["fdisk", "-l"], timeout=30)


def disk_info(dev: str = "/dev/sda", **_) -> ExecResult:
    """查看硬盘信息：hdparm -i /dev/sda"""
    if not dev:
        dev = "/dev/sda"
    return _run(["hdparm", "-i", dev], timeout=30)


def disk_io(**_):
    """查看硬盘当前读写情况：iostat -x（需 sysstat）"""
    return _run(["iostat", "-x"], timeout=20)


def disk_raid(**_):
    """查看软 raid 阵列信息：cat /proc/mdstat"""
    try:
        text = Path("/proc/mdstat").read_text(encoding="utf-8")
    except OSError as e:
        return ExecResult(False, f"读取 /proc/mdstat 失败: {e}")
    return ExecResult(True, "ok", text or "(无 md 阵列)", detail_format="text")


# ── 进程补充 ─────────────────────────────────────────────────────────

def proc_mem(**_):
    """查看当前的内存使用情况：free"""
    return _run(["free", "-h"], timeout=15)


def proc_mem_stat(**_):
    """统计程序的内存耗用：ps -eo fname,rss 汇总排序"""
    r = _run(["ps", "-eo", "fname,rss"], timeout=15)
    if not r.ok:
        return r
    agg: dict[str, int] = {}
    for line in r.detail.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2:
            try:
                agg[parts[0]] = agg.get(parts[0], 0) + int(parts[1])
            except ValueError:
                pass
    rows = sorted(agg.items(), key=lambda kv: -kv[1])[:15]
    out = "\n".join(f"{name:<24} {rss} KB" for name, rss in rows)
    return ExecResult(True, "各程序内存耗用 TOP15", out, detail_format="text")


def proc_open_files(pid: int = 0, **_) -> ExecResult:
    """查看进程打开的文件：lsof -p pid"""
    if not pid:
        return ExecResult(False, "缺少进程号 pid")
    return _run(["lsof", "-p", str(pid)], timeout=20)


def proc_port(port: int = 0, **_) -> ExecResult:
    """显示端口运行的程序：lsof -i :port"""
    if not port:
        return ExecResult(False, "缺少端口号")
    return _run(["lsof", "-i", f":{port}"], timeout=20)


# ── 网络补充 ─────────────────────────────────────────────────────────

def net_route(**_):
    """查看路由信息：ip route（回退 netstat -rn）"""
    r = _run(["ip", "route"], timeout=15)
    if r.ok:
        return r
    return _run(["netstat", "-rn"], timeout=15)


def net_mac(**_):
    """查看网卡物理地址：/sys/class/net/*/address"""
    import glob
    lines = []
    for path in sorted(glob.glob("/sys/class/net/*/address")):
        iface = path.split("/")[-2]
        try:
            mac = open(path, encoding="utf-8").read().strip()
            lines.append(f"{iface}: {mac}")
        except OSError:
            continue
    if not lines:
        return ExecResult(False, "未找到网卡地址")
    return ExecResult(True, "网卡 MAC 地址", "\n".join(lines), detail_format="text")


def net_ext_ip(**_):
    """查看当前外网 IP：curl ifconfig.me"""
    return _run(["curl", "-s", "-m", "10", "ifconfig.me"], timeout=15)


def net_whois(domain: str = "", **_) -> ExecResult:
    """查看域名注册备案情况：whois domain"""
    if not domain:
        return ExecResult(False, "缺少域名")
    return _run(["whois", domain], timeout=30)


def net_trace(host: str = "", **_) -> ExecResult:
    """查看到某一域名的路由情况：tracepath host"""
    if not host:
        return ExecResult(False, "缺少域名或主机")
    command = ["tracepath", host]
    try:
        result = run_command(command, timeout=8)
    except FileNotFoundError:
        return ExecResult(False, "未安装 tracepath，无法执行路由跟踪。")
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        diagnostics = error.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(diagnostics, bytes):
            diagnostics = diagnostics.decode("utf-8", errors="replace")
        detail = "\n".join(part.strip() for part in (output, diagnostics) if part.strip())
        return ExecResult(
            False,
            "路由跟踪在 8 秒内未完成；以下仅为已取得的部分输出，不能据此判定网络故障。"
            if detail else "路由跟踪在 8 秒内未完成，未取得路径信息；不能据此判定网络故障。",
            detail,
            detail_format="text",
        )
    return ExecResult(
        result.returncode == 0,
        "路由跟踪结束，请根据输出判断路径是否到达目标。" if result.returncode == 0
        else f"路由跟踪失败（码 {result.returncode}），保留已取得的输出。",
        "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip()),
        detail_format="text",
    )


_CLOUD_PROBE_DEFAULT = "https://api.rvcompute.com:60000/v1"


def _cloud_probe_target() -> str | None:
    """第 3 跳（云端可达性）的探测目标 host[:port]。

    只读环境变量 CLOUD_BASE_URL，默认值与 config.AgentConfig.cloud_base_url、
    routing/intent.py 预检门一致；这里不 import config，避免测试环境缺
    pydantic_settings。返回 None 表示地址无法解析。
    """
    raw = (os.getenv("CLOUD_BASE_URL") or _CLOUD_PROBE_DEFAULT).strip()
    m = re.match(r"https?://([^/\s]+)", raw)
    return m.group(1) if m else None


def net_diagnose(**_) -> ExecResult:
    """本地网络诊断：3跳探测 + 本地自查，离线兜底能力。

    第1步：3跳连通性探测（逐跳短超时，任一失败即停）
      1. 网关可达（ping默认网关 ≤1s）
      2. 公网可达（ping 8.8.8.8 ≤1s）
      3. 云端服务可达（curl CLOUD_BASE_URL ≤2s，防止captive portal）

    第2步：本地自查（复用已有工具能力）
      - net_ip: 有无IP、是否169.254开头（DHCP失败）
      - net_route: 有无默认路由
      - eth_status/wifi_status: 有线/无线连没连
      - net_conn: 本机监听端口（可选）

    第3步：拼成人话返回探测结论+自查结果。

    返回约定：message = 一句话结论；detail = 逐条事实（不含结论，避免
    pipeline._format_reply 拼接 message+detail 时把结论打印两遍）。
    """
    lines = []
    # 第 3 跳的探测目标：与预检门/云端客户端同一地址（见 _cloud_probe_target）
    cloud_target = _cloud_probe_target()
    
    # ─── 第1步：3跳探测 ───
    # 跳1：获取默认网关并探测
    route_r = _run(["ip", "route"], timeout=3)
    gateway = None
    if route_r.ok:
        for line in route_r.detail.splitlines():
            m = re.search(r"default via ([\d.]+)", line)
            if m:
                gateway = m.group(1)
                break
    
    gateway_ok = False
    if gateway:
        gw_r = _run(["ping", "-c1", "-W1", gateway], timeout=2)
        gateway_ok = gw_r.ok
        if not gateway_ok:
            lines.append(f"❌ 网关不通（{gateway}）：本机或本地网络问题")
    else:
        lines.append("❌ 未找到默认网关：路由配置可能有问题")
    
    # 如果网关不通，后续公网/云端必然不通，直接跳到自查
    public_ok = False
    cloud_ok = False
    
    if gateway_ok:
        lines.append(f"✅ 网关可达（{gateway}）")
        
        # 跳2：公网探测
        pub_r = _run(["ping", "-c1", "-W1", "8.8.8.8"], timeout=2)
        public_ok = pub_r.ok
        if not public_ok:
            lines.append("❌ 公网不通（8.8.8.8）：网关之后的问题（路由器/运营商/上游）")
        else:
            lines.append("✅ 公网可达（8.8.8.8）")
            
            # 跳3：云端服务探测（必须真连云端地址，不能只看 ping/公网）
            if cloud_target:
                cloud_r = _run(["curl", "-sI", "-m2", f"https://{cloud_target}"], timeout=3)
                cloud_ok = cloud_r.ok
                if not cloud_ok:
                    # 只报事实：原因说明在 message 的总结句里，避免同一句话出现两次
                    lines.append(f"❌ 云端不可达（{cloud_target}）")
                else:
                    lines.append(f"✅ 云端可达（{cloud_target}）")
            else:
                lines.append("⚠️  云端地址无法解析（CLOUD_BASE_URL），跳过云端可达性探测")
    
    # ─── 第2步：本地自查 ───
    lines.append("")
    lines.append("【本地自查】")
    
    # 查IP
    facts, ip_err = _collect_ip_facts()
    if ip_err is not None:
        lines.append(f"⚠️  无法查询IP：{ip_err.message}")
    else:
        addresses = _facts_ipv4_addresses(facts)
        if any(addr.startswith("169.254.") for addr in addresses):
            lines.append("⚠️  网卡IP：169.254开头（DHCP失败，自动分配链路本地地址）")
        else:
            usable = [addr for addr in addresses if not addr.startswith("127.")]
            if usable:
                lines.append(f"✅ 网卡已获取IP：{usable[0]}")
            else:
                lines.append("❌ 网卡没有可用 IPv4 地址")
    
    # 查路由
    if route_r.ok:
        if gateway:
            lines.append(f"✅ 默认路由存在：指向 {gateway}")
        else:
            lines.append("❌ 没有默认路由")
    else:
        lines.append("⚠️  无法查询路由表")
    
    # 查有线/无线状态（从system_tools导入会循环依赖，直接用nmcli）
    eth_r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=3)
    if eth_r.ok:
        wifi_found = False
        eth_found = False
        for line in eth_r.detail.splitlines():
            parts = line.split(":")
            if len(parts) >= 3:
                dev, typ, state = parts[0], parts[1], parts[2]
                if typ not in ("wifi", "ethernet"):
                    continue
                label = "无线" if typ == "wifi" else "有线"
                if typ == "wifi":
                    wifi_found = True
                else:
                    eth_found = True
                if state == "connected":
                    lines.append(f"✅ {label}（{dev}）：已连接")
                elif state == "disconnected":
                    lines.append(f"❌ {label}（{dev}）：未连接（{state}）")
                else:
                    # unavailable / unmanaged：多为没插网线或未纳管，不算故障
                    lines.append(f"•  {label}（{dev}）：未启用（{state}）")
        
        if not wifi_found and not eth_found:
            lines.append("⚠️  未找到网络设备")
    
    # ─── 第3步：总结判断 ───
    if cloud_ok:
        summary = "✅ 网络正常：本机到云端全程可达"
    elif cloud_target is None and public_ok:
        summary = "⚠️  能上公网；云端地址未配置（CLOUD_BASE_URL），未确认云端可达"
    elif public_ok:
        summary = "⚠️  能上公网但连不到云端：可能是 DNS 解析、强制门户拦截，或云端服务不可用"
    elif gateway_ok:
        summary = "❌ 断在本地网络之外：网关可达但公网不通，检查路由器或运营商"
    elif gateway:
        summary = "❌ 断在本机到网关：本地网络问题，检查网线/WiFi连接"
    else:
        summary = "❌ 路由配置异常：没有默认网关"

    # 结论只放在 message；detail 只放逐条事实（上层 pipeline._format_reply 会
    # 把 message 与 detail 拼接，把结论也塞进 detail 会打印两遍）。
    if not cloud_ok:
        lines.append("")
        lines.append("💡 以上结论来自本机自查；云端恢复后可以再帮你做进一步分析。")

    return ExecResult(True, summary, "\n".join(lines))


def net_http_head(url: str = "", **_) -> ExecResult:
    """查看 HTTP 头：curl --head url"""
    if not url:
        return ExecResult(False, "缺少 URL")
    return _run(["curl", "-sI", "-m", "15", url], timeout=20)


# ── iptables（只读）──────────────────────────────────────────────────

def iptables_list(**_):
    """查看 iptables 规则：iptables -L -n + nat"""
    r = _run(["sudo", "-n", "/usr/sbin/iptables", "-L", "-n"], timeout=15)
    if not r.ok:
        return r
    nat = _run(["sudo", "-n", "/usr/sbin/iptables", "-t", "nat", "-L", "-n"], timeout=15)
    if not nat.ok:
        return ExecResult(False, "NAT 规则查询失败", r.detail + "\n" + nat.message + "\n" + nat.detail, detail_format="text")
    detail = r.detail + ("\n\n[ NAT 规则 ]\n" + nat.detail if nat.ok else "")
    return ExecResult(True, "iptables 规则", _code_block(detail, 3000))


# ── 安全（只读扫描）──────────────────────────────────────────────────

def sec_lastlog(**_):
    """查看所有帐号的登录情况：lastlog2"""
    return _run(["lastlog2"], timeout=15)


def sec_antivirus(path: str = ".", **_) -> ExecResult:
    """病毒扫描：clamscan -r path"""
    path = str(path or ".").strip()
    if path != "." and not path.startswith("/"):
        return ExecResult(False, "病毒扫描路径必须是绝对路径", "请提供以 / 开头的文件或目录路径")
    if "\x00" in path:
        return ExecResult(False, "病毒扫描路径无效", "路径不能包含 NUL 字符")
    if not os.path.exists(path):
        return ExecResult(False, "病毒扫描路径不存在", path)
    try:
        result = run_command(["clamscan", "--suppress-ok-results", "-r", path], timeout=600)
    except FileNotFoundError:
        return ExecResult(False, "未安装 ClamAV，请先安装 clamav 并更新病毒库")
    except subprocess.TimeoutExpired:
        return ExecResult(False, "病毒扫描超时，未完成全部扫描", "请缩小扫描范围后重试，不能据此判断目录安全。")
    detail = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode == 0:
        return ExecResult(True, "病毒扫描完成，未发现病毒", detail, detail_format="text")
    if result.returncode == 1:
        return ExecResult(True, "病毒扫描发现威胁；未删除或隔离任何文件", detail, detail_format="text")
    return ExecResult(False, "病毒扫描不完整，请检查扫描错误；不能判断目录安全", detail, detail_format="text")


# ── 服务列表 ─────────────────────────────────────────────────────────

def service_list(**_):
    """查看全部服务状态：service --status-all"""
    r = _run(["service", "--status-all"], timeout=30)
    if r.ok:
        return r
    return _run(["systemctl", "list-units", "--type=service", "--no-pager", "--plain"], timeout=20)


# ── 用户列表 ─────────────────────────────────────────────────────────

def user_list(**_):
    """查看系统用户：cat /etc/passwd"""
    try:
        text = open("/etc/passwd", encoding="utf-8").read().strip()
    except OSError as e:
        return ExecResult(False, f"读取 /etc/passwd 失败: {e}")
    users = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2] and 1000 <= int(parts[2]) <= 60000:
            users.append(f"{parts[0]}  uid={parts[2]}")
    return ExecResult(True, f"共 {len(users)} 个普通用户", "\n".join(users) or "(无)", detail_format="text")


# ── 文件信息/编码 ────────────────────────────────────────────────────

def file_info(path: str = "", **_) -> ExecResult:
    """察看文件类型：file path"""
    if not path:
        return ExecResult(False, "缺少文件路径")
    return _run(["file", path], timeout=15)


def text_encoding(path: str = "", **_) -> ExecResult:
    """察看文件编码：enca path，缺 enca 时回退 file"""
    if not path:
        return ExecResult(False, "缺少文件路径")
    r = _run(["enca", path], timeout=15)
    if r.ok:
        return r
    return _run(["file", path], timeout=15)


# ── 分发表 ───────────────────────────────────────────────────────────
HANDLERS = {
    "pkg_match_preview": pkg_match_preview,
    "software_delivery_assess": software_delivery_assess,
    "pkg_candidate": pkg_candidate,
    "pkg_operation_preview": pkg_operation_preview,
    "pkg_verify": pkg_verify,
    "pkg_stats": pkg_stats,
    "pkg_archives": pkg_archives,
    "pkg_selections": pkg_selections,
    "sys_arch": sys_arch,
    "sys_modules": sys_modules,
    "sys_cpu": sys_cpu,
    "sys_hw": sys_hw,
    "sys_mem_type": sys_mem_type,
    "sys_mem_size": sys_mem_size,
    "sys_limits": sys_limits,
    "sys_kernel_limits": sys_kernel_limits,
    "sys_resolution": sys_resolution,
    "disk_partitions": disk_partitions,
    "disk_info": disk_info,
    "disk_io": disk_io,
    "disk_raid": disk_raid,
    "proc_mem": proc_mem,
    "proc_mem_stat": proc_mem_stat,
    "proc_open_files": proc_open_files,
    "proc_port": proc_port,
    "net_route": net_route,
    "net_mac": net_mac,
    "net_ext_ip": net_ext_ip,
    "net_whois": net_whois,
    "net_trace": net_trace,
    "net_http_head": net_http_head,
    "net_diagnose": net_diagnose,
    "iptables_list": iptables_list,
    "sec_lastlog": sec_lastlog,
    "sec_antivirus": sec_antivirus,
    "service_list": service_list,
    "user_list": user_list,
    "file_info": file_info,
    "text_encoding": text_encoding,
    "pkg_files": pkg_files,
    "pkg_search": pkg_search,
    "pkg_info": pkg_info,
    "pkg_installed": pkg_installed,
    "pkg_owns_file": pkg_owns_file,
    "pkg_depends": pkg_depends,
    "pkg_rdepends": pkg_rdepends,
    "sys_kernel": sys_kernel,
    "sys_version": sys_version,
    "sys_overview": sys_overview,
    "sys_pci": sys_pci,
    "usb_list": usb_list,
    "usb_tree": usb_tree,
    "usb_detail": usb_detail,
    "usb_reset": usb_reset,
    "sys_uptime": sys_uptime,
    "disk_usage": disk_usage,
    "disk_dir_usage": disk_dir_usage,
    "disk_lsblk": disk_lsblk,
    "proc_list": proc_list,
    "proc_top_mem": proc_top_mem,
    "proc_top_cpu": proc_top_cpu,
    "proc_tree": proc_tree,
    "net_ip": net_ip,
    "net_conn": net_conn,
    "system_update_status": system_update_status,
    "service_status": service_status,
    "login_records": login_records,
    "online_users": online_users,
}
