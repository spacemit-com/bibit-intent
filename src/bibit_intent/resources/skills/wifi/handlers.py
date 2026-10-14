"""
skills/wifi/handlers.py — WiFi 连接管理 Skill 的执行实现

通过 nmcli (NetworkManager CLI) 管理 WiFi。
对照 design.md §12 安全：
  - 参数化 subprocess（列表形式），绝不 shell 字符串拼接
  - 密码不回显、不记录日志
  - nmcli 不存在时返回明确错误
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Literal

from bibit_intent.execution.cancellation import run_command
from bibit_intent.execution.md_format import md_table as _md_table


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    detail_format: Literal["text", "markdown"] = "markdown"


def _run(cmd: list[str], timeout: int = 15) -> ExecResult:
    """参数化执行，cmd 必须是列表，杜绝 shell 注入。"""
    try:
        p = run_command(cmd, timeout=timeout)
        if p.returncode == 0:
            return ExecResult(True, "ok", p.stdout, detail_format="text")
        return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")
    except FileNotFoundError:
        return ExecResult(False, f"命令不存在：{cmd[0]}（需安装 NetworkManager）")
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")


# ── 表格渲染用的解析 helper ─────────────────────────────────────────

#: nmcli `device` 的电口/无线状态 → 中文标签
_STATE_CN = {
    "connected": "已连接",
    "connecting": "连接中",
    "disconnected": "未连接",
    "deactivating": "断开中",
    "unavailable": "不可用",
    "unmanaged": "未托管",
}


def _split_escaped(line: str) -> list[str]:
    """按未被转义的 ':' 切分 nmcli -t 输出。

    `nmcli -t` 把字段内的 ':' 转义成 '\\:'（SSID、BSSID 里带冒号时，本机实测
    `nmcli -t -f SSID,BSSID device wifi list` 输出 `D4\\:84\\:09\\:...`）。
    直接 `line.split(":")` 会把一个字段切成几段、后面所有字段整体错位。
    """
    fields: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    fields.append("".join(buf))
    return fields


def _saved_ssids() -> set[str]:
    """已保存的 WiFi 连接配置名（NetworkManager 里 type=802-11-wireless 的 NAME）。"""
    r = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
    if not r.ok:
        return set()
    saved: set[str] = set()
    for line in r.detail.splitlines():
        parts = _split_escaped(line)
        if len(parts) == 2 and parts[1] == "802-11-wireless":
            saved.add(parts[0])
    return saved


def _band_of(freq: str) -> str:
    """把 nmcli 的 FREQ（本机实测量如 `5300 MHz`）换算成频段标签。"""
    digits = "".join(ch for ch in freq if ch.isdigit())
    if not digits:
        return "-"
    mhz = int(digits)
    if mhz < 2500:
        return "2.4 GHz"
    if mhz < 5900:
        return "5 GHz"
    return "6 GHz"


def _signal_value(signal: str) -> int:
    """信号强度取整（排序用）；nmcli 偶发给空值时按 -1 排到最后。"""
    return int(signal) if signal.strip().isdigit() else -1


def _device_ip_info(dev: str) -> tuple[list[str], str, str]:
    """网卡的 (IPv4 列表, 网关, MAC)，取不到的部分返回空值。

    `device show` 的 MAC/IPv6 值里带**未转义**的 ':'（本机实测
    `GENERAL.HWADDR:FE:FE:FE:05:0C:FE`），所以按第一个 ':' 切分、值原样保留。
    """
    r = _run([
        "nmcli", "-t", "-f", "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY",
        "device", "show", dev,
    ], timeout=5)
    addrs: list[str] = []
    gateway = mac = ""
    if not r.ok:
        return addrs, gateway, mac
    for line in r.detail.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if not value or value == "--":
            continue
        if key.startswith("IP4.ADDRESS"):
            addrs.append(value)
        elif key == "IP4.GATEWAY":
            gateway = value
        elif key == "GENERAL.HWADDR":
            mac = value
    return addrs, gateway, mac


# ── WiFi 扫描 ────────────────────────────────────────────────────────

def wifi_scan(**_) -> ExecResult:
    """扫描附近的 WiFi 网络，返回 SSID + 信号强度列表（Markdown 表格）。"""
    # 先触发一次重新扫描
    _run(["nmcli", "device", "wifi", "rescan"], timeout=10)

    # 获取扫描结果（SSID、信号强度、安全性、信道、频点）
    r = _run([
        "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,CHAN,FREQ",
        "device", "wifi", "list",
    ])
    if not r.ok:
        return r

    saved = _saved_ssids()
    seen = set()
    networks: list[list[str]] = []
    for line in r.detail.splitlines():
        parts = _split_escaped(line)
        if len(parts) < 2 or not parts[0]:
            continue
        ssid = parts[0]
        if ssid in seen:
            continue
        seen.add(ssid)
        signal = parts[1].strip() or "?"
        security = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "开放"
        chan = parts[3].strip() if len(parts) > 3 and parts[3].strip() else "-"
        band = _band_of(parts[4]) if len(parts) > 4 else "-"
        networks.append(
            [ssid, signal, security, chan, band, "✓" if ssid in saved else "—"]
        )

    if not networks:
        return ExecResult(True, "未扫描到 WiFi 网络")

    networks.sort(key=lambda row: _signal_value(row[1]), reverse=True)
    rows = [[str(i + 1)] + row for i, row in enumerate(networks)]
    detail = "## 附近的 WiFi 网络\n\n" + _md_table(
        ["#", "SSID", "信号(%)", "安全", "信道", "频段", "已保存"], rows[:8]
    )
    if len(rows) > 8:
        detail += f"\n\n…（共 {len(rows)} 个）"
    top = networks[0]
    return ExecResult(
        True,
        f"扫描到 {len(networks)} 个网络，信号最强：{top[0]}（{top[1]}%）",
        detail,
    )


# ── WiFi 连接 ────────────────────────────────────────────────────────

# 类别词：这些不是真实网络名，而是用户泛指"WiFi/网络"本身。
# 若被误抽成 SSID，应提示用户给出具体网络名，而非拿它去连（必然失败）。
_CATEGORY_WORDS = {"wifi", "WiFi", "WIFI", "无线", "网络", "wi-fi", "wlan"}

# 占位符：用户没给具体信息时，1.7B 模型倾向编造这类占位 SSID/密码。
# 若命中，说明模型在瞎猜，应引导用户给真实信息，而非拿占位符去连。
_PLACEHOLDER_WORDS = {
    "mywifi", "myssid", "ssid", "yourwifi", "name",
    "mypass", "mypassword", "password", "pass", "xxx", "xxxx",
}


def _looks_like_placeholder(ssid: str, password: str) -> bool:
    """SSID 或密码是模型编造的占位符 → 判定为"无有效信息"。"""
    s = ssid.strip().lower()
    p = password.strip().lower()
    return s in _PLACEHOLDER_WORDS or (bool(p) and p in _PLACEHOLDER_WORDS)


def _find_saved_profile(ssid: str) -> str | None:
    """
    查找已保存的连接 profile 名（与 nm-tray 保存的同源）。
    返回 profile 名（通常等于 SSID），无则 None。

    NetworkManager 的 connection.id 通常等于 SSID。精确匹配 NAME 列，
    避免子串误判（如 "JDSK" 不应匹配到 "JDSK_Guest"）。
    """
    r = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=5)
    if not r.ok:
        return None
    for line in r.detail.splitlines():
        # -t 输出以 : 分隔，NAME 可能含转义的 :，取最后一段为 TYPE
        parts = line.rsplit(":", 1)
        if len(parts) == 2 and parts[1] == "802-11-wireless":
            name = parts[0].replace("\\:", ":")  # 还原转义
            if name == ssid:
                return name
    return None


# 域歧义提示：名字不是 WiFi 时，引导用户换到蓝牙域（而不是硬说密码/信号有问题）。
# 对照 docs/intent-ambiguity-survey.md §2.3「判别式澄清」：给一句能区分动作域的追问。
_BT_HINT = "如果 {name} 是蓝牙设备（耳机/鼠标/键盘/音箱），请说：连接蓝牙 {name}"


def _radio_state() -> tuple[str, str]:
    """返回 (WiFi 网卡名, 状态)。无 WiFi 网卡时返回 ("", "")。

    状态取自 nmcli 的 DEVICE 列表：connected / disconnected / unavailable /
    unmanaged 等。unavailable 表示网卡目前不可用（被 rfkill 挡、驱动没起、
    或本机压根没有 WiFi 网卡），此时任何 wifi_connect 都不可能成功。
    """
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=5)
    if not r.ok:
        return "", ""
    for line in r.detail.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "wifi":
            return parts[0], parts[2]
    return "", ""


def _visible_ssids(rescan: bool = False) -> list[str]:
    """当前可见的 SSID 列表（NetworkManager 的扫描缓存）。

    rescan=True 时先触发一次重扫再读（多花约 5s）。只用在"名字不在缓存里"的
    二次确认上：避免把「刚开机 / 缓存还空 / 扫描过期」误判成「没这个网络」。
    """
    if rescan:
        _run(["nmcli", "device", "wifi", "rescan"], timeout=15)
        time.sleep(5)
    r = _run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"], timeout=10)
    if not r.ok:
        return []
    # nmcli -t 把 SSID 里的 ":" 转义成 "\:"，比较前要还原
    return [l.replace("\\:", ":") for l in r.detail.splitlines() if l.strip()]


# ── 供 connect skill（动作域判域）复用的只读探针 ─────────────────────
# 「连接 <名字>」在不知道是 WiFi 还是蓝牙时，由 skills/connect/handlers.py 先查这里：
# 可见 SSID + 已保存配置（便宜、只读），再查蓝牙侧。判域靠环境事实，不靠名字形态。

def visible_ssids(rescan: bool = False) -> list[str]:
    """当前可见的 SSID 列表（去重、保序）。rescan=True 先重扫再读（多花约 5s）。"""
    out: list[str] = []
    seen: set[str] = set()
    for ssid in _visible_ssids(rescan=rescan):
        s = ssid.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def saved_profile_name(ssid: str) -> str | None:
    """已保存的连接配置名（精确同名才算，详见 _find_saved_profile）。"""
    return _find_saved_profile(ssid)


def radio_state() -> tuple[str, str]:
    """WiFi 网卡 (设备名, 状态)；无 WiFi 网卡返回 ("", "")。"""
    return _radio_state()


def wifi_connect(ssid: str = "", password: str = "", **_) -> ExecResult:
    """连接指定 WiFi。密码可选（开放网络无需密码）。"""
    # SSID 缺失/类别词/模型编造的占位符 → 引导用户提供真实网络名和密码，
    # 而非拿"wifi"/"mywifi"这类去连（必然失败，且误导用户）。
    if (not ssid
            or ssid.strip() in _CATEGORY_WORDS
            or _looks_like_placeholder(ssid, password)):
        return ExecResult(
            False,
            "请告诉我要连接的 WiFi 名称和密码",
            "例如：连接 JDSK 密码 12345678",
        )

    # 执行前用环境事实核对这个名字是不是 WiFi：可见 SSID ∪ 已保存 profile；
    # 显式给了密码时不预检（隐藏 SSID 也要能连）。
    saved = _find_saved_profile(ssid)
    dev, state = _radio_state()
    if state == "unavailable":
        # 网卡不可用时，扫描结果为空也说明不了问题，只能说清"现在连不了"。
        # 名字不是任何已知 WiFi（无已保存 profile）时，才把它当"可能是别的域"提示蓝牙。
        return ExecResult(
            False,
            f"WiFi 网卡当前不可用（{dev or '未找到 WiFi 网卡'}），无法连接 {ssid}",
            _BT_HINT.format(name=ssid) if saved is None
            else "检查网卡是否被 rfkill 关闭、驱动是否加载（nmcli device）",
        )
    if not password and saved is None:
        visible = _visible_ssids()
        if ssid not in set(visible):          # 缓存里没有 → 重扫一次再确认
            visible = _visible_ssids(rescan=True)
        if ssid not in set(visible):
            return ExecResult(
                False,
                f"未发现名为 {ssid} 的 WiFi 网络（本机当前可见 {len(visible)} 个，都不叫这个名字）",
                _BT_HINT.format(name=ssid),
            )

    # 策略：先复用已保存 profile（与 nm-tray 同源），无则新建。
    # 复用 connection up：重连已知网络更快、更稳，且不重复创建 profile。
    if saved and not password:
        # 已有 profile 且未显式给新密码 → 直接激活（秒连，复用保存的密码）
        r = _run(["nmcli", "connection", "up", saved], timeout=30)
        if r.ok:
            return ExecResult(True, f"已连接 WiFi：{ssid}（复用已保存配置）")
        # 复用失败（如密码过期）→ 落到新建路径重试
    # 无 profile 或带新密码 → device wifi connect（新建/更新 profile）。
    # 两种路径都经 NetworkManager，由 netplan 插件回写 /etc/netplan 持久化。
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]

    r = _run(cmd, timeout=30)
    if r.ok:
        return ExecResult(True, f"已连接 WiFi：{ssid}")
    # nmcli 连接失败通常是密码错误或信号问题
    return ExecResult(False, f"连接失败：{ssid}（检查密码或信号）", r.detail, detail_format=r.detail_format)


# ── WiFi 断开 ────────────────────────────────────────────────────────

def wifi_disconnect(**_) -> ExecResult:
    """断开当前 WiFi 连接。"""
    # 找到当前活动的 WiFi 设备
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    if not r.ok:
        return r

    wifi_dev = None
    for line in r.detail.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "wifi" and parts[2] == "connected":
            wifi_dev = parts[0]
            break

    if not wifi_dev:
        return ExecResult(True, "当前没有活动的 WiFi 连接")

    r2 = _run(["nmcli", "device", "disconnect", wifi_dev], timeout=10)
    if r2.ok:
        return ExecResult(True, f"已断开 WiFi（设备 {wifi_dev}）")
    return ExecResult(False, "断开失败", r2.detail, detail_format=r2.detail_format)


# ── 网卡开关（radio）─────────────────────────────────────────────────
# 打开/关闭 WiFi 是 NetworkManager 的 radio 开关，底层落在 rfkill 上：
# 关闭后 `rfkill list wifi` 两条记录都是软阻断（K3 实测 softblock=2），且 phy0
# 会连"硬阻断"一起报 yes —— 那是驱动对"radio 已关"的回显，不是硬件开关锁死，
# 所以打开时不能拿硬阻断当结论（蓝牙侧同款处理见 bt_on）。

def _radio_enabled() -> bool | None:
    """`nmcli -t radio wifi` → True/False；None 表示读不到。

    用 -t（terse）输出：非 terse 的 `nmcli radio` 走本地化（本机实测中文
    「已启用/已禁用」），-t 模式才是稳定的 enabled/disabled。
    """
    r = _run(["nmcli", "-t", "radio", "wifi"], timeout=5)
    if not r.ok:
        return None
    value = r.detail.strip().lower()
    if value in {"enabled", "disabled"}:
        return value == "enabled"
    return None


def _rfkill_soft_blocked() -> bool | None:
    """读 `rfkill list wifi` 的软阻断；None 表示读不到（无 rfkill / 无网卡）。"""
    try:
        p = subprocess.run(["rfkill", "list", "wifi"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Soft blocked:\s*(yes|no)", p.stdout or "", re.IGNORECASE)
    return None if not m else m.group(1).lower() == "yes"


def _rfkill_unblock() -> bool:
    """解除无线网卡软阻断；返回是否成功（不抛异常）。"""
    try:
        subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True,
                       text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return _rfkill_soft_blocked() is False


#: radio 开关是异步生效的：K3 实测 `nmcli radio wifi on` 返回 0 之后，radio 还要
#: 约 4 秒才从 disabled 变 enabled（网卡从 unavailable 变 disconnected）。只看退出
#: 码会把"正在开"误判成"没打开"，所以必须轮询确认。
_RADIO_SETTLE_SECONDS = 12.0
_RADIO_POLL_SECONDS = 0.5


def _wait_radio(enabled: bool, timeout: float = _RADIO_SETTLE_SECONDS) -> bool:
    """轮询等待 radio 到达目标状态。"""
    deadline = time.monotonic() + timeout
    while True:
        if _radio_enabled() is enabled:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_RADIO_POLL_SECONDS)


def wifi_on(**_) -> ExecResult:
    """打开 WiFi 网卡（radio）。

    关闭态的开关落在 rfkill 软阻断上（桌面面板关 WiFi 也会写 /dev/rfkill），
    所以先解阻断再开 radio；只发 `nmcli radio wifi on` 可能被残留软阻断挡住。
    """
    dev, _ = _radio_state()
    if not dev:
        return ExecResult(False, "打开 WiFi 失败：没有无线网卡",
                          "可执行 nmcli device 确认无线硬件是否被识别")
    if _radio_enabled() is True:
        return ExecResult(True, "WiFi 已开启")

    unblocked = False
    if _rfkill_soft_blocked() is True:
        unblocked = _rfkill_unblock()
    _run(["nmcli", "radio", "wifi", "on"], timeout=10)
    if _wait_radio(True):
        return ExecResult(True, "WiFi 已打开" + ("（已解除系统的软阻断）" if unblocked else ""))

    # 还没开起来：解阻断后再试一次，都失败才报错
    if _rfkill_soft_blocked() is True:
        if _rfkill_unblock():
            _run(["nmcli", "radio", "wifi", "on"], timeout=10)
            if _wait_radio(True):
                return ExecResult(True, "WiFi 已打开（已解除系统的软阻断）")
        return ExecResult(
            False, "打开 WiFi 失败：无线网卡被系统软阻断，自动解除未成功",
            "请手动执行 rfkill unblock wifi，并确认 rfkill list wifi 的阻断状态",
        )
    return ExecResult(
        False, "打开 WiFi 失败：无线网卡未进入启用状态",
        "可执行 nmcli radio wifi on 重试；仍失败时检查 NetworkManager 状态"
        "（systemctl status NetworkManager）",
    )


def wifi_off(**_) -> ExecResult:
    """关闭 WiFi 网卡（radio）。

    关掉后网卡状态会变成 unavailable（`nmcli device`），属正常现象 —— 不是网卡
    损坏，用 wifi_status / 状态面板看应是"已关闭"。
    """
    dev, _ = _radio_state()
    if not dev:
        return ExecResult(False, "关闭 WiFi 失败：没有无线网卡")
    if _radio_enabled() is False:
        return ExecResult(True, "WiFi 已关闭")
    r = _run(["nmcli", "radio", "wifi", "off"], timeout=10)
    if _wait_radio(False, timeout=8.0):
        return ExecResult(True, "WiFi 已关闭")
    return ExecResult(False, "关闭 WiFi 失败",
                      r.detail or "无线网卡仍处于启用状态", detail_format=r.detail_format)


# ── WiFi 状态 ────────────────────────────────────────────────────────

def wifi_status(**_) -> ExecResult:
    """查看当前 WiFi 连接状态（Markdown 表格：状态/SSID/信号/安全/信道/网卡/IP）。"""
    dev, state = _radio_state()
    # 网卡被关掉时（radio off）报"已关闭"，不要报"未连接 / 不可用"：那是两回事，
    # 看到"不可用"会以为网卡坏了或没连上，实际只是开关关着（去 wifi_on 开）。
    if _radio_enabled() is False:
        rows = [["状态", "已关闭"],
                ["网卡", f"{dev}（已关闭）" if dev else "未找到无线网卡"]]
        return ExecResult(True, "WiFi 已关闭", _md_table(["项目", "数值"], rows))

    r = _run([
        "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY,CHAN,FREQ",
        "device", "wifi", "list",
    ])

    active: list[str] = []
    if r.ok:
        for line in r.detail.splitlines():
            parts = _split_escaped(line)
            if len(parts) >= 2 and parts[0] == "yes":
                active = parts
                break

    if not active:
        rows = [["状态", "未连接"]]
        rows.append([
            "网卡",
            f"{dev}（{_STATE_CN.get(state, state)}）" if dev else "未找到无线网卡",
        ])
        return ExecResult(True, "当前未连接 WiFi", _md_table(["项目", "数值"], rows))

    ssid = active[1]
    signal = active[2].strip() if len(active) > 2 and active[2].strip() else "-"
    security = active[3].strip() if len(active) > 3 and active[3].strip() else "开放"
    chan = active[4].strip() if len(active) > 4 and active[4].strip() else "-"
    band = _band_of(active[5]) if len(active) > 5 else "-"
    rows = [
        ["状态", "已连接"],
        ["SSID", ssid],
        ["信号", f"{signal}%"],
        ["安全", security],
        ["信道/频段", f"{chan} / {band}"],
    ]
    if dev:
        rows.append(["网卡", f"{dev}（{_STATE_CN.get(state, state) or '-'}）"])
        addrs, gateway, mac = _device_ip_info(dev)
        rows.append(["IP", "<br>".join(addrs) if addrs else "—"])
        rows.append(["网关", gateway or "—"])
        rows.append(["MAC", mac or "—"])
    return ExecResult(
        True, f"当前连接：{ssid}（信号 {signal}%）", _md_table(["项目", "数值"], rows)
    )


# ── WiFi IP ──────────────────────────────────────────────────────────

def wifi_ip(**_) -> ExecResult:
    """查看无线网卡的 IPv4 地址与网关（Markdown 表格，只列无线网卡）。

    skill.yaml 声明了 wifi_ip、router 也会路由到这里，缺实现会回"工具无执行实现"。
    """
    # 找到无线设备（不要求已连接，才能给出"未连接"的明确提示而不是报无实现）
    r = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
    if not r.ok:
        return r

    wifi_dev = None
    for line in r.detail.splitlines():
        parts = _split_escaped(line)
        if len(parts) >= 2 and parts[1] == "wifi":
            wifi_dev = parts[0]
            break

    if not wifi_dev:
        return ExecResult(False, "没有找到无线网卡")

    dev, state = _radio_state()
    state_label = _STATE_CN.get(state, state) or "-"
    addrs, gateway, mac = _device_ip_info(wifi_dev)
    rows = [[
        wifi_dev,
        state_label,
        "<br>".join(addrs) if addrs else "—",
        gateway or "—",
        mac or "—",
    ]]
    detail = _md_table(["网卡", "状态", "IPv4", "网关", "MAC"], rows)

    if not addrs:
        # 状态/网关/MAC 仍然查得到，一并给出，别让用户只看到一句"没有 IP"。
        return ExecResult(
            False,
            f"无线网卡 {wifi_dev} 没有取到 IPv4 地址（可能未连接 WiFi）",
            detail,
        )
    return ExecResult(True, f"无线网络 IP：{addrs[0].split('/')[0]}", detail)


# ── 分发表 ───────────────────────────────────────────────────────────
# 工具名 → 执行函数（Skill 加载时并入主 HANDLERS）
HANDLERS = {
    "wifi_on": wifi_on,
    "wifi_off": wifi_off,
    "wifi_scan": wifi_scan,
    "wifi_connect": wifi_connect,
    "wifi_disconnect": wifi_disconnect,
    "wifi_status": wifi_status,
    "wifi_ip": wifi_ip,
}
