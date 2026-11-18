"""
tools/system.py — 系统控制工具的实际执行实现
每个函数对应 tools.yaml 里一个工具的 system 段。

⚠️ 安全（对照 design.md §12 + security-notes.md）：
  - 用参数化 subprocess（列表形式），绝不 shell 字符串拼接，防命令注入
  - 危险操作（reboot）由执行层强制二次确认，此处只提供能力
  - 设备具体 sysfs/ALSA 路径可能不同，未探测到时返回明确错误而非静默失败
"""
from __future__ import annotations

import configparser
import difflib
import glob
import json
import os
import pwd
import shlex
import shutil
import signal
import subprocess
import sys
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime as system_datetime
from pathlib import Path
from typing import Literal

from .cancellation import raise_if_cancelled, remaining_budget, run_command
from .md_format import md_table as _md_table
from bibit_intent.core.system_time import normalize_system_datetime, resolve_system_datetime
from bibit_intent.core.desktop_locations import (
    DESKTOP_LOCATIONS,
    resolve_desktop_shortcut,
    resolve_directory,
)
from bibit_intent.core.appcli_execution import is_sandboxed


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    # 缺参澄清：非 None 表示工具还差必填参数（message 为引导文案），
    # 由上层提示用户补充，而不是当作普通失败。
    need_param: str | None = None
    # 需要转云端：非 None 表示该本地工具结果不足以作答，应升级云端。
    escalate: str | None = None
    # 是否已经真正发起过启动命令（区别于"命令/应用根本没找到"）。launch_app
    # 的逐级回退据此判断：只有没发起过启动才继续尝试下一种解析方式，避免把
    # "启动了但进程没起来"的明确失败改写成误导性的 app_not_found。
    attempted: bool = False
    detail_format: Literal["text", "markdown", "internal"] = "markdown"
    # 该挂起不是缺参回填，而是用户确认后执行（如允许降级）。
    confirm: bool = False
    # 供用户「N 选一」的候选值列表（如定位到多个同名目录时让用户选序号）。
    choices: list[str] | None = None
    confirmation: dict | None = None
    unverified: bool = False


def _reply(zh: str, en: str, language: str | None = None) -> str:
    """Return a localized tool reply when a caller supplies ``language``.

    Runtime normally localizes the final pipeline response; accepting the
    language here also keeps direct MCP/Executor calls from leaking Chinese.
    """
    return en if str(language or "").lower().startswith("en") else zh


def _settings_host_guard(language: str | None = None) -> ExecResult | None:
    if is_sandboxed():
        return ExecResult(False, _reply(
            "系统时间、语言和区域必须在宿主机全局执行，请转交宿主通道。",
            "System time, language and region require the host execution channel.", language),
            escalate="needs_host_channel")
    return None


def get_system_time(language: str | None = None, **_) -> ExecResult:
    blocked = _settings_host_guard(language)
    if blocked is not None:
        return blocked
    current = system_datetime.now().astimezone()
    offset = current.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    zone = current.tzname() or f"UTC{offset}"
    stamp = current.strftime("%Y-%m-%d %H:%M:%S")
    return ExecResult(True, _reply(
        f"当前系统时间：{stamp}，时区：{zone}（UTC{offset}）。",
        f"Current system time: {stamp}, timezone: {zone} (UTC{offset}).", language),
        _reply("读取设备本地时钟，未验证网络校时状态。",
               "Read from the device clock; network synchronization has not been verified.", language),
        detail_format="markdown")


def set_system_time(datetime: str, language: str | None = None, **_) -> ExecResult:
    blocked = _settings_host_guard(language)
    if blocked is not None:
        return blocked
    target = normalize_system_datetime(datetime)
    if target is None:
        return ExecResult(False, _reply(
            "请提供有效的完整日期时间、日期（YYYY-MM-DD）或时间（HH:MM[:SS]）。",
            "Provide a valid date and time, date (YYYY-MM-DD), or time (HH:MM[:SS]).", language))
    from .time_confirmation import prepare_time_change

    return prepare_time_change(target, language)


def _write_system_time(target: str, language: str | None = None) -> ExecResult:
    if len(target) in (8, 10):
        target = resolve_system_datetime(target, system_datetime.now().astimezone())
    result = _run(["timedatectl", "--no-ask-password", "set-time", target])
    if not result.ok:
        return ExecResult(False, _reply(
            "系统时间未设置成功，请检查时间同步（NTP）状态及系统时间修改权限。",
            "Could not set system time. Check automatic time synchronization (NTP) and permissions.", language),
            result.detail or result.message, detail_format="text")
    return ExecResult(True, _reply(
        f"已将系统时间设置为 {target}（系统当前时区）。",
        f"System time set to {target} (current system timezone).", language))


def _run(cmd: list[str], timeout: int = 10, env: dict | None = None) -> ExecResult:
    """参数化执行，cmd 必须是列表，杜绝 shell 注入。env=None 继承当前环境。"""
    try:
        p = run_command(cmd, timeout=timeout, env=env)
        if p.returncode == 0:
            return ExecResult(True, "ok", p.stdout, detail_format="text")
        return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")
    except FileNotFoundError:
        return ExecResult(False, f"命令不存在：{cmd[0]}")
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")


# ── 音量 ─────────────────────────────────────────────────────────────

def _find_pipewire_runtime_dir() -> str | None:
    """
    探测 PipeWire 会话目录（含 pipewire-0 socket 的 /run/user/*）。
    ALSA 默认 ctl 走 `type pipewire`（99-pipewire-default.conf），虚拟
    Master 控件只有在对应用户会话的 XDG_RUNTIME_DIR 下才可达；Agent 常以
    root/systemd 运行，默认目录没有该 socket，amixer 会报
    'Mixer attach default error: Host is down'。优先用当前 XDG_RUNTIME_DIR，
    没有就找任意含 pipewire-0 的 /run/user/*。
    """
    def _has(d: str | None) -> bool:
        return bool(d) and os.path.exists(os.path.join(d, "pipewire-0"))
    cur = os.environ.get("XDG_RUNTIME_DIR")
    if _has(cur):
        return cur
    for d in sorted(glob.glob("/run/user/*")):
        if _has(d):
            return d
    return None


def _amixer(args: list[str], timeout: int = 10) -> ExecResult:
    """amixer 专用执行：默认失败且是 PipeWire 连不上时，带上探测到的
    XDG_RUNTIME_DIR 重试一次；仍失败则返回原始错误。"""
    r = _run(["amixer"] + args, timeout=timeout)
    if r.ok or "Host is down" not in r.detail:
        return r
    rt = _find_pipewire_runtime_dir()
    if not rt:
        return r
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = rt
    r2 = _run(["amixer"] + args, timeout=timeout, env=env)
    return r2 if r2.ok else r


def _master_volume_level() -> int | None:
    current = _amixer(["get", "Master"])
    match = re.search(r"\[(\d{1,3})%\]", current.detail)
    if not match:
        return None
    return int(match.group(1))


def get_volume(language: str | None = None, **_) -> ExecResult:
    current_level = _master_volume_level()
    if current_level is None:
        return ExecResult(False, "无法读取当前音量")
    return ExecResult(
        True,
        _reply(f"当前音量是 {current_level}%", f"Current volume is {current_level}%", language),
    )


def set_volume(level: int = 50, language: str | None = None, **_) -> ExecResult:
    level = max(0, min(100, int(level)))
    r = _amixer(["sset", "Master", f"{level}%"])
    if r.ok:
        return ExecResult(True, _reply(f"音量已设为 {level}%", f"Volume set to {level}%", language))
    return r


def adjust_volume(direction: str = "up", amount: int = 10, language: str | None = None, **_) -> ExecResult:
    """按相对方向调节主音量。direction: up/down，amount: 1-100。"""
    if direction not in {"up", "down"}:
        return ExecResult(False, "音量调节方向仅支持 up 或 down")
    current_level = _master_volume_level()
    if current_level is None:
        return ExecResult(False, "无法读取当前音量")
    amount = max(1, min(100, int(amount)))
    delta = amount if direction == "up" else -amount
    target = max(0, min(100, current_level + delta))
    if target == current_level:
        edge = "最大" if direction == "up" else "最小"
        return ExecResult(True, f"音量已达到{edge}，未执行调整")
    r = _amixer(["sset", "Master", f"{target}%"])
    if r.ok:
        change = target - current_level
        return ExecResult(True, f"音量已{('调高' if change > 0 else '调低')} {abs(change)}%，当前为 {target}%")
    return r


def mute(on: bool = True, language: str | None = None, **_) -> ExecResult:
    state = "mute" if on else "unmute"
    r = _amixer(["sset", "Master", state])
    if r.ok:
        return ExecResult(True, _reply("已静音" if on else "已取消静音", "Muted" if on else "Unmuted", language))
    return r


# ── PipeWire 音频控制 ─────────────────────────────────────────────────

def _wpctl(args: list[str], timeout: int = 10) -> ExecResult:
    """wpctl 专用执行：自动探测 XDG_RUNTIME_DIR 并注入环境。"""
    rt = _find_pipewire_runtime_dir()
    if not rt:
        return ExecResult(False, "未找到 PipeWire 会话", "无法定位 XDG_RUNTIME_DIR")
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = rt
    return _run(["wpctl"] + args, timeout=timeout, env=env)


def _parse_wpctl_status() -> dict[str, list[dict]] | None:
    """解析 wpctl status 输出，返回 {sinks: [...], sources: [...]}。
    每个设备 {id, name, vol, muted, default}。失败返回 None。
    """
    r = _wpctl(["status"])
    if not r.ok:
        return None
    lines = r.detail.splitlines()
    sinks: list[dict] = []
    sources: list[dict] = []
    section = None
    for line in lines:
        # 保留原始缩进检查section，但解析设备行时去除空格
        if "├─ Sinks:" in line or "└─ Sinks:" in line:
            section = "sinks"
            continue
        if "├─ Sources:" in line or "└─ Sources:" in line:
            section = "sources"
            continue
        if "├─ Filters:" in line or "└─ Filters:" in line or "└─ Streams:" in line:
            section = None
            continue
        if not section:
            continue
        # 设备行特征：以 │ 开头，后跟可选 * 和数字
        if "│" not in line:
            continue
        # 去掉前缀符号，提取设备信息
        device_line = line.split("│", 1)[-1].strip()
        if not device_line:
            continue
        is_default = device_line.startswith("*")
        device_line = device_line.lstrip("* ").strip()
        if not device_line or not device_line[0].isdigit():
            continue
        parts = device_line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        try:
            dev_id = int(parts[0].rstrip("."))
        except ValueError:
            continue
        rest = parts[1]
        # "Built-in Audio 立体声  [vol: 0.60]" 或 "[vol: 0.50 MUTED]"
        name = rest.split("[")[0].strip() if "[" in rest else rest
        vol = muted = None
        if "[vol:" in rest:
            vol_part = rest.split("[vol:")[1].split("]")[0].strip()
            muted = "MUTED" in vol_part
            vol_str = vol_part.replace("MUTED", "").strip()
            try:
                vol = float(vol_str)
            except ValueError:
                pass
        entry = {"id": dev_id, "name": name, "vol": vol, "muted": muted, "default": is_default}
        if section == "sinks":
            sinks.append(entry)
        elif section == "sources":
            sources.append(entry)
    return {"sinks": sinks, "sources": sources} if (sinks or sources) else None


def _canonical_output_device(device: str) -> str:
    """把 codec/接口别名归一成 audio_output_switch 的 canonical device。"""
    normalized = str(device or "auto").strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "snd-es8326": "analog",
        "snd-8326": "analog",
        "sndes8326": "analog",
        "snd8326": "analog",
        "es8326": "analog",
        "es-8326": "analog",
        "8326": "analog",
        "speakers": "speaker",
        "headphones": "headphone",
        "displayport": "dp",
        "hdmi-output": "hdmi",
    }
    return aliases.get(normalized, normalized)


def audio_output_switch(device: str = "auto", **_) -> ExecResult:
    """切换音频输出设备。device: speaker/headphone/hdmi/dp/analog/auto。"""
    device = _canonical_output_device(device)
    status = _parse_wpctl_status()
    if not status or not status["sinks"]:
        return ExecResult(False, "未找到音频输出设备")

    # 简化映射：根据设备名包含的关键词匹配
    keywords = {
        "speaker": ["es8326", "analog", "立体声"],
        "analog": ["es8326", "analog", "立体声"],
        "headphone": ["headphone", "耳机"],
        "hdmi": ["hdmi"],
        "dp": ["dp"],
    }

    target_id = None
    if device == "auto":
        # 自动切换：非当前默认的第一个
        default_id = next((s["id"] for s in status["sinks"] if s["default"]), None)
        for sink in status["sinks"]:
            if sink["id"] != default_id:
                target_id = sink["id"]
                break
        if target_id is None and status["sinks"]:
            target_id = status["sinks"][0]["id"]
    else:
        # 按设备类型匹配
        kws = keywords.get(device, [])
        for sink in status["sinks"]:
            if any(kw in sink["name"].lower() for kw in kws):
                target_id = sink["id"]
                break

    if target_id is None:
        return ExecResult(False, f"未找到匹配的输出设备: {device}")

    r = _wpctl(["set-default", str(target_id)])
    if r.ok:
        return ExecResult(True, f"已切换输出设备到 ID {target_id}")
    return r


def audio_input_volume(level: int = 80, direction: str = "", amount: int = 10, **_) -> ExecResult:
    """调节输入音量（麦克风）。"""
    if direction:
        current = _wpctl(["get-volume", "@DEFAULT_AUDIO_SOURCE@"])
        match = re.search(r"Volume:\s*(0(?:\.\d+)?|1(?:\.0+)?)", current.detail)
        if not match:
            return ExecResult(False, "无法读取麦克风当前音量", current.detail, detail_format=current.detail_format)
        current_level = round(float(match.group(1)) * 100)
        amount = max(1, min(100, int(amount)))
        target = max(0, min(100, current_level + (amount if direction == "up" else -amount)))
        if target == current_level:
            return ExecResult(True, f"麦克风音量已达到{'最大' if direction == 'up' else '最小'}，未执行调整")
        r = _wpctl(["set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{target / 100:.2f}"])
        if r.ok:
            return ExecResult(True, f"麦克风音量已{'调高' if target > current_level else '调低'} {abs(target-current_level)}%，当前为 {target}%")
        return r
    level = max(0, min(100, int(level)))
    r = _wpctl(["set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{level / 100:.2f}"])
    if r.ok:
        return ExecResult(True, f"输入音量已设为 {level}%")
    return r


def audio_input_switch(device: str = "auto", **_) -> ExecResult:
    """切换音频输入设备。device: builtin/headset/auto。"""
    status = _parse_wpctl_status()
    if not status or not status["sources"]:
        return ExecResult(False, "未找到音频输入设备")

    keywords = {
        "builtin": ["es8326", "analog", "立体声", "built-in"],
        "headset": ["headset", "耳机", "external"],
    }

    target_id = None
    if device == "auto":
        default_id = next((s["id"] for s in status["sources"] if s["default"]), None)
        for source in status["sources"]:
            if source["id"] != default_id:
                target_id = source["id"]
                break
        if target_id is None and status["sources"]:
            target_id = status["sources"][0]["id"]
    else:
        kws = keywords.get(device, [])
        for source in status["sources"]:
            if any(kw in source["name"].lower() for kw in kws):
                target_id = source["id"]
                break

    if target_id is None:
        return ExecResult(False, f"未找到匹配的输入设备: {device}")

    r = _wpctl(["set-default", str(target_id)])
    if r.ok:
        return ExecResult(True, f"已切换输入设备到 ID {target_id}")
    return r


