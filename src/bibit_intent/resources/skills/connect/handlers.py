"""
skills/connect/handlers.py — 「连接 <名字>」的动作域判域与编排

用户只给一个名字（"连接JDSK" / "连接iKF-Ultra"）时，不知道那到底是 WiFi 网络
还是蓝牙设备。规则层不猜域，本工具用**环境事实**判：

  ① WiFi 侧（便宜）：NetworkManager 当前可见的 SSID（板机实测 0.06s / 113 个）
     + 本机已保存的连接配置
  ② 蓝牙侧（较便宜）：bluez 已知/已配对设备（`devices` / `devices Paired`，约 0.7s）
  ③ 两侧都没有：让蓝牙侧做一次完整发现（缓存 + 发现窗口，10.4s / 55 台），
     WiFi 侧重扫一次复核（约 5s），然后**如实报告两侧证据** —— 不猜、不硬连

分支（对照 docs/intent-ambiguity-survey.md §2.3「判别式澄清」）：
  A 只有一边有           → 连那一边，并说明是按哪个域连的
  B 两边都有 / 同侧多候选 → 反问一句（给具体选项），不猜
  C 两边都没有           → 报告两侧证据 + 下一步建议（不把"名字不是 WiFi 名"
                           说成"密码或信号问题"——那是真机事故的根因）

为什么能靠 WiFi 侧确定就不开蓝牙扫描：板机实测（K3 同适配器）WiFi 可见列表
0.06s，而蓝牙发现窗口 10.4s，且会被 blueman 的常驻发现会话影响（结果可能不完整）。
执行层给"可能改设备"的工具留了 40s 最小预算，本工具的耗时上界要控制在这个窗口内。
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bibit_intent.execution.cancellation import raise_if_cancelled


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    detail_format: Literal["text", "markdown"] = "markdown"


#: 兄弟 Skill 目录（skills/<名>/handlers.py）
_SKILLS_DIR = Path(__file__).resolve().parent.parent


def _sibling(skill: str):
    """取兄弟 Skill 的 handlers 模块。

    skills/ 不是 Python 包（`import bibit_intent.resources.skills.wifi` 会
    ModuleNotFoundError），skill_loader 是以 `_skill_<skill名>` 把模块放进
    sys.modules 的。本模块被独立加载（离线测试、直接调用）时那里可能还没有，
    按文件路径兜底加载一次并登记同名模块，避免重复执行。
    """
    mod_name = f"_skill_{skill}"
    mod = sys.modules.get(mod_name)
    if mod is not None:
        return mod
    path = _SKILLS_DIR / skill / "handlers.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


#: 泛指词：不是真实名字，用户还没说清要连什么。拿它去连必然失败，
#: 应该反问而不执行（与 wifi/bluetooth 两个 Skill 的类别词过滤保持一致）。
_CATEGORY_WORDS = {
    "蓝牙", "bluetooth", "设备", "device", "wifi", "WiFi", "WIFI", "无线", "网络",
    "耳机", "音响", "音箱", "鼠标", "键盘", "手柄", "这个", "那个", "刚才", "东西",
}


def _is_category(name: str) -> bool:
    return name.strip().lower() in {w.lower() for w in _CATEGORY_WORDS}


def _tail(items: list[str], limit: int = 5) -> str:
    return "、".join(items[:limit])


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _ssid_matches(name: str, ssids: list[str]) -> list[str]:
    """SSID 匹配：精确同名优先（有就只回它），否则回包含该名字的（去重、保序）。

    先精确再包含，是因为 K3 实测「JDSK」能同时子串命中 JDSK / JDSK_Test /
    JDSK_OFC / JDSK_Guest 等好几种网络：同名网络通常有多个 BSSID（已去重），
    而不同名的那几个是**不同网络**，不能替用户挑一个。
    """
    key = name.strip().lower()
    exact = [s for s in ssids if s.strip().lower() == key]
    if exact:
        return exact[:1]
    return [s for s in ssids if key in s.lower()]


def connect_any(device: str = "", **_) -> ExecResult:
    """按名字连接设备：两侧都查，哪边有连哪边（见模块 docstring）。"""
    name = (device or "").strip()
    if not name or _is_category(name):
        return ExecResult(
            False,
            "请告诉我要连接的设备名",
            "例如：连接 AirPods  /  连接 JDSK 的wifi  /  连接蓝牙耳机",
        )
    raise_if_cancelled()

    try:
        wifi = _sibling("wifi")
        bt = _sibling("bluetooth")
    except Exception as e:  # noqa: BLE001 — 加载失败也要给人话，不能抛给上层
        return ExecResult(False, "连接失败：本地连接能力不可用", str(e))

    # ── ① WiFi 侧 + ② 蓝牙侧（都只读缓存，不开扫描） ─────────────────────
    ssids = _ssid_matches(name, wifi.visible_ssids())
    saved = wifi.saved_profile_name(name) if not ssids else None
    paired = bt.paired_device_matches(name)
    known = bt.known_device_matches(name)
    bt_names = sorted({dname for _, dname in (paired + known)})

    # ── 同侧多个候选 → 判别式反问，不替用户挑 ────────────────────────────
    if len(ssids) > 1:
        return ExecResult(
            False,
            f"有多个 WiFi 匹配「{name}」，要连哪一个？",
            _tail(ssids) + f"\n请说完整网络名，例如：连接 {ssids[0]}",
        )
    if len(bt_names) > 1:
        return ExecResult(
            False,
            "匹配到多个蓝牙设备，要连哪一个？",
            _tail(bt_names) + f"\n例如：连接蓝牙 {bt_names[0]}",
        )

    # ── 分支 B：两侧都有 → 反问域，不猜 ──────────────────────────────────
    if (ssids or saved) and bt_names:
        wifi_label = ssids[0] if ssids else saved
        return ExecResult(
            False,
            f"「{name}」在 WiFi 和蓝牙里都有，要连哪一个？",
            _join(
                f"WiFi：{wifi_label}",
                f"蓝牙：{bt_names[0]}",
                f"要 WiFi 说「连接 {wifi_label} 的wifi」；要蓝牙说「连接蓝牙 {name}」",
            ),
        )

    # ── 分支 A：只有一侧有 → 连它，并说明按哪个域连的 ────────────────────
    if ssids or saved:
        target = ssids[0] if ssids else saved
        note = (f"（按 WiFi 连接）本机可见这个网络：{target}" if ssids
                else f"（按 WiFi 连接）本机保存过这个网络：{target}")
        r = wifi.wifi_connect(ssid=target)
        return ExecResult(r.ok, r.message, _join(note, r.detail), detail_format=getattr(r, "detail_format", "markdown"))
    if bt_names:
        r = bt.bt_connect(device=name)
        evidence = "已配对" if paired else "系统里有记录"
        return ExecResult(
            r.ok, r.message,
            _join(f"（按蓝牙连接）{evidence}的设备：{bt_names[0]}", r.detail),
            detail_format=getattr(r, "detail_format", "markdown"),
        )

    # ── 分支 C：两侧都没有这个确切名字 ───────────────────────────────────
    # 蓝牙侧做完整发现：设备正在广播就顺带 pair/trust/connect 连上（BLE 随机地址
    # 只有在发现窗口内才连得上，见 bluetooth/handlers._connect_via_discovery）。
    r = bt.bt_connect(device=name)
    if r.ok:
        return ExecResult(
            True, r.message,
            _join(f"（WiFi 侧没有叫「{name}」的网络，按蓝牙连接）", r.detail),
            detail_format=getattr(r, "detail_format", "markdown"),
        )

    # WiFi 侧复核：可见列表是 NetworkManager 的扫描缓存，可能过期 → 重扫再下结论。
    dev, state = wifi.radio_state()
    if state == "unavailable":
        wifi_side = f"WiFi 网卡 {dev or '未找到'} 当前不可用，WiFi 侧无法确认"
    else:
        visible = wifi.visible_ssids(rescan=True)
        fresh = _ssid_matches(name, visible)
        if fresh:
            r2 = wifi.wifi_connect(ssid=fresh[0])
            return ExecResult(
                r2.ok, r2.message,
                _join(f"（重扫后 WiFi 侧出现 {fresh[0]}，按 WiFi 连接）", r2.detail),
                detail_format=getattr(r2, "detail_format", "markdown"),
            )
        wifi_side = f"本机当前可见 {len(visible)} 个 WiFi，没有叫「{name}」的（已重扫确认）"

    paired_labels = bt.known_device_labels()
    return ExecResult(
        False,
        f"WiFi 和蓝牙里都没找到「{name}」",
        _join(
            f"WiFi 侧：{wifi_side}",
            f"蓝牙侧：{r.message}",
            f"已配对的蓝牙设备：{_tail(paired_labels)}" if paired_labels else "",
            r.detail,
            "设备开着机的话，请让它进入可配对状态再试一次；"
            f"要指定域可以说「连接蓝牙 {name}」或「连接 {name} 的wifi」",
        ),
        detail_format=getattr(r, "detail_format", "markdown"),
    )


# ── 分发表 ───────────────────────────────────────────────────────────
# 工具名 → 执行函数（Skill 加载时并入主 HANDLERS）
HANDLERS = {
    "connect_any": connect_any,
}