def audio_input_mute(on: bool = True, **_) -> ExecResult:
    """输入设备静音/取消静音。"""
    state = "1" if on else "0"
    r = _wpctl(["set-mute", "@DEFAULT_AUDIO_SOURCE@", state])
    if r.ok:
        return ExecResult(True, "麦克风已静音" if on else "麦克风已取消静音")
    return r


# ── 亮度 ─────────────────────────────────────────────────────────────

def _backlight_state() -> tuple[Path, int, int] | None:
    candidates = glob.glob("/sys/class/backlight/*/brightness")
    if not candidates:
        return None
    bl_dir = Path(candidates[0]).parent
    try:
        max_val = int((bl_dir / "max_brightness").read_text().strip())
        current_val = int((bl_dir / "brightness").read_text().strip())
    except (OSError, ValueError):
        return None
    current_level = round(current_val / max_val * 100)
    return bl_dir, max_val, current_level


def _write_backlight_level(bl_dir: Path, max_val: int, level: int) -> ExecResult | None:
    target = round(level / 100 * max_val)
    try:
        (bl_dir / "brightness").write_text(str(target))
    except PermissionError:
        return ExecResult(False, "无权限写背光", f"需 root 或 udev 规则：{bl_dir}")
    except (OSError, ValueError) as e:
        return ExecResult(False, "亮度设置失败", str(e))
    return None


def _backlight_is_powered_off(bl_dir: Path) -> bool:
    """K3 开发板可能注册了 pwm-backlight 但没有物理背光。

    这类设备 sysfs 背光目录存在，bl_power 为 4（FB_BLANK_POWERDOWN），
    实际写入 brightness 不会改变屏幕亮度；应提示硬件不支持，而不是报
    无权限写背光，误导用户以为提权后可用。
    """
    try:
        return (bl_dir / "bl_power").read_text().strip() == "4"
    except OSError:
        return False


def get_brightness(language: str | None = None, **_) -> ExecResult:
    state = _backlight_state()
    if state is None:
        return ExecResult(
            False,
            _reply(
                "未找到背光设备，无法读取亮度",
                "No backlight device found; cannot read brightness",
                language,
            ),
            "预期路径 /sys/class/backlight/*/brightness 不存在",
        )
    bl_dir, _max_val, current_level = state
    if _backlight_is_powered_off(bl_dir):
        return ExecResult(
            False,
            _reply(
                "当前设备没有可用的背光，无法读取亮度",
                "This device has no usable backlight; cannot read brightness",
                language,
            ),
            f"bl_power=4: {bl_dir}",
        )
    return ExecResult(
        True,
        _reply(f"当前亮度是 {current_level}%", f"Current brightness is {current_level}%", language),
    )


def set_brightness(level: int = 80, language: str | None = None, **_) -> ExecResult:
    level = max(0, min(100, int(level)))
    state = _backlight_state()
    if state is None:
        return ExecResult(
            False,
            "未找到背光设备",
            "预期路径 /sys/class/backlight/*/brightness 不存在，"
            "此设备可能不支持软件调节亮度（待硬件确认）",
        )
    bl_dir, max_val, _ = state
    if _backlight_is_powered_off(bl_dir):
        return ExecResult(
            False,
            _reply(
                "当前设备没有可用的背光，不支持调节亮度",
                "This device has no usable backlight and cannot adjust brightness",
                language,
            ),
            f"bl_power=4: {bl_dir}",
        )
    error = _write_backlight_level(bl_dir, max_val, level)
    if error is not None:
        return error
    return ExecResult(True, _reply(f"亮度已设为 {level}%", f"Brightness set to {level}%", language))


def adjust_brightness(direction: str = "up", amount: int = 10, language: str | None = None, **_) -> ExecResult:
    if direction not in {"up", "down"}:
        return ExecResult(False, "亮度调节方向仅支持 up 或 down")
    state = _backlight_state()
    if state is None:
        return ExecResult(
            False,
            "未找到背光设备",
            "预期路径 /sys/class/backlight/*/brightness 不存在，"
            "此设备可能不支持软件调节亮度（待硬件确认）",
        )
    bl_dir, max_val, current_level = state
    if _backlight_is_powered_off(bl_dir):
        return ExecResult(
            False,
            _reply(
                "当前设备没有可用的背光，不支持调节亮度",
                "This device has no usable backlight and cannot adjust brightness",
                language,
            ),
            f"bl_power=4: {bl_dir}",
        )
    amount = max(1, min(100, int(amount)))
    delta = amount if direction == "up" else -amount
    target_level = max(0, min(100, current_level + delta))
    if target_level == current_level:
        edge = "最大" if direction == "up" else "最小"
        return ExecResult(True, f"亮度已达到{edge}，未执行调整")
    error = _write_backlight_level(bl_dir, max_val, target_level)
    if error is not None:
        return error
    change = target_level - current_level
    return ExecResult(True, f"亮度已{('调高' if change > 0 else '调低')} {abs(change)}%，当前为 {target_level}%")


# ── 系统信息 ─────────────────────────────────────────────────────────

def _meminfo_kb() -> dict[str, int]:
    info: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        parts = value.strip().split()
        if parts:
            try:
                info[key] = int(parts[0])
            except ValueError:
                pass
    return info


def _format_cpu_markdown(language: str | None = None) -> str:
    rows: list[list[str]] = []
    try:
        lines = Path("/proc/cpuinfo").read_text().splitlines()
        models: dict[str, int] = {}
        for line in lines:
            lower = line.lower()
            if lower.startswith("model name") or lower.startswith("hardware"):
                model = line.partition(":")[2].strip()
                if model:
                    models[model] = models.get(model, 0) + 1
        cores = max(1, sum(1 for line in lines if line.lower().startswith("processor")))
        model_label = _reply("处理器", "Processor", language)
        if models:
            count_sep = _reply(" × ", " x ", language)
            model_sep = _reply("、", ", ", language)
            model_value = model_sep.join(
                f"{model}{count_sep}{count}" if count > 1 else model
                for model, count in models.items()
            )
            rows.append([model_label, model_value])
        rows.append([_reply("核心数", "Cores", language), str(cores)])
    except OSError:
        pass
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        rows.append([_reply("负载(1/5/15分钟)", "Load (1/5/15 min)", language), " ".join(load)])
    except OSError:
        pass
    if not rows:
        return _reply("## CPU 信息\n\n未获取到 CPU 信息", "## CPU\n\nNo CPU information available", language)
    return _reply("## CPU 信息", "## CPU", language) + "\n\n" + _md_table(
        [_reply("项目", "Item", language), _reply("数值", "Value", language)], rows
    )


def _format_memory_markdown(language: str | None = None) -> str:
    def gib(kb: int) -> str:
        return f"{kb / (1024 * 1024):.2f} GiB"

    try:
        info = _meminfo_kb()
        total_kb = info.get("MemTotal", 0)
        available_kb = info.get("MemAvailable", 0)
        used_kb = max(0, total_kb - available_kb)
        percent = (used_kb / total_kb * 100) if total_kb else 0.0
        buffers_kb = info.get("Buffers", 0)
        cached_kb = info.get("Cached", 0)
        swap_total_kb = info.get("SwapTotal", 0)
        swap_free_kb = info.get("SwapFree", 0)
        rows = [
            [_reply("总内存", "Total memory", language), gib(total_kb)],
            [_reply("已使用", "Used", language), gib(used_kb)],
            [_reply("可用", "Available", language), gib(available_kb)],
            [_reply("使用率", "Usage", language), f"{percent:.1f}%"],
            [_reply("Buffers/Cache", "Buffers/Cache", language), f"{gib(buffers_kb)} / {gib(cached_kb)}"],
            [_reply("Swap", "Swap", language), f"{gib(swap_total_kb - swap_free_kb)} / {gib(swap_total_kb)}"],
        ]
    except (OSError, ValueError):
        return _reply("## 内存信息\n\n未获取到内存信息", "## Memory\n\nNo memory information available", language)
    return _reply("## 内存信息", "## Memory", language) + "\n\n" + _md_table(
        [_reply("项目", "Item", language), _reply("数值", "Value", language)], rows
    )


def _format_temp_markdown(language: str | None = None) -> str:
    rows: list[list[str]] = []
    for tz in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            value = int(Path(tz).read_text().strip()) / 1000
            zone = Path(tz).parent.name
            type_path = Path(tz).parent / "type"
            sensor_type = type_path.read_text().strip() if type_path.exists() else ""
            label = f"{zone} ({sensor_type})" if sensor_type else zone
            rows.append([label, f"{value:.1f}°C"])
        except (OSError, ValueError):
            continue
    if not rows:
        return _reply("## 温度信息\n\n未获取到温度信息", "## Temperature\n\nNo temperature information available", language)
    return _reply("## 温度信息", "## Temperature", language) + "\n\n" + _md_table(
        [_reply("传感器", "Sensor", language), _reply("温度", "Temperature", language)], rows
    )


def _format_disk_markdown(language: str | None = None) -> str:
    r = _run(["df", "-h", "/"])
    if not r.ok:
        return _reply("## 磁盘信息\n\n未获取到磁盘信息", "## Disk\n\nNo disk information available", language)
    lines = r.detail.splitlines()
    if len(lines) < 2:
        return _reply("## 磁盘信息\n\n未获取到磁盘信息", "## Disk\n\nNo disk information available", language)
    cols = lines[1].split()
    if len(cols) < 6:
        cols = (cols + ["-"] * 6)[:6]
    return _reply("## 磁盘信息", "## Disk", language) + "\n\n" + _md_table(
        [_reply("挂载点", "Mount", language), _reply("容量", "Size", language),
         _reply("已用", "Used", language), _reply("可用", "Available", language),
         _reply("使用率", "Usage", language)],
        [[cols[5], cols[1], cols[2], cols[3], cols[4]]],
    )


def get_system_info(item: str = "all", language: str | None = None, **_) -> ExecResult:
    sections: list[str] = []
    messages: list[str] = []
    if item in ("cpu", "cpu_temp", "all"):
        sections.append(_format_cpu_markdown(language))
        messages.append(_reply("已获取 CPU 信息", "CPU information retrieved", language))
    if item in ("memory", "all"):
        sections.append(_format_memory_markdown(language))
        messages.append(_reply("已获取内存信息", "Memory information retrieved", language))
    if item in ("temp", "cpu_temp", "all"):
        sections.append(_format_temp_markdown(language))
        messages.append(_reply("已获取温度信息", "Temperature information retrieved", language))
    if item in ("disk", "all"):
        sections.append(_format_disk_markdown(language))
        messages.append(_reply("已获取磁盘信息", "Disk information retrieved", language))

    if not sections:
        return ExecResult(False, f"无法获取系统信息(item={item})")

    prefix = _reply("## 系统信息", "## System information", language) if item == "all" else ""
    if prefix:
        sections.insert(0, prefix)
    if item == "all":
        message = _reply("已获取系统信息", "System information retrieved", language)
    elif item == "cpu_temp":
        message = _reply("已获取 CPU 与温度信息", "CPU and temperature information retrieved", language)
    else:
        message = messages[0]
    return ExecResult(True, message, "\n\n".join(sections))


# ── 应用控制 ─────────────────────────────────────────────────────────

_APP_ALIASES_CACHE: dict[str, str] | None = None


def _app_aliases() -> dict[str, str]:
    """Load app_control aliases shared by launch_app/close_app from tools.yaml."""
    global _APP_ALIASES_CACHE
    if _APP_ALIASES_CACHE is None:
        try:
            from bibit_intent.core.yaml_utils import safe_load
            from bibit_intent.resources import resource_path
            data = safe_load(resource_path("tools.yaml").read_text(encoding="utf-8")) or {}
            tools = data.get("tools", {})
            merged: dict[str, str] = {}
            for tool_name in ("launch_app", "close_app"):
                section = (tools.get(tool_name) or {}).get("system", {}) or {}
                aliases = section.get("aliases", {}) or {}
                merged.update(
                    {str(k).casefold(): str(v) for k, v in aliases.items()}
                )
            _APP_ALIASES_CACHE = merged
        except (OSError, TypeError, ValueError, ImportError):
            _APP_ALIASES_CACHE = {}
    return _APP_ALIASES_CACHE


def _app_command(name: str) -> list[str]:
    """Return explicit argv for aliases; qterminal must be a normal window."""
    if name == "qterminal":
        return ["qterminal"]
    return [name]


# ── .desktop 应用解析 ──────────────────────────────────────────────
# 动态扫描 XDG 应用目录，按 Name / Name[zh_CN] / 文件名匹配用户输入，
# 再用 Exec 字段启动。系统装了什么应用就认识什么应用，不用在代码里
# 维护一份易过时的白名单。

_DESKTOP_FIELD_CODES = frozenset("fFuUickdDnNvme")


def _desktop_search_dirs() -> list[str]:
    """返回应扫描的 applications 目录（去重、只保留存在的目录）。"""
    dirs: list[str] = []
    xdg = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    for d in xdg:
        if d:
            dirs.append(os.path.join(d, "applications"))
    dirs.append(os.path.expanduser("~/.local/share/applications"))
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def _parse_desktop_file(path: str) -> dict | None:
    """解析单个 .desktop 文件，不可启动或无 Exec 时返回 None。"""
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.optionxform = str  # 保留 Name[zh_CN] 的大小写
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg.read_file(fh)
    except (OSError, configparser.Error):
        return None

    section = "Desktop Entry"
    if not cfg.has_section(section):
        return None
    sec = cfg[section]
    for key in ("NoDisplay", "Hidden"):
        if sec.get(key, "").strip().lower() == "true":
            return None
    exec_line = sec.get("Exec", "").strip()
    if not exec_line:
        return None

    names: list[str] = []
    for key in ("Name", "Name[zh_CN]"):
        value = sec.get(key, "").strip()
        if value and value not in names:
            names.append(value)
    if not names:
        return None

    desktop_id = os.path.basename(path)
    stem = desktop_id[:-len(".desktop")] if desktop_id.endswith(".desktop") else desktop_id
    return {
        "id": desktop_id, "stem": stem, "names": names, "exec": exec_line,
        "terminal": sec.get("Terminal", "").strip().lower() == "true",
    }


_DESKTOP_APPS_CACHE: list[dict] | None = None
_DESKTOP_APPS_SIGNATURE: tuple[tuple[str, float], ...] | None = None


def _desktop_dirs_signature() -> tuple[tuple[str, float], ...]:
    """applications 目录的 (路径, mtime) 快照：新装/卸载应用会改目录 mtime。"""
    sig: list[tuple[str, float]] = []
    for d in _desktop_search_dirs():
        try:
            sig.append((d, os.stat(d).st_mtime))
        except OSError:
            continue
    return tuple(sig)


def _get_desktop_apps(force_reload: bool = False) -> list[dict]:
    """扫描并缓存可启动的 .desktop 应用；applications 目录 mtime 变化时自动重扫。

    边侧服务和 MCP server 都是长驻进程，“安装 X → 打开 X”必须在同一进程
    内可用，不能等重启。
    """
    global _DESKTOP_APPS_CACHE, _DESKTOP_APPS_SIGNATURE
    signature = _desktop_dirs_signature()
    if _DESKTOP_APPS_CACHE is None or force_reload or signature != _DESKTOP_APPS_SIGNATURE:
        apps: list[dict] = []
        for d in _desktop_search_dirs():
            for path in sorted(glob.glob(os.path.join(d, "*.desktop"))):
                info = _parse_desktop_file(path)
                if info:
                    apps.append(info)
        _DESKTOP_APPS_CACHE = apps
        _DESKTOP_APPS_SIGNATURE = signature
    return _DESKTOP_APPS_CACHE


def _match_desktop_app(query: str, apps: list[dict]) -> dict | None:
    """按优先级匹配应用：精确 Name → 文件名 → Name 包含查询词。

    包含（contains）只是兜底，要求查询词至少 2 个字符，并取命中的**最短**
    名称。否则极短或泛化的输入会命中第一个恰好包含该字的应用：实测
    "打开 e" 会启动 Chromium、"关闭 器" 会把计算器杀掉。精确命中不受限制
    （单个字符的正式应用名如 "X" 仍可解析）。
    """
    raw = query.strip()
    if not raw:
        return None

    def _lookup_keys() -> list[str]:
        keys = [raw.casefold()]
        # "LibreOffice Writer应用" 这类口语后缀：先按原样精确匹配，未命中
        # 再用去掉尾缀“应用”的名称兜底。不做全局字符串替换，避免把
        # “卡死的应用”误抽成“卡死的”。
        stripped = re.sub(r"应用$", "", raw).strip()
        if stripped and stripped.casefold() not in keys:
            keys.append(stripped.casefold())
        return keys

    exact = stem_match = None
    contains = None
    contains_len = 0
    for q in _lookup_keys():
        for app in apps:
            if exact is None:
                for name in app["names"]:
                    if name.casefold() == q:
                        exact = app
                        break
            if app["stem"].casefold() == q:
                if stem_match is None:
                    stem_match = app
                continue
            if len(q) >= 2:
                for name in app["names"]:
                    folded = name.casefold()
                    if q in folded and (contains is None or len(folded) < contains_len):
                        contains = app
                        contains_len = len(folded)
                        break
        if exact or stem_match:
            break
    return exact or stem_match or contains


def _suggest_app_names(query: str, limit: int = 3) -> list[str]:
    """对无法解析的应用名做只读近似候选（不启动任何程序）。

    候选来自别名键和 .desktop 应用名/stem，按大小写无关的字符相似度/
    包含关系打分，低于阈值丢弃。多应用串（含"和/以及/与/跟/及/and"）由
    调用方先排除，避免把“打开终端和控制中心”误当成对“控制中心”的近似。
    """
    q = (query or "").strip().casefold()
    if not q:
        return []

    scored: dict[str, float] = {}

    def add(label: str) -> None:
        label = (label or "").strip()
        n = label.casefold()
        if not n or n == q:
            return
        if q in n or n in q:
            base = 0.9
        else:
            base = difflib.SequenceMatcher(None, q, n).ratio()
        if n.startswith(q) or q.startswith(n):
            base = min(1.0, base + 0.1)
        if base >= 0.5:
            scored[label] = max(scored.get(label, 0.0), base)

    for alias in _app_aliases():
        add(alias)
    for app in _get_desktop_apps():
        add(app["stem"])
        for name in app["names"]:
            add(name)
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
    return [label for label, _score in ranked[:limit]]


def _exec_argv(exec_line: str) -> list[str] | None:
    """把 Exec 行解析成安全的 argv，去掉 %U/%F 等桌面字段码。"""
    try:
        argv = shlex.split(exec_line)
    except ValueError:
        return None
    cleaned: list[str] = []
    for token in argv:
        if len(token) == 2 and token[0] == "%" and token[1] in _DESKTOP_FIELD_CODES:
            continue
        cleaned.append(token)
    return cleaned or None


def _host_worker_desktop_launch() -> bool:
    # Supervisor identity only; tool arguments and desktop process env are not
    # an authorization source. An active sandbox must retain direct execution.
    return (os.environ.get("BIANBU_RUNTIME_ROLE") == "agent-worker"
            and os.environ.get("BIANBU_WORKER_EFFECTIVE_MODE") == "host"
            and os.environ.get("BIANBU_SANDBOX_ACTIVE") != "1")


_SESSION_SETENV_KEYS = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
    # XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS: Wayland socket, portal, tray
    # and single-instance hand-off all need them; without them GUI apps start
    # detached from the real desktop session and can exit immediately.
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_SESSION_ID",
    "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "XDG_SEAT", "XDG_VTNR",
    "DESKTOP_SESSION", "DBUS_SESSION_BUS_ADDRESS",
    "LANG", "LANGUAGE",
)
_SESSION_SETENV_PREFIXES = ("QT_", "GTK_", "LXQT_", "LC_")


def _session_setenv_args(session_env: dict | None) -> list[str]:
    """桌面会话里 GUI 进程需要的显示/主题变量 → systemd-run --setenv 参数。"""
    args: list[str] = []
    for key, value in sorted((session_env or {}).items()):
        if not value or "\n" in value:
            continue
        if key in _SESSION_SETENV_KEYS or key.startswith(_SESSION_SETENV_PREFIXES):
            args.append(f"--setenv={key}={value}")
    return args


def _launch_host_desktop(argv: list[str], app_name: str, session_env: dict | None) -> ExecResult:
    if os.environ.get("BIANBU_SANDBOX_ACTIVE") != "0":
        return ExecResult(False, "缺少 Host 桌面启动标记，请更新 Runtime 后重试")
    try:
        uid = os.getuid()
        home = pwd.getpwuid(uid).pw_dir
        runtime_dir = f"/run/user/{uid}"
        # The user manager launches a service in the desktop namespace. A
        # --scope would inherit our hardened Runtime mount namespace again.
        command = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect",
                   "--service-type=exec", f"--working-directory={home}",
                   f"--setenv=HOME={home}"]
        command.extend(_session_setenv_args(session_env))
        command.extend(["--", *argv])
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin", "HOME": home,
                            "XDG_RUNTIME_DIR": runtime_dir,
                            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus"})
        return ExecResult(True, f"已启动应用: {app_name}")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return ExecResult(False, "宿主桌面应用启动失败", str(exc))


def _edge_service_desktop_launch() -> bool:
    """边侧 bibit-intent 服务自身（非 Runtime Worker）是否应经用户服务管理器启动应用。

    服务以 systemd user service 长驻；直接 Popen 的 GUI 会留在
    bibit-intent.service 的 cgroup 里，包升级 restart 服务时会把用户开着的
    终端/浏览器一起杀掉。用 systemd-run --user 把应用挂到独立 transient
    service 下。Worker 身份（sandbox/host）由 _host_worker_desktop_launch 决定。
    """
    if os.environ.get("BIANBU_RUNTIME_ROLE"):
        return False
    if os.getuid() == 0:
        return False
    return (os.path.exists(f"/run/user/{os.getuid()}/bus")
            and shutil.which("systemd-run") is not None)


def _launch_via_user_manager(argv: list[str], session_env: dict | None) -> bool:
    """systemd-run --user 启动 argv；成功返回 True，失败返回 False 由调用方回退。"""
    uid = os.getuid()
    runtime_dir = f"/run/user/{uid}"
    command = ["systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
               *_session_setenv_args(session_env), "--", *argv]
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


_LAUNCH_VERIFY_TIMEOUT = 8.0
_LAUNCH_VERIFY_INTERVAL = 0.2


def _spawn_detached(argv: list[str], env: dict | None) -> subprocess.Popen:
    """启动脱离服务进程组的桌面 GUI 子进程，并保证退出后被回收。

    GUI 必须 start_new_session（脱离 bibit-intent.service 的 cgroup，否则服务
    restart 会把用户开着的窗口一起杀掉），但这样一来就无法用 wait() 阻塞回收：
    子进程随时可能被外部 pkill/用户关窗结束，而服务自身等到下一次 Spawn 才
    有机会清理，期间 /proc 里一直挂着 <defunct>。实测「打开相机 → 关闭相机」
    必留一个 zombie snapshot。

    这里只对本函数启动的这棵进程树做「兜底回收」：不注册 SIGCHLD/waitpid(-1)，
    因为那会抢走 run_command（bounded_command 里阻塞 wait 的 shell 工具）的退出
    状态。每个子进程起一个 daemon 线程等它退出并 wait() 掉，线程随进程结束，
    不留残留。
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )

    def _reap() -> None:
        try:
            proc.wait()
        except Exception:  # noqa: BLE001 - 回收线程绝不能把异常抛进未捕获线程钩子
            pass

    threading.Thread(target=_reap, name="bibit-detached-reap", daemon=True).start()
    return proc


def _expected_process_name(argv: list[str]) -> str | None:
    """从启动 argv 推出「桌面上应该出现的进程名」。

    跳过 env/sh 等包装器后落到真正的 GUI 可执行名；chromium 的 /usr/bin 入口
    是 shell wrapper，但它 exec 出来的子进程同名（chromium），basename 匹配
    即可覆盖，不需要解析脚本内容。
    """
    if not argv:
        return None
    name = _first_close_target(argv)
    if name:
        return name
    name = _exec_binary_name(argv[0])
    return name or None


def _proc_matches_name(pid_dir: str, name: str) -> bool:
    """单个 /proc/<pid> 是否对应该可执行名。

    优先看 cmdline argv[0]；Chromium 这类程序会用 prctl 重写命令行，把整条
    命令压成一个用空格分隔的字符串（cmdline 里没有 NUL），所以要再按空白切
    一次。cmdline 不可用时回退到 /proc/<pid>/exe 的真实可执行路径。
    """
    raw = b""
    try:
        raw = Path(pid_dir, "cmdline").read_bytes()
    except OSError:
        raw = b""
    if raw:
        head = raw.split(b"\0", 1)[0].decode(errors="ignore").split(None, 1)[0]
        if head and _exec_binary_name(head) == name:
            return True
    try:
        exe = os.readlink(Path(pid_dir) / "exe")
    except OSError:
        return False
    return bool(exe) and _exec_binary_name(exe) == name


def _process_name_running(name: str) -> bool:
    """扫描 /proc/*，判断该可执行名是否有活进程。"""
    return bool(_process_pids(name))


def _process_pids(name: str) -> set[int]:
    """返回当前与可执行名匹配的所有 PID。

    与 _process_name_running 不同，这里保留完整 PID 集合，供 launch 校验
    区分「启动前就存在的残留进程」和「本次启动真正创建的新进程」。
    """
    if not name:
        return set()
    pids: set[int] = set()
    for pid_dir in glob.glob("/proc/[0-9]*"):
        if not _proc_matches_name(pid_dir, name):
            continue
        try:
            pids.add(int(os.path.basename(pid_dir)))
        except ValueError:
            continue
    return pids


def _verify_launch(
    argv: list[str],
    app_name: str,
    before_pids: set[int] | None = None,
) -> ExecResult:
    """启动后校验：本次启动真的产生了对应进程才返回成功。

    systemd-run/Popen 退出码只说明命令被受理；chromium 这类 wrapper 会在
    「已有浏览器会话」或环境缺失时立刻退出，于是回复“已启动”但桌面无窗口。
    这里轮询确认出现了启动前不存在的目标进程，否则返回明确失败。

    before_pids 必须是发起启动前同一目标进程名的 PID 快照；未传时保留旧
    行为（只看目标进程是否存在），用于无需区分新旧进程的兼容调用。
    """
    target = _expected_process_name(argv)
    if not target:
        return ExecResult(True, f"已启动应用: {app_name}", attempted=True)
    deadline = time.monotonic() + _LAUNCH_VERIFY_TIMEOUT
    while True:
        if before_pids is None:
            running = _process_name_running(target)
        else:
            running = bool(_process_pids(target) - before_pids)
        if running:
            return ExecResult(True, f"已启动应用: {app_name}", attempted=True)
        if time.monotonic() >= deadline:
            return ExecResult(
                False,
                f"已发送“{app_name}”的启动请求，暂未确认窗口是否显示，请检查桌面；不会重复启动。",
                f"expected process: {target}",
                attempted=True,
                detail_format="internal",
                unverified=True,
            )
        time.sleep(_LAUNCH_VERIFY_INTERVAL)


def _gui_child_env(session_env: dict | None) -> dict[str, str]:
    """构造 GUI 子进程环境：继承本进程环境再叠加桌面会话变量。

    WAYLAND_SOCKET 是「已连接的 Wayland fd 编号」，只对持有该 fd 的进程有效。
    本进程或在桌面里启动的父进程环境里可能残留旧值，libwayland 会优先用它而不是
    WAYLAND_DISPLAY，子进程里该 fd 不存在 → GTK 应用退回 X11 后启动失败
    （实测锁屏报 "doesn't support ext-session-lock"）。统一在这里剔除。
    """
    env = dict(os.environ)
    if session_env:
        env.update(_desktop_session_home_env(session_env))
    env.pop("WAYLAND_SOCKET", None)
    return env


def _launch_argv(argv: list[str], app_name: str, session_env: dict | None) -> ExecResult:
    """启动 argv 表示的进程；有桌面会话环境时注入，保证 GUI 落在当前桌面。"""
    target = _expected_process_name(argv)
    before_pids = _process_pids(target) if target else set()
    if _host_worker_desktop_launch():
        result = _launch_host_desktop(argv, app_name, session_env)
        if not result.ok:
            return result
        return _verify_launch(argv, app_name, before_pids)
    env = _gui_child_env(session_env)
    if _edge_service_desktop_launch():
        resolved = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0], path=env.get("PATH"))
        if resolved is None:
            return ExecResult(False, f"命令不存在：{argv[0]}")
        if _launch_via_user_manager([resolved, *argv[1:]], session_env):
            return _verify_launch([resolved, *argv[1:]], app_name, before_pids)
        # user manager 不可用（无 session bus 等）时退回直接启动，功能优先。
    try:
        _spawn_detached(argv, env)
        return _verify_launch(argv, app_name, before_pids)
    except FileNotFoundError:
        return ExecResult(False, f"命令不存在：{argv[0]}")
    except PermissionError as e:
        return ExecResult(False, f"无权限启动：{argv[0]}", str(e))
    except OSError as e:
        return ExecResult(False, f"启动失败：{e}")


_UNSAFE_BARE_COMMANDS = frozenset({
    "poweroff", "reboot", "shutdown", "halt", "init", "telinit", "systemctl",
    "kill", "pkill", "killall", "rm", "dd", "mkfs", "fdisk", "parted", "sudo",
    "su", "pkexec", "passwd", "chmod", "chown", "mount", "umount", "sh", "bash",
    "dash", "zsh", "env", "exec", "nohup", "eval", "python", "python3", "perl",
})
_UNSAFE_BARE_DIRS = ("/sbin", "/usr/sbin", "/usr/local/sbin")


def _bare_command_path(name: str) -> str | None:
    """命令行回退：只接受 PATH 中真实存在、非 sbin、非破坏性/解释器的可执行文件。

    用户口中的应用名直接当命令执行风险很高（"打开 poweroff"），这里不做
    "先跑一下看能不能起来"，先解析再决定。
    """
    if not name or name in _UNSAFE_BARE_COMMANDS or name.startswith("mkfs"):
        return None
    path = shutil.which(name)
    if not path:
        return None
    real = os.path.realpath(path)
    if any(real == d or real.startswith(d + "/") for d in _UNSAFE_BARE_DIRS):
        return None
    if Path(real).name in _UNSAFE_BARE_COMMANDS:
        return None
    return path


def launch_app(app_name: str, language: str | None = None, **_) -> ExecResult:
    """
    启动应用。策略（按优先级）：
      ① 内置中文别名映射（如"终端" → qterminal）
      ② 扫描 /usr/share/applications、~/.local/share/applications，
         按 Name / Name[zh_CN] / 文件名匹配，并用 Exec 字段启动
      ③ 命令行工具回退：直接执行 / which 找路径
    """
    if not app_name or not isinstance(app_name, str):
        return ExecResult(False, "缺少应用名称")
    raw = app_name.strip()
    if not raw:
        return ExecResult(False, "缺少应用名称")

    session_env = _find_desktop_session_env()

    location = DESKTOP_LOCATIONS.get(raw.casefold())
    if location:
        # gvfs 虚拟位置（trash:/// computer:///）优先走 FileManager1：gio open
        # 会按默认 handler 分发，端侧 inode/directory 被 codium 抢走，导致
        # 「打开回收站/计算机」用编辑器打开甚至无窗口。
        if _file_manager1_show_folders(location, session_env):
            return ExecResult(
                True,
                _reply(f"已打开: {location}", f"Opened: {location}", language),
            )
        gio = shutil.which("gio")
        if gio:
            return _launch_desktop_argv(
                [gio, "open", location],
                f"位置 {location}",
                language,
                f"已打开: {location}",
                f"Opened: {location}",
            )
        opener = shutil.which("xdg-open")
        if opener:
            return _launch_desktop_argv(
                [opener, location],
                f"位置 {location}",
                language,
                f"已打开: {location}",
                f"Opened: {location}",
            )
        return ExecResult(False, f"未找到 gio/xdg-open，无法打开位置: {location}")

    # ① 内置别名（.desktop 匹配不到的常用叫法）
    # Prefer aliases declared in tools.yaml (exposed through ToolRegistry),
    # while retaining the legacy table for direct handler compatibility.
    mapped = _app_aliases().get(app_name.casefold()) or _app_aliases().get(raw.casefold())
    apps = _get_desktop_apps()
    desktop = _match_desktop_app(mapped or raw, apps)
    if desktop is None and mapped:
        desktop = _match_desktop_app(raw, apps)
    if desktop and desktop.get("terminal"):
        argv = _exec_argv(desktop["exec"])
        if not argv:
            return ExecResult(False, f"应用启动配置无效: {app_name}")
        env = dict(os.environ)
        env.update(session_env or {})
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            return ExecResult(False, "未找到图形桌面会话，请先登录桌面再打开终端应用")
        for terminal in ("qterminal", "x-terminal-emulator", "xterm"):
            executable = shutil.which(terminal, path=env.get("PATH"))
            if executable:
                return _launch_argv([executable, "-e", *argv], app_name, session_env)
        return ExecResult(False, "未找到终端模拟器，请先安装 qterminal 再打开终端应用")
    if mapped:
        r = _launch_argv(_app_command(mapped), app_name, session_env)
        if r.ok or r.attempted or _host_worker_desktop_launch():
            return r
        # 别名指向的裸命令不存在时继续走后续解析，不要直接失败

    # ② .desktop 动态解析
    desktop = _match_desktop_app(raw, apps)
    if desktop:
        argv = _exec_argv(desktop["exec"])
        if argv:
            r = _launch_argv(argv, app_name, session_env)
            if r.ok or r.attempted or _host_worker_desktop_launch():
                return r

    # ③ 命令行工具回退：先解析出安全可执行路径再启动，不盲跑裸词
    safe_name = "".join(c for c in raw if c.isalnum() or c in "-_.")
    if safe_name:
        full_path = _bare_command_path(safe_name)
        if full_path:
            r = _launch_argv([full_path], app_name, session_env)
            if r.ok or r.attempted or _host_worker_desktop_launch():
                return r

    # 多应用串（"打开终端和控制中心"这类被规则整串抓来的名字）不在本地做
    # 近似建议：交给云端按应用拆分，避免把整串误判成对某个应用的近似。
    if not _APP_CONJUNCTION_RE.search(raw):
        suggestions = _suggest_app_names(raw)
        if suggestions:
            return ExecResult(
                False,
                f"没有找到“{raw}”这个应用。相近的应用：{'、'.join(suggestions)}",
                " | ".join(suggestions),
            )
    return ExecResult(
        False,
        f"应用未找到: {app_name} (请检查应用是否已安装)",
        escalate="app_not_found",
    )


_WRAPPER_BINS = frozenset({
    "env", "dbus-run-session", "sh", "bash", "dash", "exec", "nohup", "timeout",
})
# 启动命令与实际常驻进程名不同的 GUI 包装器：libreoffice 只是入口脚本，
# 真正桌面进程是 soffice.bin（oosplash 会随后退出）。关闭/启动校验都要
# 按真实进程名匹配，否则“关闭 LibreOffice Writer”会永远找不到进程。
_WRAPPER_PROCESS_ALIASES = {
    "libreoffice": "soffice.bin",
}
# 绝不能作为 close_app 目标的进程：桌面会话/系统基础设施与解释器本体。
_PROTECTED_CLOSE_TARGETS = frozenset({
    "systemd", "systemd-run", "labwc", "wayfire", "sway", "weston", "hyprland",
    "Xorg", "Xwayland", "dbus-daemon", "dbus-broker", "pipewire", "wireplumber",
    "pulseaudio", "sshd", "ssh", "python", "python3", "perl", "node", "init",
    "lxqt-session", "lxqt-panel", "sddm", "bibit-intent", "bianbu-agent",
    "kill", "pkill", "killall",
})
_INTERPRETER_BINS = "(python[0-9.]*|sh|bash|dash|node|perl)"
_ERE_SPECIAL = re.compile(r"[.^$*+?()\[\]{}|\\]")


def _pkill_pattern(target: str) -> str:
    """把可执行名转成 pkill -f 用的 ERE：锚定到 argv[0]（或解释器后的 argv[1]）。

    未锚定的 `pkill -f name` 是整条命令行子串匹配："关闭 code" 会连 codium
    一起杀，"关闭微信" 能杀掉命令行里恰好含"微信"二字的任何进程（实测杀掉了
    发起请求的 SSH shell）。这里只允许 name 出现在：
      - 命令行第一个 token（可带目录前缀），或
      - 解释器（python/sh/node…）之后的第一个 token（脚本型应用）。
    ERE 元字符按字面转义，`-` 等普通字符保持原样。
    """
    literal = _ERE_SPECIAL.sub(lambda m: "\\" + m.group(0), target)
    exe = f"([^ ]*/)?{literal}( |$)"
    return f"^{exe}|^([^ ]*/)?{_INTERPRETER_BINS} {exe}"


def _exec_binary_name(command: str) -> str:
    """从命令/路径取进程匹配名（/usr/bin/chromium → chromium）。"""
    name = Path(command).name if "/" in command else command
    return name.strip()


def _real_process_name(name: str) -> str:
    """把入口命令名映射到真实常驻进程名（libreoffice → soffice.bin）。"""
    return _WRAPPER_PROCESS_ALIASES.get(name, name)


def _close_target(command: str) -> str | None:
    """把命令归一成可安全 pkill 的目标；包装器命令不直接作为目标。"""
    name = _exec_binary_name(command)
    if not name or name in _WRAPPER_BINS or name in _PROTECTED_CLOSE_TARGETS:
        return None
    return _real_process_name(name)


def _first_close_target(tokens: list[str]) -> str | None:
    """从头扫描 argv，返回第一个可当作 pkill 目标的真实可执行名。

    跳过包装器（env/dbus-run-session/sh/bash/dash/exec/nohup）、环境变量赋值
    与选项；内联命令串（bash -c "exec /usr/bin/xterm -e vim" 的引号内容）
    拆词后递归处理，避免把带空格的整段命令误当进程名。
    """
    for token in tokens:
        if "=" in token or token.startswith("-"):
            continue
        words = shlex.split(token) if any(ch.isspace() for ch in token) else [token]
        if len(words) > 1:
            inner = _first_close_target(words)
            if inner:
                return inner
            continue
        name = _exec_binary_name(words[0])
        if name and name not in _WRAPPER_BINS and name not in _PROTECTED_CLOSE_TARGETS:
            return _real_process_name(name)
    return None


def _desktop_close_target(exec_line: str) -> str | None:
    """取 .desktop Exec 里实际 GUI 可执行名（env/sh 等包装器场景跳过）。"""
    argv = _exec_argv(exec_line)
    if not argv:
        return None
    return _first_close_target(argv)


def _raw_close_target(safe_name: str) -> str | None:
    """原样名兜底：只有它本身就是 PATH 里的真实可执行文件才允许当 pkill 目标。

    "微信"/"设置" 这类未解析到程序的名字不再直接 pkill —— 那会按子串匹配
    整条命令行误杀无关进程，然后还回报"已关闭"。
    """
    if not safe_name or not safe_name.isascii():
        return None
    if shutil.which(safe_name) is None:
        return None
    return _close_target(safe_name)


def close_app(app_name: str, **_) -> ExecResult:
    """
    关闭应用。解析策略与 launch_app 对称：
      ① 内置别名映射（如"控制中心" → bianbu-control-center-shell）
      ② .desktop 匹配（Name / Name[zh_CN] / 文件名 → Exec 可执行名）
      ③ 原样命令名回退（仅当它是 PATH 中真实可执行文件）
    按序 pkill（模式锚定到进程的可执行名），命中即关闭；全部未命中视为找不到可关的应用，
    与 launch_app 的 app_not_found 一致，升级云端兜底。
    """
    if not app_name or not isinstance(app_name, str):
        return ExecResult(False, "缺少应用名称")
    raw = app_name.strip()
    if not raw:
        return ExecResult(False, "缺少应用名称")

    safe_name = "".join(c for c in raw if c.isalnum() or c in "-_.")
    if not safe_name:
        return ExecResult(False, f"无效的应用名称: {app_name}")

    targets: list[str] = []
    mapped = _app_aliases().get(raw.casefold()) or _app_aliases().get(
        safe_name.casefold()
    )
    if mapped:
        target = _close_target(mapped)
        if target and target not in targets:
            targets.append(target)

    desktop = _match_desktop_app(raw, _get_desktop_apps())
    if desktop:
        target = _desktop_close_target(desktop["exec"])
        if target and target not in targets:
            targets.append(target)

    raw_target = _raw_close_target(safe_name)
    if raw_target and raw_target not in targets:
        targets.append(raw_target)

    for target in targets:
        # pkill 返回 0 表示找到并杀死了进程,1 表示未找到
        r = _run(["pkill", "-f", _pkill_pattern(target)], timeout=3)
        if r.ok:
            return ExecResult(True, f"已关闭应用: {raw}")

    # 与 launch_app 对称：单应用名找不到可关进程时，先做只读近似检查并
    # 在本地答复里给出候选名（绝不自动关闭候选）；名字含连词的多应用整串
    # 不做近似建议，保持升级云端由模型拆分。应用已安装但未运行且无近似时，
    # 没有可建议的对象，仍升级云端说明。
    if not _APP_CONJUNCTION_RE.search(raw):
        tried = set(targets)

        def _fresh(label: str) -> bool:
            """建议指向的可执行名若已经 pkill 过（同一应用未在运行），丢弃。"""
            alias = _app_aliases().get(label.casefold())
            if alias:
                return _close_target(alias) not in tried
            for app in _get_desktop_apps():
                if (
                    app["stem"].casefold() == label.casefold()
                    or any(nm.casefold() == label.casefold() for nm in app["names"])
                ):
                    return _desktop_close_target(app["exec"]) not in tried
            return True

        suggestions = [
            label for label in _suggest_app_names(raw) if _fresh(label)
        ]
        if suggestions:
            return ExecResult(
                False,
                f"没有找到正在运行的“{raw}”。相近的应用：{'、'.join(suggestions)}",
                " | ".join(suggestions),
            )
    # 名称可能未解析到真实进程（如"微信"），或应用未在运行；
    # 本地无法确认，交给云端模型兜底（与 launch_app 行为对称）。
    return ExecResult(
        False,
        f"未找到运行中的应用: {raw}",
        " | ".join(targets),
        escalate="app_not_found",
    )


def close_apps(app_names: str | list[str] | None = None,
               language: str | None = None, **_) -> ExecResult:
    """批量关闭应用：逐个复用 close_app 的解析逻辑，聚合逐条结果。

    与 launch_apps 对称，供云端 MCP 表达“关闭终端和控制中心”等多应用
    请求；收到字符串时走 split_app_names 保守拆分（每段必须可解析成
    真实应用才拆，避免拆散名字自带“和/空格”的应用）。
    """
    if app_names is None:
        return ExecResult(False, "缺少应用名称列表")
    if isinstance(app_names, str):
        names = split_app_names(app_names)
    else:
        names = [str(n).strip() for n in app_names if str(n).strip()]
    if not names:
        return ExecResult(False, "缺少应用名称列表")
    ok_count = 0
    lines: list[str] = []
    for name in names:
        r = close_app(name)
        if r.ok:
            ok_count += 1
        lines.append(f"{name}: {r.message}")
    total = len(names)
    if ok_count == total:
        msg = _reply(f"已关闭 {total} 个应用", f"Closed {total} application(s)", language)
    elif ok_count > 0:
        msg = _reply(f"关闭完成 {ok_count}/{total}", f"Closed {ok_count}/{total}", language)
    else:
        msg = _reply(f"关闭失败 {total} 个应用", f"Failed to close {total} application(s)", language)
    return ExecResult(ok_count > 0, msg, "\n".join(lines))


_APP_PUNCT_SEP_RE = re.compile(r"[,，、;；]+")
_APP_CONJUNCTION_RE = re.compile(r"(?:以及|还有|和|与|跟|及|\band\b)", re.IGNORECASE)


def _resolvable_app_name(name: str) -> bool:
    """名字能否作为单个应用名解析（别名精确命中或 .desktop 整名命中）。

    不做 contains 子串匹配：拆分守卫只需要"整名可启动"，子串命中会把
    "file" 误当成 "file manager" 这类应用。
    """
    n = name.strip().casefold()
    if not n:
        return False
    if _app_aliases().get(n):
        return True
    for app in _get_desktop_apps():
        if app["stem"].casefold() == n:
            return True
        if any(nm.casefold() == n for nm in app["names"]):
            return True
    return False


def split_app_names(raw: str) -> list[str]:
    """把多个应用名拆成名单（供 launch_apps 字符串入参/信封兜底）。

    按优先级保守拆：
      ① 语义连词（和/以及/还有/与/跟/及/and）整体切分 —— 每个片段必须
         能被本地别名/.desktop 解析成真实应用才接受，避免误拆应用名里
         自带"和"的情况；
      ② 标点显式分隔（，,、;；）—— 标点是明确列表语法，直接拆；每段再
         尝试一次连词切分；
      ③ 纯空白（含回车/换行/全角空格）—— 只有每段都可解析时才拆，否则
         当作含空格的单个应用名（如 "file manager"）保留整串。
    云端模型自行给出名单时参数已是 list，不会走到这里。
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    # 整串本身就能作为一个应用名解析（可能名字自带"和"）→ 优先保留整串
    if _resolvable_app_name(raw):
        return [raw]

    def conj_parts(token: str) -> list[str]:
        return [p.strip() for p in _APP_CONJUNCTION_RE.split(token) if p.strip()]

    # ① 连词整体切分
    cands = conj_parts(raw)
    if len(cands) >= 2 and all(_resolvable_app_name(p) for p in cands):
        return cands

    # ② 标点显式分隔
    out: list[str] = []
    for token in (p.strip() for p in _APP_PUNCT_SEP_RE.split(raw) if p.strip()):
        cands = conj_parts(token)
        if len(cands) >= 2 and all(_resolvable_app_name(p) for p in cands):
            out.extend(cands)
        else:
            out.append(token)
    if len(out) >= 2:
        return out

    # ③ 纯空白兜底（仅当每段都可解析）
    if len(out) == 1:
        words = [w for w in re.split(r"\s+", out[0]) if w]
        if len(words) >= 2 and all(_resolvable_app_name(w) for w in words):
            return words
    return out


def resolve_multi_app_names(raw: str) -> list[str] | None:
    """把“打开A和B”里的对象部分解析成 >=2 个真实应用名；不足 / 无法解析返回 None。

    供路由层在多应用串上确定性拆分：只有每个片段都能被本地别名或 .desktop
    整名解析时才接受，避免把名字自带“和/与”的单个应用拆坏，也避免把规则层
    没有把握的串硬拆后逐条报错。
    """
    names = split_app_names(raw)
    if len(names) < 2:
        return None
    if not all(_resolvable_app_name(name) for name in names):
        return None
    return names


def launch_apps(app_names: str | list[str] | None = None,
                language: str | None = None, **_) -> ExecResult:
    """批量启动应用：逐个复用 launch_app 的解析逻辑，聚合逐条结果。

    供 MCP 模型在一轮内表达多动作（“打开终端和设置”）；规则层由
    multi_action 检测升级云端，不直接调用。
    """
    if app_names is None:
        return ExecResult(False, "缺少应用名称列表")
    if isinstance(app_names, str):
        names = split_app_names(app_names)
    else:
        names = [str(n).strip() for n in app_names if str(n).strip()]
    if not names:
        return ExecResult(False, "缺少应用名称列表")
    ok_count = 0
    lines: list[str] = []
    for name in names:
        r = launch_app(name, language=language)
        if r.ok:
            ok_count += 1
        lines.append(f"{name}: {r.message}")
    total = len(names)
    if ok_count == total:
        msg = _reply(f"已启动 {total} 个应用", f"Launched {total} application(s)", language)
    elif ok_count > 0:
        msg = _reply(f"启动完成 {ok_count}/{total}", f"Launched {ok_count}/{total}", language)
    else:
        msg = _reply(f"启动失败 {total} 个应用", f"Failed to launch {total} application(s)", language)
    return ExecResult(ok_count > 0, msg, "\n".join(lines))


def list_apps(query: str = "", language: str | None = None, **_) -> ExecResult:
    """列出可启动的应用（.desktop 名称/文件名 + 内置别名）。只读。

    供 MCP 模型在调用 launch_app 前查询准确的 app_name；规则路径从原文
    抽词，不需要它。query 非空时按名称包含匹配过滤。
    """
    q = (query or "").strip().casefold()
    seen: dict[str, str] = {}  # 名称 -> 提示（stem 或 alias:target）

    def add(name: str, hint: str) -> None:
        name = (name or "").strip()
        if not name or name in seen:
            return
        if q and q not in name.casefold():
            return
        seen[name] = hint

    for app in _get_desktop_apps():
        for n in app["names"]:
            add(n, app["stem"])
        add(app["stem"], app["stem"])
    for alias, target in _app_aliases().items():
        add(alias, f"alias:{target}")

    if not seen:
        return ExecResult(
            True,
            _reply("没有找到可启动的应用", "No launchable applications found", language),
            "",
        )
    lines = [
        f"{name}" + (f" ({hint})" if hint else "")
        for name, hint in sorted(seen.items())
    ]
    return ExecResult(
        True,
        _reply(f"可用应用 {len(lines)} 个：",
               f"{len(lines)} launchable application(s):", language),
        "\n".join(lines),
    )


# ── 锁屏 ─────────────────────────────────────────────────────────────

# 桌面会话进程名：从 /proc/<pid>/environ 提取真实会话环境
_DESKTOP_SESSION_PROCS = ("labwc", "wayfire", "sway", "weston", "hyprland", "Xorg", "Xwayland")


def _session_env_owner_uid(env: dict[str, str]) -> int | None:
    """从会话环境的 XDG_RUNTIME_DIR（/run/user/<uid>）反推会话属主 uid。"""
    m = re.fullmatch(r"/run/user/(\d+)", env.get("XDG_RUNTIME_DIR") or "")
    return int(m.group(1)) if m else None


def _desktop_session_home_env(env: dict[str, str]) -> dict[str, str]:
    """按桌面会话属主补全 HOME/XDG_CONFIG_HOME。

    会话环境来自合成器进程，root 的会话或部分合成器不带 HOME；此时若沿用
    bibit-intent 服务自身的 HOME（可能是 root 的 /root），GUI 子进程连同
    AT-SPI helper 都会把用户目录算错（snapshot 会把照片写进 /root/Pictures）。
    以 XDG_RUNTIME_DIR 反推的会话属主为准（labwc/wayland 会话内该目录必为
    /run/user/<uid>），查不到属主才回退进程属主。会话自带的 HOME 不动。
    """
    if env.get("HOME"):
        return env
    uid = _session_env_owner_uid(env)
    if uid is None:
        return env
    try:
        home = pwd.getpwuid(uid).pw_dir
    except (KeyError, OSError):
        return env
    if not home:
        return env
    env = dict(env)
    env["HOME"] = home
    env.setdefault("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    return env


def _session_belongs_to_user(env: dict[str, str], proc_uid: int, uid: int) -> bool:
    """判断桌面会话是否属于 uid 用户；uid 为 0（root）时不设限（任意会话）。

    优先用 XDG_RUNTIME_DIR 反推的属主（Xorg 常以 root 启动、进程属主不可靠），
    反推不出才退回进程属主 /proc/<pid> 的 st_uid。
    """
    if not uid:
        return True
    owner_uid = _session_env_owner_uid(env)
    if owner_uid is not None:
        return owner_uid == uid
    return proc_uid == uid


def _find_desktop_session_env(uid: int | None = None) -> dict | None:
    """
    从桌面会话进程（labwc/wayfire/sway/weston/Xorg 等）的 /proc/<pid>/environ
    提取真实会话环境（XDG_RUNTIME_DIR / WAYLAND_DISPLAY / DISPLAY /
    DBUS_SESSION_BUS_ADDRESS / HOME，含 QT_*/LXQT_*/XDG_CURRENT_DESKTOP 等
    主题变量，整体返回供 GUI 子进程复用桌面外观）。

    Agent 常以 root 运行，环境里缺这些变量；bianbu-gtklock 是 Wayland 客户端，
    连不上会话 socket 会直接失败，必须先注入会话环境。

    多用户场景：默认只返回当前用户（os.getuid()）的会话，避免把别的用户的
    DISPLAY/WAYLAND_DISPLAY/XDG_RUNTIME_DIR 注入子进程（sudo 授权框会弹到
    错误的会话）；root（uid 0，系统级 agent）无自有桌面会话，退化为返回任意
    可用会话，保持 lock_screen 等既有行为。找不到会话返回 None。
    """
    if uid is None:
        uid = os.getuid()
    candidates: list[dict[str, str]] = []
    for pid_dir in sorted(glob.glob("/proc/[0-9]*")):
        try:
            cmdline = Path(pid_dir, "cmdline").read_bytes().split(b"\0")[0].decode(errors="ignore")
        except (OSError, IndexError):
            continue
        if not any(proc in cmdline for proc in _DESKTOP_SESSION_PROCS):
            continue
        try:
            raw = Path(pid_dir, "environ").read_bytes()
            proc_uid = os.stat(pid_dir).st_uid
        except OSError:
            continue
        proc_env: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if b"=" in item:
                k, _, v = item.partition(b"=")
                proc_env[k.decode(errors="ignore")] = v.decode(errors="ignore")
        if not proc_env.get("XDG_RUNTIME_DIR") or not os.path.isdir(proc_env["XDG_RUNTIME_DIR"]):
            continue
        if not _session_belongs_to_user(proc_env, proc_uid, uid):
            continue
        candidates.append(proc_env)
    if not candidates:
        return None
    # 同一图形会话里合成器与 Xwayland/Xorg 各带一部分变量：labwc 有
    # WAYLAND_DISPLAY 而没有 DISPLAY，Xwayland 才是 DISPLAY/XAUTHORITY 的
    # 来源。按同一 XDG_RUNTIME_DIR 归并，缺失的键用同会话其他进程补齐。
    env = candidates[0]
    runtime_dir = env["XDG_RUNTIME_DIR"]
    for other in candidates[1:]:
        if other.get("XDG_RUNTIME_DIR") != runtime_dir:
            continue
        for key, value in other.items():
            env.setdefault(key, value)
    # WAYLAND_DISPLAY 缺省时按 /run/user/<uid>/wayland-* socket 推导
    if not env.get("WAYLAND_DISPLAY"):
        for sock in sorted(glob.glob(os.path.join(runtime_dir, "wayland-*"))):
            name = os.path.basename(sock)
            if not name.endswith(".lock"):
                env["WAYLAND_DISPLAY"] = name
                break
    # WAYLAND_SOCKET 是「已连接的 Wayland fd 编号」，只对持有该 fd 的进程有效。
    # Xwayland 的环境里带 WAYLAND_SOCKET=74，按同会话补齐时会混进来；libwayland
    # 优先用 WAYLAND_SOCKET 而不是 WAYLAND_DISPLAY，子进程里 fd 74 不存在，GTK
    # 应用于是退回 X11 并报 "doesn't support ext-session-lock"（实测锁屏静默失败）。
    env.pop("WAYLAND_SOCKET", None)
    if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
        return None
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return env


_LOCK_PROCESS_NAME = "gtklock"
_LOCK_VERIFY_TIMEOUT = 3.0
_LOCK_VERIFY_INTERVAL = 0.2


def lock_screen(**_) -> ExecResult:
    """锁屏（bianbu-gtklock）：注入桌面会话环境变量后后台启动并校验。

    gtklock 是阻塞式锁屏进程，用 Popen 后台拉起立即返回；Agent 以 root
    运行时必须带上会话环境（WAYLAND_DISPLAY/XDG_RUNTIME_DIR/DBUS…），
    否则 Wayland 客户端连不上会话直接失败。Popen 成功只说明命令被受理，
    连不上合成器/合成器不支持 ext-session-lock 时 gtklock 会立刻退出，
    所以这里轮询确认锁屏进程真的活着，避免回「已锁屏」但屏幕没锁。
    """
    env = _find_desktop_session_env()
    if env is None:
        return ExecResult(False, "未找到桌面会话（无法确定 Wayland 环境），锁屏失败")
    full_env = _gui_child_env(env)
    try:
        # 阻塞式锁屏进程：spawn 后不 wait（锁屏要一直活着），仍交给兜底回收
        # 线程，用户解锁退出后不会留下 zombie。
        _spawn_detached(["bianbu-gtklock"], full_env)
    except FileNotFoundError:
        return ExecResult(False, "命令不存在：bianbu-gtklock")
    except OSError as e:
        return ExecResult(False, f"锁屏命令启动失败：{e}")
    # bianbu-gtklock 是 setsid -f 包装脚本，父进程立刻退出属正常；
    # 只以「gtklock 进程是否存在」判断锁屏是否真的生效（已在锁屏也算成功）。
    deadline = time.monotonic() + _LOCK_VERIFY_TIMEOUT
    while True:
        if _process_name_running(_LOCK_PROCESS_NAME):
            return ExecResult(True, "已锁屏")
        if time.monotonic() >= deadline:
            return ExecResult(
                False,
                "锁屏未生效：锁屏进程未能启动，屏幕可能仍是解锁状态",
                "expected process: gtklock",
            )
        time.sleep(_LOCK_VERIFY_INTERVAL)


def camera_open(**_) -> ExecResult:
    """打开相机（snapshot）：注入桌面会话环境变量后后台启动。

    snapshot 是 GTK 相机应用（GNOME Camera 旧名），与 lock_screen 同理，
    Agent 以 root 运行时须带上会话环境（WAYLAND_DISPLAY/XDG_RUNTIME_DIR/
    DBUS…），否则 Wayland 客户端连不上会话直接失败。
    """
    env = _find_desktop_session_env()
    if env is None:
        return ExecResult(False, "未找到桌面会话（无法确定 Wayland 环境），打开相机失败")
    full_env = _gui_child_env(env)
    try:
        _spawn_detached(["snapshot"], full_env)
        return ExecResult(True, "已打开相机")
    except FileNotFoundError:
        return ExecResult(False, "命令不存在：snapshot")
    except OSError as e:
        return ExecResult(False, f"打开相机失败：{e}")


def camera_close(**_) -> ExecResult:
    """关闭相机：pkill 掉 snapshot 进程。

    必须用 -x（精确匹配进程名），不能用 -f（匹配完整命令行）：chromium
    的命令行含 v8_context_snapshot_data 参数，-f snapshot 会误杀桌面浏览器。
    """
    r = _run(["pkill", "-x", "snapshot"], timeout=3)
    if r.ok:
        return ExecResult(True, "已关闭相机")
    # pkill 返回 1 表示未找到正在运行的进程
    return ExecResult(False, "相机未在运行")


# ── 截图 ─────────────────────────────────────────────────────────────
#
# 系统里已有截图工具 bianbu-screenshot（grim 取图 + slurp 选区 + wl-copy 写
# Wayland 剪贴板）：产物目录与命名规则、剪贴板行为、取消与失败退出码都由它一处
# 实现，labwc 快捷键与桌面右键菜单用的是同一个二进制。这里只做一层薄封装——
# 注入桌面会话环境后调用它、把退出码翻成用户文案——bibit-intent 内部不再存第二份
# grim/slurp/wl-copy 逻辑，否则「语音截图存这里、按键截图存那里」两边必然漂移。

_SHOT_BINARIES = ("/usr/bin/bianbu-screenshot", "/usr/local/bin/bianbu-screenshot")
_SHOT_MODES = ("region", "full")
# slurp 的等待时长是「人在拖框」，正常几秒；到点按超时处理并回收进程组。
_SHOT_TIMEOUT = 35


def _screenshot_binary() -> str | None:
    """定位系统截图工具；未安装返回 None（老镜像可能没有这个包）。"""
    for path in _SHOT_BINARIES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGTERM 后仍不退出就 SIGKILL；只等直接子进程，不等它 fork 出去的守护。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_shot_tool(binary: str, mode: str, env: dict[str, str]) -> tuple[int, str, str]:
    """跑系统截图工具，返回 (退出码, stdout, stderr)。

    不用 ``cancellation.run_command``：那个实现把输出收在管道里，而截图工具内部用
    ``setsid wl-copy`` 起剪贴板守护，守护会继承交给它的 stdout/stderr，工具自身退出
    后管道仍不关 —— run_command 收尾时的 ``communicate()`` 永远等不到 EOF，实测
    region 超时后 ``/v1/runs`` 一直不返回（35s 已超时，请求仍挂在会话里）。
    输出落临时文件即可绕开管道继承，同时保留「超时/取消按进程组回收」的语义。
    """
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        process = subprocess.Popen(
            [binary, mode],
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
            env=env,
            start_new_session=True,
        )
        deadline = time.monotonic() + _SHOT_TIMEOUT
        returncode: int | None = None
        try:
            while True:
                code = process.poll()
                if code is not None:
                    returncode = code
                    break
                raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired([binary, mode], _SHOT_TIMEOUT)
                time.sleep(0.05)
        finally:
            if returncode is None:
                # 超时或被取消：slurp 还挂在选区上，必须连同进程组一起收掉。
                _kill_process_group(process)
        out_file.seek(0)
        err_file.seek(0)
        return (
            returncode,
            out_file.read().decode("utf-8", errors="replace"),
            err_file.read().decode("utf-8", errors="replace"),
        )


def _shot_output_path(stdout: str) -> str:
    """取出 PNG 产物路径，供回复文案展示。

    系统工具末行形如「已保存并复制到剪贴板：/path/x.png」，中英文各一套；
    这里不去猜分隔符语义，只按「行尾的绝对 png 路径」抽取，抽不到只影响文案。
    """
    for line in reversed((stdout or "").splitlines()):
        m = re.search(r"(/[^\s:：]+\.png)\s*$", line.strip())
        if m:
            return m.group(1)
    return ""


def screenshot(mode: str = "region", language: str | None = None, **_) -> ExecResult:
    """截图：选区/全屏 → 落盘 → 复制进 Wayland 剪贴板（委托 bianbu-screenshot）。

    退出码语义（工具约定）：0 成功 / 1 失败 / 2 用户取消 / 3 无 Wayland 会话 /
    4 缺少依赖。取消要按「成功但没做」返回（不产生文件、剪贴板不变），不能报失败。
    """
    session_env = _find_desktop_session_env()
    if session_env is None:
        return ExecResult(
            False,
            _reply(
                "未找到桌面会话（无法确定 Wayland 环境），截图失败",
                "No desktop session found, so no screenshot was taken.", language,
            ),
            detail_format="text",
        )
    binary = _screenshot_binary()
    if binary is None:
        return ExecResult(
            False,
            _reply(
                "端侧缺少截图工具 bianbu-screenshot，无法截图",
                "The screenshot tool is not installed on this device.", language,
            ),
            detail_format="text",
        )
    if mode not in _SHOT_MODES:
        mode = "region"
    env = _gui_child_env(session_env)
    try:
        returncode, stdout, stderr = _run_shot_tool(binary, mode, env)
    except FileNotFoundError:
        return ExecResult(False, _reply("命令不存在：bianbu-screenshot",
                                        "Command not found: bianbu-screenshot.", language),
                          detail_format="text")
    except subprocess.TimeoutExpired:
        return ExecResult(
            False,
            _reply(
                "截图超时：选区等待时间过长，本次未截图",
                "The screenshot timed out while waiting for a selection.", language,
            ),
            detail_format="text",
        )
    except OSError as e:
        return ExecResult(False, f"截图失败：{e}", detail_format="text")

    if returncode == 0:
        path = _shot_output_path(stdout)
        if path:
            return ExecResult(
                True,
                _reply(f"已截图并复制到剪贴板：{path}，可直接粘贴",
                       f"Screenshot saved and copied to the clipboard: {path}", language),
                detail_format="text",
            )
        return ExecResult(
            True,
            _reply("已截图并复制到剪贴板", "Screenshot copied to the clipboard.", language),
            detail_format="text",
        )
    if returncode == 2:
        # 用户按 Esc 取消选区：没有产生文件，也不算失败。
        return ExecResult(True, _reply("已取消截图。", "Screenshot cancelled.", language),
                          detail_format="text")
    if returncode == 3:
        return ExecResult(
            False,
            _reply(
                "未找到 Wayland 会话，无法截图（请在图形会话中操作）",
                "No Wayland session found, so no screenshot was taken.", language,
            ),
            detail_format="text",
        )
    if returncode == 4:
        return ExecResult(
            False,
            _reply(
                "端侧缺少截图依赖（grim / slurp / wl-clipboard），无法截图",
                "The screenshot dependencies (grim, slurp, wl-clipboard) are missing.", language,
            ),
            detail_format="text",
        )
    detail = (stderr or "").strip() or (stdout or "").strip()
    if "还在进行中" in detail or "still running" in detail:
        # 系统工具自己的单实例锁：上一次选区没结束（用户还没拖完框）。
        # 不把 detail 一起带上：成功结果里 detail 会被渲染成代码块，同一句话显示两遍。
        return ExecResult(
            True,
            _reply(
                "上一次截图还在进行中，请先完成或按 Esc 取消",
                "A previous capture is still running; finish it or press Esc.", language,
            ),
            detail_format="text",
        )
    return ExecResult(
        False,
        _reply("截图失败", "The screenshot failed.", language),
        detail,
        detail_format="text",
    )


# ── 拍照 ─────────────────────────────────────────────────────────────
#
# snapshot 没有命令行拍照入口（`snapshot --help` 只有 -d），D-Bus 也不导出
# take-picture 动作，但它把「拍摄照片」按钮背后的动作 win.take-picture 挂在
# 无障碍树上。执行层据此触发与手点按钮完全相同的动作，产物路径/EXIF 一致，
# 且不依赖窗口焦点（实测焦点在别的窗口时仍可拍照），也不独占 /dev/video*。
#
# 实现在 execution/camera_capture.py，必须用系统 python3 跑：bibit-intent 的
# venv（.venv，Python 3.14）没有 PyGObject，而 AT-SPI 需要 gi.repository.Atspi。

_CAPTURE_HELPER = Path(__file__).resolve().parent / "camera_capture.py"
_SYSTEM_PYTHON = "/usr/bin/python3"


def _capture_helper_path() -> Path:
    """端侧 helper 路径；部署布局与源码布局各试一次。"""
    candidates = [
        _CAPTURE_HELPER,
        Path("/opt/bibit-intent/src/bibit_intent/execution/camera_capture.py"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return _CAPTURE_HELPER


def _capture_call(action: str, session_env: dict, timeout: int = 25, args: list[str] | None = None) -> dict:
    """调用 helper 并解析其单行 JSON 输出。任何异常都收敛成 ok=False。"""
    helper = _capture_helper_path()
    if not helper.is_file():
        return {"ok": False, "reason": "helper_missing", "detail": str(helper)}
    env = _gui_child_env(session_env)
    cmd = [_SYSTEM_PYTHON, str(helper), action, *(args or [])]
    r = _run(cmd, timeout=timeout, env=env)
    if not r.ok:
        return {"ok": False, "reason": "helper_failed", "detail": (r.detail or r.message).strip()}
    line = next((ln for ln in (r.detail or "").splitlines() if ln.strip()), "")
    try:
        payload = json.loads(line)
    except (ValueError, TypeError):
        return {"ok": False, "reason": "helper_bad_output", "detail": line[:200]}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "helper_bad_output", "detail": line[:200]}
    return payload


def _capture_wait_ready(session_env: dict, timeout: float = 12.0) -> dict:
    """等到拍照动作可用（刚启动的 snapshot 需要几秒才把窗口树建好）。"""
    deadline = time.monotonic() + timeout
    state: dict = {"running": False}
    while True:
        state = _capture_call("status", session_env, timeout=20)
        if state.get("ok") and state.get("can_take"):
            return state
        if time.monotonic() >= deadline:
            return state
        time.sleep(0.5)


def _capture_reason_text(reason: str, state: dict, language: str | None) -> str:
    """把 helper 的失败原因翻译成用户可读文案。"""
    if reason == "camera_not_running":
        return _reply(
            "相机未能启动，无法拍照",
            "The camera app did not start, so no photo was taken.", language)
    if reason in ("take_action_unavailable", "camera_page_not_active"):
        return _reply(
            "相机没有停在拍摄页面，无法拍照",
            "The camera is not on the capture page, so no photo was taken.", language)
    if reason == "helper_error" and "gi" in str(state.get("detail", "")):
        return _reply(
            "端侧缺少相机拍照所需的无障碍组件（PyGObject/AT-SPI）",
            "The accessibility stack required for capture is unavailable.", language)
    if reason == "no_photo_written":
        return _reply(
            f"相机没有写出照片文件（相册目录：{state.get('pictures_dir', '未知')}）",
            f"No photo file was written (pictures dir: {state.get('pictures_dir', 'unknown')}).",
            language)
    if reason in ("permission_timeout", "awaiting_authorization"):
        return _reply(
            "还没收到摄像头授权，这次先不拍照了。请在弹出的窗口里点「允许」，"
            "然后再说一次「拍照」。",
            "Camera authorization is still pending, so no photo was taken. "
            "Please click “Allow” in the dialog and ask me to take a photo again.",
            language)
    if reason == "permission_denied":
        return _reply(
            "摄像头授权被拒绝，无法拍照。可在系统隐私设置里重新允许相机，"
            "再说一次「拍照」。",
            "Camera permission was denied, so no photo was taken. You can re-allow "
            "the camera in the privacy settings and ask again.",
            language)
    return _reply("拍照失败", "Failed to take a photo.", language)


def _await_camera_permission(session_env: dict, language: str | None, timeout: float = 20.0) -> ExecResult:
    """等用户在授权框上点「允许」（helper 会先弹一条桌面通知提示）。

    授权决定必须由用户下，这里不替点。等待受 edge 截止时间约束，超时就
    交给用户下一轮；返回 True 表示已授权可以直接补拍。
    """
    remaining = remaining_budget()
    if remaining is not None:
        timeout = min(timeout, max(2.0, remaining - 5.0))
    result = _capture_call(
        "await-permission",
        session_env,
        timeout=int(timeout) + 15,
        args=["--timeout", f"{timeout:.1f}", "--notify", "--require-dialog"],
    )
    if result.get("ok") and result.get("reason") == "granted":
        return ExecResult(True, "", detail_format="text")
    reason = str(result.get("reason", ""))
    if reason == "permission_timeout":
        return ExecResult(False, _capture_reason_text("permission_timeout", result, language))
    if reason == "denied" or (reason == "no_dialog" and result.get("permission") == "no"):
        # 「拒绝」会同时把弹窗收掉，所以要靠 store 里的 no 补判，否则用户看不到
        # 拒绝的明确说明，只看到一句含糊的「还没收到授权」。
        return ExecResult(False, _capture_reason_text("permission_denied", result, language))
    # no_dialog / helper 异常：授权框已消失，按当前状态再试一次拍照
    return ExecResult(True, "", detail_format="text")


def take_photo(language: str | None = None, **_) -> ExecResult:
    """调用 snapshot 的 take-picture 动作拍一张照片。

    相机没开就先开（与"拍照"的自然预期一致）；停在相册页时切回相机页再拍。
    首次使用要用户在系统授权框上点「允许」，此时先提示并等待，点完自动补拍。
    """
    session_env = _find_desktop_session_env()
    if session_env is None:
        return ExecResult(False, _reply(
            "未找到桌面会话（无法确定 Wayland 环境），拍照失败",
            "No desktop session found; cannot take a photo.", language))

    state = _capture_call("status", session_env)
    if not state.get("ok"):
        return ExecResult(False, _capture_reason_text(state.get("reason", ""), state, language))

    camera_was_open = bool(state.get("running"))
    if not camera_was_open:
        opened = camera_open()
        if not opened.ok:
            return ExecResult(False, opened.message, opened.detail)
        state = _capture_wait_ready(session_env)

    # 卡在授权框上就等用户点「允许」；只等一次，避免超时后反复空等。
    waited = False
    if state.get("awaiting_authorization"):
        wait = _await_camera_permission(session_env, language)
        if not wait.ok:
            return wait
        waited = True
        state = _capture_call("status", session_env)

    if state.get("ok") and not state.get("can_take"):
        _capture_call("to-camera", session_env)
        state = _capture_wait_ready(session_env, timeout=8.0)

    result = _capture_call("take", session_env)
    if not waited and not result.get("ok") and result.get("reason") == "awaiting_authorization":
        # status 之后授权框才弹出来（或刚被拒绝），补一次等待
        wait = _await_camera_permission(session_env, language)
        if not wait.ok:
            return wait
        result = _capture_call("take", session_env)

    if result.get("ok") and result.get("path"):
        path = str(result["path"])
        message = _reply(f"已拍照：{path}", f"Photo taken: {path}", language)
        if not camera_was_open:
            # 刚才是为拍照新开的相机，透明地说明一下，避免用户以为窗口是误弹的。
            message += _reply("（相机已打开）", " (camera opened)", language)
        return ExecResult(True, message, detail_format="text")

    reason = str(result.get("reason", ""))
    if reason in ("take_action_unavailable", "camera_page_not_active"):
        # 保险：切回相机页后再拍一次
        _capture_call("to-camera", session_env)
        retry = _capture_call("take", session_env)
        if retry.get("ok") and retry.get("path"):
            path = str(retry["path"])
            message = _reply(f"已拍照：{path}", f"Photo taken: {path}", language)
            if not camera_was_open:
                message += _reply("（相机已打开）", " (camera opened)", language)
            return ExecResult(True, message, detail_format="text")
        result = retry
        reason = str(result.get("reason", ""))

    return ExecResult(
        False,
        _capture_reason_text(reason, result, language),
        str(result.get("detail", "")),
        detail_format="text",
    )


# ── 有线网络（以太网）─────────────────────────────────────────────────
#
# config/tools.yaml 里的 system.cmd 写的是 `nmcli device connect eth0`，那只是
# 声明式占位：真实设备上有线网卡名五花八门（eth0 / end0 / enp3s0 / enP2p1s0），
# 写死必然失败。这里一律先探测再操作。
#
# 选卡策略（_pick_eth）：
#   1. 只认 nmcli 里 TYPE=ethernet 的设备，排除 lo / wifi / p2p
#   2. 优先有载波（网线插着，/sys/class/net/<夹>/carrier == 1）的
#   3. 载波相同则优先已连接的
# 这样单网卡设备行为不变，多网卡设备也能挑到真正在用的那块。

_ETH_STATE_CN = {
    "connected": "已连接",
    "disconnected": "已断开",
    "unavailable": "不可用（网线未插或被禁用）",
    "unmanaged": "未托管",
    "connecting": "正在连接",
    "deactivating": "正在断开",
}


def _nmcli_eth_devices() -> tuple[list[dict], ExecResult | None]:
    """列出所有有线网卡。返回 ([{name,state,conn,carrier}], 错误信息)。

    用 `nmcli -t` 的机器可读格式：字段以 ':' 分隔，STATE 是英文枚举
    （connected/unavailable/...），不受系统语言影响；`nmcli device status`
    的人读输出会被本地化成中文，不能拿来判断。
    """
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    if not r.ok:
        if "命令不存在" in r.message:
            return [], ExecResult(False, "命令不存在：nmcli（需安装 NetworkManager）")
        return [], r

    devs = []
    for line in r.detail.splitlines():
        # CONNECTION 名字本身可能含 ':'（如 "有线连接 2"），故限制切分次数
        parts = line.split(":", 3)
        if len(parts) < 3 or parts[1] != "ethernet":
            continue
        name = parts[0]
        carrier = ""
        try:
            carrier = Path(f"/sys/class/net/{name}/carrier").read_text().strip()
        except OSError:
            pass  # 设备刚拔出/不支持时读不到，按未知处理
        devs.append({
            "name": name,
            "state": parts[2],
            "conn": parts[3] if len(parts) > 3 else "",
            "carrier": carrier,
        })
    if not devs:
        return [], ExecResult(False, "未找到有线网卡（本机可能只有无线网卡）")
    return devs, None


def _pick_eth(devs: list[dict]) -> dict:
    """挑一块有线网卡：有载波优先，其次已连接优先。"""
    return sorted(
        devs,
        key=lambda d: (d["carrier"] != "1", d["state"] != "connected", d["name"]),
    )[0]


def _ssh_bound_device() -> str | None:
    """返回承载当前 SSH 会话的网卡名，没有则 None。

    SSH_CONNECTION = "<客户端IP> <客户端端口> <服务端IP> <服务端端口>"，取服务端
    IP 反查它绑在哪块网卡上。用途：eth_down 拒绝切断自己的远程通道，否则一条
    「关掉有线网」会让运维当场失联且无法恢复。本地图形会话下无此变量，返回 None。
    """
    conn = os.environ.get("SSH_CONNECTION", "").split()
    if len(conn) < 3:
        return None
    server_ip = conn[2]
    r = _run(["ip", "-o", "-4", "addr", "show"])
    if not r.ok:
        return None
    for line in r.detail.splitlines():
        parts = line.split()
        # 形如: "2: end0    inet 10.0.90.103/23 brd ... "
        if len(parts) >= 4 and parts[2] == "inet" and parts[3].split("/")[0] == server_ip:
            return parts[1]
    return None


def eth_up(**_) -> ExecResult:
    """启用有线网卡（nmcli device connect）。"""
    devs, err = _nmcli_eth_devices()
    if err:
        return err
    dev = _pick_eth(devs)

    if dev["state"] == "connected":
        return ExecResult(True, f"有线网络已经是连接状态（{dev['name']}）",
                          f"连接：{dev['conn']}")
    if dev["carrier"] == "0":
        return ExecResult(False, f"{dev['name']} 没有检测到网线，请先插好网线")

    r = _run(["nmcli", "device", "connect", dev["name"]], timeout=30)
    if r.ok:
        return ExecResult(True, f"有线网络已启用（{dev['name']}）", r.detail, detail_format=r.detail_format)
    return ExecResult(False, f"启用有线网络失败（{dev['name']}）", r.detail or r.message, detail_format=r.detail_format)


def eth_down(**_) -> ExecResult:
    """禁用有线网卡（nmcli device disconnect）。

    自保护：若选中的网卡正承载当前 SSH 会话，拒绝执行。远程调试时切断它等于
    自断退路，且断开后无法再连回来修。
    """
    devs, err = _nmcli_eth_devices()
    if err:
        return err
    dev = _pick_eth(devs)

    ssh_dev = _ssh_bound_device()
    if ssh_dev and ssh_dev == dev["name"]:
        return ExecResult(
            False,
            f"拒绝执行：{dev['name']} 正承载当前远程连接，断开会导致失联",
            "如确需断开，请在设备本地操作，或先切到其他网络通道",
        )

    if dev["state"] not in ("connected", "connecting"):
        return ExecResult(True, f"有线网络本来就没连接（{dev['name']}）",
                          f"状态：{_ETH_STATE_CN.get(dev['state'], dev['state'])}")

    r = _run(["nmcli", "device", "disconnect", dev["name"]], timeout=30)
    if r.ok:
        return ExecResult(True, f"有线网络已断开（{dev['name']}）", r.detail, detail_format=r.detail_format)
    return ExecResult(False, f"断开有线网络失败（{dev['name']}）", r.detail or r.message, detail_format=r.detail_format)


def eth_status(**_) -> ExecResult:
    """查看有线网络状态：连接状态 + 网线是否插着 + 连接名。"""
    devs, err = _nmcli_eth_devices()
    if err:
        return err

    lines = []
    for d in devs:
        state = _ETH_STATE_CN.get(d["state"], d["state"])
        cable = {"1": "网线已插", "0": "网线未插"}.get(d["carrier"], "网线状态未知")
        part = f"{d['name']}：{state}，{cable}"
        if d["conn"]:
            part += f"，连接名 {d['conn']}"
        lines.append(part)

    dev = _pick_eth(devs)
    if dev["state"] == "connected":
        head = f"有线网络已连接（{dev['name']}）"
    elif dev["carrier"] == "0":
        head = f"有线网络未连接，{dev['name']} 没插网线"
    else:
        head = (f"有线网络未连接（{dev['name']}："
                f"{_ETH_STATE_CN.get(dev['state'], dev['state'])}）")
    return ExecResult(True, head, "\n".join(lines))


def eth_ip(**_) -> ExecResult:
    """查看有线网卡的 IPv4 地址与网关。"""
    devs, err = _nmcli_eth_devices()
    if err:
        return err
    dev = _pick_eth(devs)

    if dev["state"] != "connected":
        return ExecResult(
            False,
            f"有线网络未连接（{dev['name']}），拿不到 IP",
            f"状态：{_ETH_STATE_CN.get(dev['state'], dev['state'])}"
            + ("，网线未插" if dev["carrier"] == "0" else ""),
        )

    r = _run(["nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY",
              "device", "show", dev["name"]])
    if not r.ok:
        return ExecResult(False, f"查询 {dev['name']} 的 IP 失败", r.detail or r.message, detail_format=r.detail_format)

    addrs, gateway = [], ""
    for line in r.detail.splitlines():
        key, _, val = line.partition(":")
        val = val.strip()
        if not val or val == "--":
            continue
        if key.startswith("IP4.ADDRESS"):
            addrs.append(val)
        elif key == "IP4.GATEWAY":
            gateway = val

    if not addrs:
        return ExecResult(False, f"有线网卡 {dev['name']} 没有取到 IPv4 地址")
    detail = f"{dev['name']}：{'、'.join(addrs)}"
    if gateway:
        detail += f"\n网关：{gateway}"
    return ExecResult(True, f"有线网络 IP：{addrs[0].split('/')[0]}", detail)


# ── 危险操作（电源）─────────────────────────────────────────────────
# 关机/重启/睡眠统一走 systemd-logind（systemctl 即 logind 的 D-Bus 客户端），
# 由 polkit 决定是否放行。桌面会话里 allow_active=yes，但 MCP 工具进程跑在
# bianbu-agent-runtime 系统服务里、没有 seat，会得到 "Interactive
# authentication required"；deploy/50-bianbu-power.rules 为 sudo 组放行这几个
# 动作，否则云端/MCP 路径的关机、重启、休眠全部不可用。

_POWER_ACTIONS = {
    "poweroff": "正在关机...",
    "suspend": "正在进入睡眠...",
    "hibernate": "正在进入休眠...",
}


def _power_action(action: str) -> ExecResult:
    """执行 logind 电源动作；被 polkit 拒绝时给出可读原因而不是裸退出码。"""
    r = _run(["systemctl", action], timeout=20)
    if r.ok:
        return ExecResult(True, _POWER_ACTIONS[action])
    return _power_failure(action, r)


def _power_failure(action: str, r: ExecResult) -> ExecResult:
    detail = (r.detail or "") + " " + (r.message or "")
    if "interactive authentication required" in detail.casefold() \
            or "access denied" in detail.casefold():
        return ExecResult(
            False,
            f"系统拒绝了本次操作（{action} 需要授权），未执行",
            r.detail,
            detail_format=r.detail_format,
        )
    return r


def reboot(**_) -> ExecResult:
    """重启：真正执行由执行层在二次确认后调用。"""
    result = _run(["systemctl", "reboot", "--when=+1min"], timeout=20)
    if not result.ok:
        return _power_failure("reboot", result)
    return ExecResult(
        True,
        "重启指令已发送，设备将在约一分钟后重启，连接将断开。",
        '{"device_action":"reboot","status":"accepted","expected_disconnect":true}',
        detail_format="internal",
    )


def shutdown(**_) -> ExecResult:
    """关机（poweroff）：真正执行由执行层在二次确认后调用。"""
    return _power_action("poweroff")


def _power_states() -> list[str]:
    """读取 /sys/power/state 支持的状态；不可读时返回空列表（视为不支持）。"""
    try:
        with open("/sys/power/state", "r", encoding="ascii") as fh:
            return fh.read().split()
    except OSError:
        return []


def sleep(**_) -> ExecResult:
    """睡眠/待机/休眠。

    本机 /sys/power/state 只支持 freeze/mem（无 disk、无 swap）时只能
    suspend-to-RAM；内核支持 disk 时按用户措辞走 hibernate，两者都
    不可用则明确报错，不要谎称已休眠。
    """
    states = _power_states()
    if "disk" in states:
        return _power_action("hibernate")
    if states:
        return _power_action("suspend")
    return ExecResult(
        False,
        "本机不支持睡眠或休眠",
        "/sys/power/state 不可读或不支持任何睡眠状态",
    )


# ── 本地文件检索 ─────────────────────────────────────────────────────

def _filesearch_port() -> int:
    """bibit-filesearch 服务端口：环境变量优先，否则按 uid 派生。

    与 routing/router.py 的端口派生保持一致（对齐服务端 _port_for_uid）。
    """
    env_port = os.environ.get("BIBIT_FILESEARCH_PORT")
    if env_port:
        return int(env_port)
    return 18700 if os.getuid() < 1000 else 18700 + (os.getuid() - 1000)


def local_file_search(query: str = "", *, port: int | None = None, **_) -> ExecResult:
    """两个搜索入口共用真实结果契约，保留旧文字消费者。"""
    from bibit_intent.file_results import FileResultError, normalize_results

    query = str(query or "").strip()
    if not query:
        # 缺参而不是"服务坏了"：明确告诉调用方该补什么，避免模型以为检索服务挂了。
        return ExecResult(
            False, "本地搜索需要查询内容。",
            need_param="query",
        )
    try:
        import httpx
        response = httpx.post(
            f"http://127.0.0.1:{port or _filesearch_port()}/api/search",
            json={"query": query, "page": 1, "pageSize": 10}, timeout=15, trust_env=False,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        # 只有真·不可达（连接失败/超时/HTTP 错误/JSON 坏）才是"服务不可用"。
        return ExecResult(False, f"本地文件检索服务不可用（{type(exc).__name__}），请确认 bibit-filesearch 已启动。")
    # 走到这里说明服务活着并回了 200，code!=0 是业务层拒绝（典型是 query
    # 分词后关键词为空，服务端回 code=1001「query 不能为空」）。原样透出服务端
    # 的 message 并标记 need_param，让调用方知道"换个更具体的词重试"，
    # 而不是把它误当成服务故障转去绕路（如改开终端扫盘）。
    if not isinstance(body, dict) or body.get("code", 0) != 0:
        reason = ""
        if isinstance(body, dict):
            reason = str(body.get("message") or "").strip()
        return ExecResult(
            False,
            f"本地检索已拒绝该查询：{reason or 'query 无效'}。请改用更具体的词"
            "（文件名、主题、扩展名，如 合同 / 周报 / pdf），不要用 * 或「所有文件」这类泛词。",
            need_param="query",
        )
    try:
        result = normalize_results(body.get("data"), port=port)
    except FileResultError as exc:
        return ExecResult(False, str(exc))
    entries = result["entries"]
    lines = [f"本地检索结果（按相关度排序，第 1 位最相关）：{result['total_count']} 个候选文件（服务 Top-K 范围）"]
    for index, item in enumerate(entries, 1):
        score = f"（相对匹配度 {item['score']:.0%}）" if item["score"] is not None else "（未提供匹配度）"
        lines.append(f"第 {index} 位：{item['name']}{score} — {item['path']}")
    if not entries:
        lines.append("本地没有找到当前授权下可用的相关文件。")
    else:
        lines.append("可点击文件名打开，或选择文件后总结正文；也可说「读取本地文件 <路径>」。")
    return ExecResult(bool(entries), "\n\n".join(lines), json.dumps(result, ensure_ascii=False))


def _default_open_argv(file_path: Path) -> list[str] | None:
    """把本地文件解析成宿主桌面默认应用的启动 argv。

    xdg-open 是短生命周期包装脚本，经 systemd-run --service-type=exec 启动后
    往往刚 fork 出真实 GUI 进程就退出，transient service 随之结束并清理 cgroup，
    实际应用窗口不会留下。这里直接用 xdg-mime 查默认 .desktop，再展开 Exec 的
    %U/%F 字段码，把文件路径传给真实应用（如 libreoffice --impress <path>）。
    """
    try:
        mime = subprocess.run(
            ["xdg-mime", "query", "filetype", str(file_path)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        desktop_id = subprocess.run(
            ["xdg-mime", "query", "default", mime],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not mime or not desktop_id:
        return None

    for base in _desktop_search_dirs():
        desktop_path = Path(base) / desktop_id
        if not desktop_path.exists():
            continue
        info = _parse_desktop_file(str(desktop_path))
        if not info:
            continue
        try:
            tokens = shlex.split(info["exec"])
        except ValueError:
            return None
        argv: list[str] = []
        for token in tokens:
            if token in {"%u", "%U", "%f", "%F"}:
                argv.append(str(file_path))
            elif len(token) == 2 and token[0] == "%" and token[1] in _DESKTOP_FIELD_CODES:
                continue
            else:
                argv.append(token)
        return argv or None
    return None


def _launch_desktop_argv(
    argv: list[str],
    label: str,
    language: str | None,
    zh_done: str,
    en_done: str,
) -> ExecResult:
    """把 argv 通过 worker/edge/本地三条桌面通道启动，成功后回双语文案。

    open_file / open_folder 共用：构造好 argv 后，剩下的会话环境探测、沙箱
    桥接、用户级 systemd-run 与本地 Popen 兜底完全一致，避免两处复制。
    """
    session_env = _find_desktop_session_env()
    worker_role = os.environ.get("BIANBU_RUNTIME_ROLE") == "agent-worker"
    if worker_role:
        if not _host_worker_desktop_launch():
            return ExecResult(
                False,
                "当前会话在沙箱内，无法在桌面打开",
                escalate="needs_host_channel",
            )
        result = _launch_host_desktop(argv, label, session_env)
        if not result.ok:
            return result
        return ExecResult(True, _reply(zh_done, en_done, language))

    if _edge_service_desktop_launch():
        if _launch_via_user_manager(argv, session_env):
            return ExecResult(True, _reply(zh_done, en_done, language))

    env = _gui_child_env(session_env)
    try:
        # 「执行」入口的子进程（CLI-Anything 等）：不阻塞等待，但记一条兜底回收
        # 线程，进程退出后立刻 wait 掉，避免留下 zombie。
        _spawn_detached(argv, env)
    except FileNotFoundError:
        return ExecResult(False, f"命令不存在：{argv[0]}")
    except PermissionError as exc:
        return ExecResult(False, f"无权限打开：{argv[0]}", str(exc))
    except OSError as exc:
        return ExecResult(False, f"打开失败：{exc}")
    return ExecResult(True, _reply(zh_done, en_done, language))


def open_file(path: str, language: str | None = None, **_) -> ExecResult:
    """用宿主桌面的默认应用打开本地文件。

    这是「打开本地文件/文档」的专用入口，和 launch_app（只启动应用、不携带
    文件路径）以及 CLI-Anything 的 document open（导入成可编辑工程）分开。
    file_search 返回路径后、或用户给出显式文件路径时都应调用这里，避免模型
    回退到 terminal 直接跑 libreoffice/xdg-open，把 GUI 进程落到 Worker 沙箱
    或没有 DISPLAY 的后台命名空间里。
    """
    path = str(path or "").strip()
    if not path:
        return ExecResult(False, "缺少文件路径")
    file_path = Path(path).expanduser()
    if not file_path.is_absolute():
        return ExecResult(False, "文件路径必须是绝对路径", need_param="path")
    if not file_path.exists():
        return ExecResult(False, f"文件不存在: {file_path}")
    if not file_path.is_file():
        return ExecResult(False, f"目标不是普通文件: {file_path}")

    argv = _default_open_argv(file_path)
    if argv is None:
        opener = shutil.which("xdg-open")
        if opener:
            argv = [opener, str(file_path)]
        else:
            gio = shutil.which("gio")
            if not gio:
                return ExecResult(False, "未找到 xdg-open/gio，无法打开文件")
            argv = [gio, "open", str(file_path)]

    return _launch_desktop_argv(
        argv,
        f"文件 {file_path.name}",
        language,
        f"已打开文件: {file_path}",
        f"Opened file: {file_path}",
    )


def _file_manager1_show_folders(target: str, session_env: dict | None) -> bool:
    """通过 freedesktop FileManager1 D-Bus 打开目录/位置 URI，返回是否受理成功。

    ``gio open`` 走 inode/directory 的默认 handler；端侧默认 handler 可能被
    codium 等非文件管理器抢走，导致「打开文件夹」用编辑器打开甚至无窗口。
    FileManager1 只由真正的文件管理器（pcmanfm-qt / nautilus / dolphin …）
    注册，能稳定弹出文件管理器窗口，绕过默认 handler 的错配。
    """
    busctl = shutil.which("busctl")
    if not busctl:
        return False
    uri = target if "://" in target else Path(target).as_uri()
    cmd = [
        busctl, "--user", "call",
        "org.freedesktop.FileManager1",
        "/org/freedesktop/FileManager1",
        "org.freedesktop.FileManager1", "ShowFolders",
        "ass", "1", uri, "",
    ]
    env = _gui_child_env(session_env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _open_dir_argv(dir_path: Path) -> list[str] | None:
    """目录打开的回退 argv：gio open（走默认 handler）→ xdg-open。

    FileManager1 不可用时的兜底；仍经 freedesktop/GIO 默认 handler 分发，
    兼容 nautilus / dolphin / thunar / pcmanfm-qt 等文件管理器。默认 handler
    被错配（如 codium 抢走 inode/directory）时本回退可能打开非文件管理器。
    """
    gio = shutil.which("gio")
    if gio:
        return [gio, "open", str(dir_path)]
    opener = shutil.which("xdg-open")
    if opener:
        return [opener, str(dir_path)]
    return None


_DIR_LOCATE_MAXDEPTH = 5
_DIR_LOCATE_LIMIT = 10
# find 剪枝的目录名：隐藏目录用 '.*' 一次覆盖（.git/.cache/.local…），
# 再补常见非隐藏的大目录，避免陷入其庞大子树导致定位变慢。
_DIR_LOCATE_PRUNED_NAMES = (".*", "node_modules", "__pycache__", "venv")


def _dir_locate_roots() -> list[Path]:
    """受限定位根目录：home + 常见挂载点，只扫用户可读位置，不扫全盘。"""
    roots = [Path.home()]
    for mount in ("/mnt", "/media"):
        mp = Path(mount)
        if mp.exists():
            roots.append(mp)
    return roots


def locate_directories(
    name: str,
    limit: int = _DIR_LOCATE_LIMIT,
    roots: list[Path] | None = None,
) -> list[str]:
    """在受限根目录按名称定位目录，短路径（更浅）优先。

    用 find -prune 剪掉隐藏/缓存目录后一次性列出目录，再在内存做名称子串
    匹配，避免把用户输入拼进 find 的 -name glob（通配符注入）；中文按字面
    子串匹配，ASCII 走 casefold 不区分大小写。``roots`` 显式给出时只在这些
    目录内定位（「打开桌面的 X 文件夹」的 scoped 定位）。
    """
    name = str(name or "").strip()
    if not name:
        return []
    needle = name.casefold()
    found: list[str] = []
    for root in (roots if roots is not None else _dir_locate_roots()):
        if not root.exists():
            continue
        cmd = ["find", str(root), "-maxdepth", str(_DIR_LOCATE_MAXDEPTH)]
        for pruned in _DIR_LOCATE_PRUNED_NAMES:
            cmd += ["-name", pruned, "-prune", "-o"]
        cmd += ["-type", "d", "-print"]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                encoding=sys.getfilesystemencoding(), errors="surrogateescape",
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            line = line.strip()
            if line and needle in Path(line).name.casefold():
                found.append(line)
    found = sorted(set(found), key=lambda p: (len(p), p))
    return found[:limit]


def _open_dir_path(dir_path: Path, language: str | None) -> ExecResult:
    """校验并打开一个真实目录路径（open_folder / 快捷方式目标共用）。"""
    try:
        dir_path = dir_path.resolve(strict=True)
    except OSError:
        return ExecResult(False, f"目录不存在: {dir_path}", escalate="dir_not_found")
    if not dir_path.is_dir():
        return ExecResult(False, f"目标不是目录: {dir_path}", escalate="dir_not_found")

    # 优先走 FileManager1（真正的文件管理器注册的 D-Bus 接口），避免 gio
    # open 被默认 handler 错配（codium 抢走 inode/directory）导致无窗口。
    session_env = _find_desktop_session_env()
    if _file_manager1_show_folders(str(dir_path), session_env):
        return ExecResult(
            True,
            _reply(f"已打开文件夹: {dir_path}", f"Opened folder: {dir_path}", language),
        )

    argv = _open_dir_argv(dir_path)
    if argv is None:
        return ExecResult(False, "未找到 gio/xdg-open，无法打开目录")

    return _launch_desktop_argv(
        argv,
        f"目录 {dir_path.name}",
        language,
        f"已打开文件夹: {dir_path}",
        f"Opened folder: {dir_path}",
    )


def _open_shortcut_target(target: str, label: str, language: str | None) -> ExecResult:
    """打开桌面占位快捷方式解析出的目标：gvfs URI 走 FileManager1/gio，路径走 _open_dir_path。"""
    if "://" in target:
        # 优先 FileManager1，避免 gvfs URI 被默认 handler 错配（同上）。
        session_env = _find_desktop_session_env()
        if _file_manager1_show_folders(target, session_env):
            return ExecResult(
                True,
                _reply(f"已打开: {target}", f"Opened: {target}", language),
            )
        gio = shutil.which("gio")
        if gio:
            return _launch_desktop_argv(
                [gio, "open", target], f"位置 {target}", language,
                f"已打开: {target}", f"Opened: {target}",
            )
        opener = shutil.which("xdg-open")
        if opener:
            return _launch_desktop_argv(
                [opener, target], f"位置 {target}", language,
                f"已打开: {target}", f"Opened: {target}",
            )
        return ExecResult(False, f"未找到 gio/xdg-open，无法打开位置: {target}")
    return _open_dir_path(Path(target), language)


def _resolve_scoped_base(loc: str) -> Path | None:
    """把位置限定词解析成 scoped 定位的基目录。

    支持三层来源：XDG 别名（桌面/文档/下载/主目录…）、当前用户主目录名
    （user-home 快捷方式，如 bianbu → /home/bianbu）、以及任意目录名（全局
    受限定位唯一命中时）。gvfs 虚拟位置（计算机/回收站）不能作为嵌套定位的
    基目录，返回 None 由调用方回退/上云。
    """
    loc = str(loc or "").strip()
    if not loc:
        return None
    name = re.sub(r"(文件夹|目录)$", "", loc).strip() or loc
    resolved = resolve_directory(loc)
    if resolved is None:
        resolved = resolve_directory(name)
    if resolved:
        return Path(resolved)
    shortcut = resolve_desktop_shortcut(name)
    if shortcut and "://" not in shortcut:
        return Path(shortcut)
    matches = locate_directories(name)
    if len(matches) == 1:
        return Path(matches[0])
    return None


def open_folder(
    path: str,
    location: str | None = None,
    language: str | None = None,
    **_
) -> ExecResult:
    """用宿主文件管理器打开本地目录。

    与 open_file 对称：open_file 只开文件、本函数只开目录。支持 XDG 标准
    位置别名（桌面/文档/下载/主目录/根目录，中英文均可）与显式绝对路径
    （~/… 会展开），以及「位置 + 的/里 + 名称」的 scoped 定位（打开桌面的
    bianbu 文件夹），避免「打开桌面 / 打开 /home/xxx 文件夹」被 launch_app
    或 open_file 误吃、被迫上云。``location`` 是位置限定词（桌面/文档/…），
    与 ``path`` 里的名称组合做目录定位。
    """
    raw = str(path or "").strip()
    if not raw:
        return ExecResult(False, "缺少目录路径", need_param="path")

    # 沙箱 worker 的宿主文件系统不可见（home//mnt//media 故意不挂载），
    # 且打开文件夹本身要在宿主桌面弹文件管理器窗口。这里若在沙箱内做目录
    # 定位/存在性检查，只会得到误导性的「没有找到 / 目录不存在」；与启动层
    # _launch_desktop_argv 的宿主桥接保持一致，直接升级到 host 通道。
    if is_sandboxed():
        return ExecResult(
            False,
            _reply(
                "打开文件夹需要在宿主桌面执行，当前会话在沙箱内，本次未执行，已转交宿主处理。",
                "Opening a folder must run on the host desktop; this session is "
                "sandboxed, so nothing was executed and the request was handed to "
                "the host.",
                language,
            ),
            escalate="needs_host_channel",
        )

    loc = str(location or "").strip()

    # 桌面占位快捷方式（user-home/computer/trash-can 的显示名），仅在带位置
    # 限定词（「打开桌面的 bianbu 文件夹」）时生效：当前用户名/主目录别名 →
    # 主目录，「计算机/回收站」→ gvfs URI。用户名动态取自 Path.home().name，
    # 不硬编码。裸「打开 X 文件夹」不做此映射，仍走下方定位/上云。
    if loc:
        shortcut = resolve_desktop_shortcut(raw)
        if shortcut is not None:
            return _open_shortcut_target(shortcut, raw, language)

    resolved = resolve_directory(raw)
    if resolved is not None:
        dir_path = Path(resolved)
    else:
        # 只允许 ~ / ~/ 展开当前用户主目录；~otheruser 会越权打开他人主目录。
        if raw.startswith("~") and raw != "~" and not raw.startswith("~/"):
            return ExecResult(False, f"不支持打开其他用户目录: {raw}", need_param="path")
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            dir_path = expanded
        else:
            # 模糊名称 → 受限目录定位（第二期）。有位置限定词时只在该位置
            # 目录内定位，否则退回 home + /mnt + /media 全局受限定位。
            roots = None
            if loc:
                base = _resolve_scoped_base(loc)
                if base:
                    roots = [Path(base)]
            matches = locate_directories(raw, roots=roots)
            if not matches:
                return ExecResult(
                    False,
                    f"没有找到包含「{raw}」的目录",
                    escalate="dir_not_found",
                )
            if len(matches) == 1:
                dir_path = Path(matches[0])
            else:
                numbered = "\n".join(f"{i}. {m}" for i, m in enumerate(matches, 1))
                return ExecResult(
                    False,
                    f"找到多个匹配目录，请回复序号或完整路径：\n{numbered}",
                    need_param="path",
                    choices=matches,
                )

    return _open_dir_path(dir_path, language)


# ── 分发表 ───────────────────────────────────────────────────────────
# 工具名 → 执行函数
HANDLERS = {
    "get_system_time": get_system_time,
    "set_system_time": set_system_time,
    "get_vol_status": get_volume,
    "set_volume": set_volume,
    "adjust_volume": adjust_volume,
    "mute": mute,
    "audio_output_switch": audio_output_switch,
    "audio_input_volume": audio_input_volume,
    "audio_input_switch": audio_input_switch,
    "audio_input_mute": audio_input_mute,
    "get_display_light": get_brightness,
    "set_brightness": set_brightness,
    "adjust_brightness": adjust_brightness,
    "get_system_info": get_system_info,
    "launch_app": launch_app,
    "launch_apps": launch_apps,
    "close_app": close_app,
    "close_apps": close_apps,
    "list_apps": list_apps,
    "lock_screen": lock_screen,
    "eth_up": eth_up,
    "eth_down": eth_down,
    "eth_status": eth_status,
    "eth_ip": eth_ip,
    "reboot": reboot,
    "shutdown": shutdown,
    "sleep": sleep,
    "file_search": local_file_search,
    "open_file": open_file,
    "open_folder": open_folder,
    "camera_open": camera_open,
    "camera_close": camera_close,
    "take_photo": take_photo,
    "screenshot": screenshot,
}

# 屏幕显示（分辨率/刷新率/旋转/缩放/外接屏模式）实现在 tools/display.py，
# 它复用本模块的 ExecResult / _run / _find_desktop_session_env，因此两模块
# 互相引用。两种导入顺序都要能工作：
#   先 import bibit_intent.system_tools  → 走下面这句，display 反向 import 本模块时
#                             上面的helpers 已定义好，正常完成
#   先 import bibit_intent.display_tools → 它 import 本模块，本模块执行到这里时 display
#                             还没建完（HANDLERS 未定义）→ ImportError，
#                             跳过；display 收尾时会把自己的 handlers
#                             注册进本模块的 HANDLERS（见其文件末尾）
try:  # noqa: SIM105
    from .display_tools import HANDLERS as _DISPLAY_HANDLERS  # noqa: E402

    HANDLERS.update(_DISPLAY_HANDLERS)
except ImportError:
    pass

try:
    from .wallpaper_tools import HANDLERS as _WALLPAPER_HANDLERS

    HANDLERS.update(_WALLPAPER_HANDLERS)
except ImportError:
    pass

try:
    from .locale_tools import HANDLERS as _LOCALE_HANDLERS

    HANDLERS.update(_LOCALE_HANDLERS)
except ImportError:
    pass

try:  # noqa: SIM105
    from .window_tools import HANDLERS as _WINDOW_HANDLERS  # noqa: E402

    HANDLERS.update(_WINDOW_HANDLERS)
except ImportError:
    pass
