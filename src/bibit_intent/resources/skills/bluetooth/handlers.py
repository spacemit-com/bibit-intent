"""
skills/bluetooth/handlers.py — 蓝牙管理 Skill 的执行实现

通过 bluetoothctl (BlueZ 交互式 CLI) 管理蓝牙适配器与设备。
对照 design.md §12 安全 + skills/wifi/handlers.py 风格：
  - 参数化 subprocess（列表形式），绝不 shell 字符串拼接
  - bluetoothctl 是交互式 REPL，用「分步喂命令」的会话方式驱动（见 _session），
    命令之间留等待时间，D-Bus 的异步回复才有机会打印出来
  - 命令不存在/超时/硬件缺失时返回明确错误，不抛异常
  - 设备名支持中英模糊匹配 → MAC 解析（沿用 wifi 的类别词过滤思路）

板子实测修掉的两个真实缺陷（bluetoothctl 5.85）：
  1) 把 "power on\\nquit\\n" 一次性喂进 stdin，bluetoothctl 读完立刻退出，
     "Changing power on succeeded" 这条 D-Bus 回复还没打印进程就没了：
       - 成功判定字符串落空 → 整个 REPL 原始输出（ANSI 色码、prompt 与命令
         回显、成排空格、"Waiting to connect to bluetoothd..."、"Agent
         registered"、"SupportedUUIDs"）被当成 detail 打给用户（cli.py:93
         会原样打印 detail），用户看到的就是"打一大串 log"。
       - 现在改为：命令之间留等待时间，并用 `show` 的 `Powered: yes/no` 判定
         成败，detail 只保留清洗后的有效信息。
  2) 同样因为 "scan on" 后紧跟 "quit"，discovery 实际只跑了 0.02 秒，
     必然扫不到任何设备（用户手动交互式扫描却能扫到）。
     现在 scan on 会真的等满 _SCAN_SECONDS 再 scan off，并从
     `[NEW] Device <MAC> <Name>` / `[CHG] ... Name:|Alias:` 解析本次结果。
  附带修掉：v1 的设备解析正则会把 `[... ] Device <MAC> RSSI: 0x.. (-78)`
  这类属性行当成设备，设备名被解析成 "RSSI: 0xffffffb2 (-78)"。
  3) 扫描前要先清掉适配器上已有的发现会话（`scan off` → `scan on`）。别人
     （桌面面板/配对流程）或上一次异常退出留下的残留 discovery 会让 bluez 认为
     发现已在跑，不再重新发起底层扫描，本会话收不到 DeviceFound 事件：直接 join
     只解析到 15~16 个设备，长时间残留的会话更是 0 个，云端据此判成"附近没有
     设备"。K3 实测先 scan off 再 scan on 可稳定拿到 44~58 个设备（见 _scan_window）。
  4) 按设备名连接必须先在**发现窗口内**定位设备。BLE 设备（鼠标/耳机）用随机地址，
     每次重新配对都会换一个：同一只 LOGI M240 B 在 bluez 缓存里留下过
     ...:42/:43/:44/:45/:46 六个地址，拿缓存里的旧地址 connect 只会得到
     "not available" / "le-connection-abort-by-local"，用户看到的就是
     "连接失败：XXX（设备未响应或拒绝连接）" —— 其实设备就在旁边，只是地址过期了。
     而且一旦离开发现窗口，bluez 对刚发现的地址直接回
     `Device D1:... not available`，所以 pair/trust/connect 必须和 `scan on`
     共用同一个 bluetoothctl 会话（见 _connect_via_discovery）。
  5) 残留 discovery（`Discovering: yes` 且属主已退出）期间，新会话的 `scan on`
     只能收到"发生变化"的事件，别人刚发现过的设备不会重新上报 —— 排查时表现为
     "设备明明在广播却扫不到"。这种残留用 `scan off` 清不掉
     （Failed to stop discovery），只能重启 bluetoothd（bluetooth.service）恢复。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from bibit_intent.execution.cancellation import raise_if_cancelled
from bibit_intent.execution.md_format import md_table as _md_table


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    detail_format: Literal["text", "markdown"] = "markdown"


# ── bluetoothctl 输出清洗 ────────────────────────────────────────────
# bluetoothctl 是为终端人机交互写的 REPL，输出里混着 ANSI 色码、光标退格、
# 成排的空格填充、prompt（以及我们喂进去的命令回显），直接透传就是刷屏。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROMPT_RE = re.compile(r"(?:[ \t]*\[bluetoothctl\]>[ \t]*)+")

# 与业务无关的 REPL 噪声行（前缀匹配）
_NOISE_PREFIXES = (
    "Waiting to connect to bluetoothd",
    "Agent registered",
    "Agent unregistered",
    "Agent removed",
    "SetDiscoveryFilter success",
    "Discovery started",
    "Discovery stopped",
    "Changing power on succeeded",
    "Changing power off succeeded",
    "SupportedUUIDs:",
)

# ManufacturerData 之类的十六进制转储（属性值会整块打出来）
_HEX_BYTE_RE = re.compile(r"[0-9A-Fa-f]{2}")


def _is_data_dump(line: str) -> bool:
    """属性值的十六进制转储行（`01 41 1c 32 ... .A.2y...`），对用户没有意义。"""
    tokens = line.split()
    if not tokens:
        return False
    if re.fullmatch(r"[0-9A-Fa-f ]{2,80}", line):
        return True
    hex_tokens = sum(1 for t in tokens if _HEX_BYTE_RE.fullmatch(t))
    if hex_tokens >= 3 and hex_tokens * 2 >= len(tokens):
        return True
    # 单字节 + ASCII 侧栏（如 "00            ."）：整行没有任何英文单词
    return hex_tokens >= 1 and not re.search(r"[A-Za-z]{3,}", line)


# MAC 地址：严格 5 段冒号格式，避免把属性文本里的数字串当 MAC
_MAC = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
_MAC_RE = re.compile(rf"^{_MAC}$")

_DEVICE_LINE_RE = re.compile(rf"^Device\s+({_MAC})\s*(.*)$")
_NEW_RE = re.compile(rf"^\[NEW\]\s+Device\s+({_MAC})(?:\s+(.*))?$")
_CHG_NAME_RE = re.compile(rf"^\[CHG\]\s+Device\s+({_MAC})\s+(?:Name|Alias):\s*(.+)$")
_CHG_RSSI_RE = re.compile(rf"^\[CHG\]\s+Device\s+({_MAC})\s+RSSI:\s*0x[0-9a-fA-F]+\s*\((-?\d+)\)")
_POWERED_RE = re.compile(r"^Powered:\s*(yes|no)$", re.IGNORECASE)

# 属性行（防御性过滤：`devices` 与发现流混在一起时不能当成设备名）
_PROPERTY_NAME_RE = re.compile(
    r"^(?:RSSI|TxPower|ManufacturerData|ServiceData|UUIDs|Class|Icon|Appearance"
    r"|AddressType|Address|Modalias|Adapter|ServicesResolved|Connected|Paired"
    r"|Trusted|Bonded|Blocked|LegacyPairing|Alias|Name|WakeAllowed|Discovering)"
    r"(?:\.\w+)*:",
    re.IGNORECASE,
)


def _iter_lines(raw: str, drop: frozenset[str] | set[str] | None = None,
                since: str | None = None) -> list[str]:
    """把 bluetoothctl 原始输出切成「干净行」。

    去 ANSI 色码与退格、去 prompt 前缀、去空行，并丢掉被 REPL 回显的命令行。
    ``since`` 给定时只保留该行（命令回显）之后的内容 —— 用于把结果限定在
    「本次 scan on 之后的发现窗口」内，避免把适配器上既有的发现会话产生的
    事件算进本次扫描。
    """
    text = _ANSI_RE.sub("", raw).replace("\x08", "")
    dropped = {d.strip().lower() for d in (drop or ()) if d.strip()}
    out: list[str] = []
    for line in text.splitlines():
        line = _PROMPT_RE.sub("", line).strip()
        if not line:
            continue
        out.append(line)
    if since:
        marker = since.strip().lower()
        index = None
        for i, line in enumerate(out):
            if line.lower() == marker:
                index = i
        if index is not None:
            out = out[index + 1:]
    return [line for line in out if line.lower() not in dropped]


def _clean_detail(raw: str, drop: frozenset[str] | set[str] | None = None,
                  limit: int = 12) -> str:
    """把原始输出收敛成可直接给用户看的错误/提示文本（去噪声与发现刷屏）。"""
    lines: list[str] = []
    for line in _iter_lines(raw, drop=drop):
        if line.startswith(_NOISE_PREFIXES) or line.startswith(("[NEW]", "[CHG]")):
            continue
        if _is_data_dump(line):
            continue
        lines.append(line)
    if len(lines) > limit:
        lines = lines[:limit] + ["..."]
    return "\n".join(lines)


# ── bluetoothctl 会话式执行 ─────────────────────────────────────────

#: `scan on` 的持续时间。蓝牙经典 inquiry 需要数秒才能发现设备，
#: BLE 广播快得多，但太短会漏掉只广播音频/经典服务的设备。
_SCAN_SECONDS = 8.0


@dataclass
class _Session:
    """一次 bluetoothctl 会话的结果。"""

    started: bool                     # bluetoothctl 是否成功启动
    raw: str = ""                     # stdout + stderr 原始输出
    echoed: frozenset[str] = field(default_factory=frozenset)  # 喂进去的命令
    error: str = ""                   # 启动失败时的说明
    timed_out: bool = False           # 是否因总时长超限被提前收尾


def _kill_process_group(proc: subprocess.Popen) -> None:
    """结束 bluetoothctl 进程组（SIGTERM → SIGKILL），不留后台进程。"""
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            continue


def _sleep_interruptible(seconds: float) -> None:
    """可被取消打断的 sleep（0.1s 粒度轮询取消事件）。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


#: 动态步骤：按「目前拿到的原始输出」决定下一条命令；返回 None/"" 表示不必继续。
#: 用于"先扫描定位设备、再配对连接"这类必须和前一条命令共用同一个 bluetoothctl
#: 会话的场景（见 _connect_via_discovery）。
_Step = str | Callable[[str], "str | None"]


def _session(steps: list[tuple["_Step", float]], timeout: float = 30.0) -> _Session:
    """
    以会话方式驱动 bluetoothctl：steps 逐条写进 stdin，每条命令后等待
    wait 秒再写下一条，最后自动补 `quit`。

    步骤里的命令可以是字符串，也可以是 `f(已有输出) -> 命令` 的可调用对象
    （动态步骤）：先扫描、拿到本次发现窗口的结果后再决定配对/连接哪台设备。

    为什么必须分步：bluetoothctl 的 D-Bus 回复是异步打印的，
    `"cmd\\nquit\\n"` 一次性喂进去时进程会在回复打印前退出（实测
    `power on` 的 "Changing power on succeeded" 就是这样丢的），
    `scan on` 也会因为立刻 quit 而只跑 0.02 秒（必然扫不到设备）。
    """
    raise_if_cancelled()
    try:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError:
        return _Session(False, error="命令不存在：bluetoothctl（需安装 BlueZ / bluez）")

    echoed: set[str] = {cmd.strip().lower() for cmd, _ in steps
                        if isinstance(cmd, str) and cmd.strip()}
    echoed.add("quit")
    chunks: list[str] = []
    lock = threading.Lock()

    def _pump(stream) -> None:
        # 持续读走 stdout/stderr，避免管道写满把 bluetoothctl 卡死
        try:
            for line in stream:
                with lock:
                    chunks.append(line)
        except (OSError, ValueError):
            pass

    readers = [
        threading.Thread(target=_pump, args=(stream,), daemon=True)
        for stream in (proc.stdout, proc.stderr)
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    timed_out = False
    try:
        for raw_cmd, wait in steps:
            raise_if_cancelled()
            if time.monotonic() - started > timeout:
                timed_out = True
                break
            cmd = raw_cmd
            if callable(cmd):
                with lock:
                    snapshot = "".join(chunks)
                cmd = cmd(snapshot) or ""
                if not cmd:
                    # 动态步骤判定「没必要继续」（如本次发现窗口里没找到目标设备）
                    break
                echoed.add(cmd.strip().lower())
            try:
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            _sleep_interruptible(wait)
        try:
            proc.stdin.write("quit\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            timed_out = True
    except BaseException:
        # 含 EdgeExecutionCancelled：必须先把进程组收掉再往外抛
        _kill_process_group(proc)
        raise
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    with lock:
        raw = "".join(chunks)
    return _Session(True, raw=raw, echoed=frozenset(echoed), timed_out=timed_out)


# ── 若干输出判定 ────────────────────────────────────────────────────

def _powered(raw: str, drop: frozenset[str] | set[str] | None = None) -> bool | None:
    """从 `show` 的输出里读 `Powered: yes/no`；None 表示没有可用控制器。"""
    for line in _iter_lines(raw, drop=drop):
        m = _POWERED_RE.match(line)
        if m:
            return m.group(1).lower() == "yes"
    return None


def _discovering(raw: str, drop: frozenset[str] | set[str] | None = None) -> bool | None:
    """从 `show` 输出里读 `Discovering: yes/no`；None 表示读不到。"""
    for line in _iter_lines(raw, drop=drop):
        m = re.match(r"^Discovering:\s*(yes|no)$", line, re.IGNORECASE)
        if m:
            return m.group(1).lower() == "yes"
    return None


def _no_controller(raw: str, drop: frozenset[str] | set[str] | None = None) -> bool:
    return any("no default controller" in line.lower()
               for line in _iter_lines(raw, drop=drop))


def _is_placeholder_name(name: str, mac: str) -> bool:
    """BLE 设备常不广播名称，bluez 会拿 MAC 当名字（4F-65-FB-...）。"""
    upper = name.strip().upper()
    if not upper:
        return True
    return upper == mac.upper().replace(":", "-")


def _parse_discovered(raw: str, drop: frozenset[str] | set[str] | None = None,
                      since: str | None = None) -> dict[str, dict[str, object]]:
    """
    解析本次扫描发现的设备：MAC → {name, rssi}。

    只认 `[NEW] Device <MAC> <Name>`（新发现）与
    `[CHG] Device <MAC> Name:|Alias:`（发现过程中拿到名字），
    信号强度取 `[CHG] ... RSSI:`。属性行不会被当成设备名。
    ``since`` 传 "scan on" 时只看本次发现窗口内的事件。
    """
    found: dict[str, dict[str, object]] = {}

    def record(mac: str) -> dict[str, object]:
        return found.setdefault(mac.upper(), {"name": "", "rssi": None})

    for line in _iter_lines(raw, drop=drop, since=since):
        m = _NEW_RE.match(line)
        if m:
            entry = record(m.group(1))
            if m.group(2):
                entry["name"] = m.group(2).strip()
            continue
        m = _CHG_NAME_RE.match(line)
        if m:
            record(m.group(1))["name"] = m.group(2).strip()
            continue
        m = _CHG_RSSI_RE.match(line)
        if m:
            record(m.group(1))["rssi"] = int(m.group(2))
    return found


def _parse_device_list(raw: str, drop: frozenset[str] | set[str] | None = None,
                       since: str | None = None) -> dict[str, str]:
    """
    解析 `devices` 命令的输出：MAC → Name。
    行格式：`Device 88:C9:E8:XX:XX:XX AirPods Pro`；属性行会被过滤掉
    （属性行不能当设备名，v1 的正则把 `RSSI: 0xffffffb2 (-78)` 当成了名字）。
    ``since`` 给定时只解析该命令回显之后的内容（见 bt_scan 里区分
    `devices` / `devices Paired` 两次输出）。
    """
    out: dict[str, str] = {}
    for line in _iter_lines(raw, drop=drop, since=since):
        m = _DEVICE_LINE_RE.match(line)
        if not m:
            continue
        name = (m.group(2) or "").strip()
        if not name or _PROPERTY_NAME_RE.match(name):
            continue
        out[m.group(1).upper()] = name
    return out


def _list_known_devices() -> list[tuple[str, str]]:
    """返回 bluetoothctl 已知设备的 [(MAC, Name), ...]（`devices` 输出）。"""
    s = _session([("devices", 0.6)], timeout=10)
    if not s.started:
        return []
    return sorted(_parse_device_list(s.raw, s.echoed).items())


# ── 设备解析：设备名/MAC → MAC ──────────────────────────────────────

# 类别词：不是真实设备名，用户在泛指"蓝牙/设备"本身。
# 若被误抽成 device 参数，应引导用户给具体设备名，而非拿它去连（必然失败）。
_CATEGORY_WORDS = {
    "蓝牙", "bluetooth", "设备", "device",
    "耳机", "音响", "音箱", "鼠标", "键盘",
    "这个", "那个", "刚才", "旧",
}


def _looks_like_category(name: str) -> bool:
    return name.strip().lower() in {w.lower() for w in _CATEGORY_WORDS}


def _resolve_device(name: str) -> tuple[str | None, str]:
    """
    把用户输入的 device 参数解析为 MAC。
    返回 (mac_or_None, 展示名)。
    - MAC 格式直接用
    - 否则在已知设备里做不区分大小写的子串匹配
    """
    name = name.strip()
    if _MAC_RE.match(name):
        return name.upper(), name.upper()
    key = name.lower()
    for mac, dname in _list_known_devices():
        if key in dname.lower():
            return mac, dname
    return None, name


# ── 蓝牙开关 ────────────────────────────────────────────────────────

def _rfkill_soft_blocked() -> bool | None:
    """读 `rfkill list bluetooth` 的软阻断状态；None 表示读不到（无 rfkill/无控制器）。"""
    try:
        p = subprocess.run(["rfkill", "list", "bluetooth"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Soft blocked:\s*(yes|no)", p.stdout or "", re.IGNORECASE)
    return None if not m else m.group(1).lower() == "yes"


def _rfkill_unblock() -> bool:
    """解除蓝牙软阻断；返回是否成功（不抛异常）。"""
    try:
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True,
                       text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return _rfkill_soft_blocked() is False


def bt_on(**_) -> ExecResult:
    """打开蓝牙适配器。成败以 `show` 的 Powered 字段为准（不看 REPL 文案）。

    适配器被 rfkill 软阻断时 bluez 一律回 org.bluez.Error.Failed（桌面端关蓝牙会写
    /dev/rfkill 造成软阻断），这里先解除阻断再试一次；都失败才报中文说明。
    """
    s = _session([("power on", 0.6), ("show", 0.6)], timeout=15)
    if not s.started:
        return ExecResult(False, "打开蓝牙失败", s.error, detail_format="text")
    if _powered(s.raw, s.echoed) is True:
        return ExecResult(True, "蓝牙已打开")
    if _rfkill_soft_blocked() is True:
        if _rfkill_unblock():
            s = _session([("power on", 0.6), ("show", 0.6)], timeout=15)
            if s.started and _powered(s.raw, s.echoed) is True:
                return ExecResult(True, "蓝牙已打开（已解除系统的软阻断）")
        return ExecResult(
            False, "打开蓝牙失败：蓝牙被系统软阻断，自动解除未成功",
            "请手动执行 rfkill unblock bluetooth，并确认 rfkill list bluetooth 的硬阻断为 no",
        )
    if _no_controller(s.raw, s.echoed):
        return ExecResult(
            False, "打开蓝牙失败：没有可用的蓝牙控制器",
            "请确认硬件存在并已启用；可执行 rfkill list bluetooth 检查是否被软/硬阻断",
        )
    return ExecResult(
        False, "打开蓝牙失败：适配器未进入 Powered: yes",
        "可检查 rfkill list bluetooth 与 dmesg 中的蓝牙初始化日志",
    )


def bt_off(**_) -> ExecResult:
    """关闭蓝牙适配器。同样以 `show` 的 Powered 字段为准。"""
    s = _session([("power off", 0.6), ("show", 0.6)], timeout=15)
    if not s.started:
        return ExecResult(False, "关闭蓝牙失败", s.error, detail_format="text")
    if _powered(s.raw, s.echoed) is False:
        return ExecResult(True, "蓝牙已关闭")
    if _no_controller(s.raw, s.echoed):
        return ExecResult(True, "蓝牙已关闭", "当前没有可用的蓝牙控制器")
    return ExecResult(False, "关闭蓝牙失败",
                      _clean_detail(s.raw, s.echoed) or "适配器仍为 Powered: yes", detail_format="text")


# ── 蓝牙扫描 ────────────────────────────────────────────────────────

def _scan_window(seconds: float) -> _Session:
    """占有一个发现窗口：`scan off` → `scan on` (seconds) → `scan off`。

    `scan on` 之前先 `scan off`：适配器上可能已经有 discovery 在跑（别的客户端
    的扫描、或上一次调用异常退出留下的残留会话）。这种情况下 bluez 认为发现已经
    在进行，本会话只能收到"发生变化"的设备。K3 实测（同一适配器）：
      - 从空闲状态启动的扫描：42~46 个设备；
      - 已有 discovery 在跑时启动的扫描：10~20 个；长时间残留（客户端已死、
        `Discovering` 仍为 true）的会话下甚至 0 个 —— 云端据此判成"附近没有设备"。
      - 停止后需要约 40s 空闲才能恢复到 42 个（5s → 16、15s → 27、40s → 38）。
    注意 `scan off` 只能停掉本客户端自己的发现：别人（活着的客户端）持有的扫描，
    bluez 会直接拒绝（`Failed to stop discovery: org.bluez.Error.Failed`），
    D-Bus 的 StopDiscovery 也报 "No discovery started"。所以这里只做尽力清理，
    真正的"结果可能不完整"由 bt_scan 读取扫描前的 `Discovering` 状态如实说明。
    """
    return _session(
        [("power on", 0.5), ("show", 0.5),
         ("scan off", 0.4),
         ("scan on", seconds),
         ("scan off", 0.3), ("devices", 0.6),
         # 同一次会话里读已配对列表：bt_scan 的表格要标"已配对"，另起一次会话多约 1s
         ("devices Paired", 0.6)],
        timeout=seconds + 25,
    )


#: 适配器上已有发现会话时，结果可能不完整的提示（不报错，但必须让调用方知道）
_PARTIAL_NOTE = "适配器上已有其它扫描在进行，本次只包含出现变化的设备，结果可能不完整，稍后可再扫一次"
_PARTIAL_SUFFIX = "（提示：适配器上已有其它扫描在进行，结果可能不完整）"


def bt_scan(**_) -> ExecResult:
    """
    扫描附近蓝牙设备：清掉本客户端可能留下的发现会话，再 `scan on` 持续
    _SCAN_SECONDS 秒，返回本次窗口内发现的设备（名称 + MAC + 信号强度）。

    注意：不能依赖 `devices` 的历史缓存 —— 它包含以前会话发现过的、当前并不在
    附近的设备；判定"附近有哪些"必须看本次扫描的发现流（因此解析限定在本次
    `scan on` 之后的事件里）。
    """
    s = _scan_window(_SCAN_SECONDS)
    if not s.started:
        return ExecResult(False, "扫描失败", s.error, detail_format="text")
    if _no_controller(s.raw, s.echoed):
        return ExecResult(False, "扫描失败：没有可用的蓝牙控制器",
                          "请确认硬件存在并已启用")
    if _powered(s.raw, s.echoed) is False:
        return ExecResult(False, "扫描失败：蓝牙适配器未打开",
                          "请先执行「打开蓝牙」；若打开失败可检查 rfkill list bluetooth")

    lines = _iter_lines(s.raw, s.echoed)
    discovery_failed = [l for l in lines
                        if "failed to start discovery" in l.lower()
                        or "inprogress" in l.lower()]
    if discovery_failed:
        # 发现没真正启动：报"没扫到设备"会让用户/云端误判成"设备不在附近"。
        return ExecResult(False, "扫描失败：蓝牙发现未能启动",
                          "\n".join(discovery_failed[:3]) +
                          "\n稍后重试；若持续失败可检查是否有别的程序正在扫描蓝牙", detail_format="text")

    # `show` 是在动 discovery 之前读的，代表本次扫描前的适配器状态；
    # 已有别的扫描在跑时 bluez 只把"有变化"的设备送给本会话，结果可能不完整，
    # 这点必须如实告诉调用方（云端据此判断"附近有没有某设备"，不能默默少报）。
    partial = _discovering(s.raw, s.echoed) is True
    found = _parse_discovered(s.raw, s.echoed, since="scan on")
    if not found:
        if partial:
            return ExecResult(True, "未扫描到蓝牙设备" + _PARTIAL_SUFFIX,
                              _PARTIAL_NOTE)
        return ExecResult(True, "未扫描到蓝牙设备")

    # 只报「本次扫描发现的」设备：`devices` 的历史缓存里躺着以前会话发现过、
    # 现在并不在附近的设备，拿它回答"附近有什么"会误导用户。
    # 但 bluez 已知的别名要借用（很多设备只在 Cached/Alias 里带名字，
    # 发现流里只有 MAC，直接显示 MAC 会认不出来）。
    known = _parse_device_list(s.raw, s.echoed)
    # `devices` 缓存把本次刚发现的设备也一并列出，所以「在不在缓存里」区分不出新旧；
    # 真正有意义的是「已配对（bonded，能直接重连）」还是「只是扫到的」。读不到
    # 已配对列表时（会话异常/命令没回显）状态列给 "-"，不硬说未配对。
    paired: set[str] | None = None
    if any(line.lower() == "devices paired" for line in _iter_lines(s.raw)):
        paired = set(_parse_device_list(s.raw, s.echoed, since="devices Paired"))
    rows: list[list[str]] = []      # [设备, 地址, 信号, 状态, RSSI(排序用)]
    named_count = 0
    for mac, info in found.items():
        name = str(info.get("name") or "") or known.get(mac, "")
        rssi = info.get("rssi")
        if _is_placeholder_name(name, mac):
            label = "未命名设备"
        else:
            label = name
            named_count += 1
        if paired is None:
            status = "-"
        elif mac in paired:
            status = "已配对"
        else:
            status = "未配对"
        rows.append([
            label,
            mac,
            f"{rssi} dBm" if isinstance(rssi, int) else "—",
            status,
            rssi if isinstance(rssi, int) else -999,
        ])

    # 有名称的排前面（用户按名字认设备），组内按信号强→弱；没报 RSSI 的排最后。
    rows.sort(key=lambda row: (row[0] == "未命名设备", -int(row[4])))
    table = [[str(i + 1)] + row[:4] for i, row in enumerate(rows)]
    detail = "## 附近的蓝牙设备\n\n" + _md_table(
        ["#", "设备", "地址", "信号", "状态"], table[:8]
    )
    if len(table) > 8:
        detail += f"\n\n…（共 {len(table)} 个）"
    message = f"扫描到 {len(found)} 个设备"
    if named_count:
        message += f"，其中 {named_count} 个有名称"
    else:
        message += "（均未广播名称）"
    if partial:
        message += _PARTIAL_SUFFIX
    return ExecResult(True, message, detail)


# ── 蓝牙连接/断开/配对 ─────────────────────────────────────────────

#: 连接前用来定位设备的发现窗口秒数。BLE 随机地址每次重新配对都会变，
#: 只有「本次窗口里真的在广播」的地址才配得上、连得上（见 _connect_via_discovery）。
_CONNECT_SCAN_SECONDS = 8.0

#: bluez 的失败文本 → 给用户看的原因说明（顺序匹配，先命中先返回）
_CONNECT_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("le-connection-abort-by-local",
     "设备在连接过程中主动断开：对端可能已连到别的设备，或这个地址已失效"),
    ("not available",
     "设备当前不可用：可能已关机/离开范围，或地址已变化（BLE 随机地址）"),
    ("authenticationcanceled", "设备取消了配对请求，请让设备重新进入可连接状态"),
    ("authenticationrejected", "设备拒绝了配对"),
    ("authenticationfailed", "配对认证失败，需要重新配对"),
    ("authenticationtimeout", "配对认证超时，设备未响应"),
    ("connectionattemptfailed", "连接尝试失败：设备未响应（信号弱或正在连其它主机）"),
    ("le-connection-timeout", "连接超时：设备未响应"),
)


def _list_paired_devices() -> list[tuple[str, str]]:
    """已配对（bonded）设备的 [(MAC, Name), ...]（`devices Paired` 输出）。"""
    s = _session([("devices Paired", 0.6)], timeout=10)
    if not s.started:
        return []
    return sorted(_parse_device_list(s.raw, s.echoed).items())


def known_device_matches(name: str) -> list[tuple[str, str]]:
    """bluez 已知设备缓存（`devices`，含以前配对/发现过的）里名字匹配的条目。

    供 connect skill 判域用：名字在蓝牙侧有记录 → 值得按蓝牙试连。
    注意这是**历史缓存**，里面的地址可能已失效（BLE 随机地址会变）；
    真正的"能不能连上"由 bt_connect 在发现窗口里重新定位地址（见 _connect_via_discovery）。
    """
    return _cached_matches(name)


def paired_device_matches(name: str) -> list[tuple[str, str]]:
    """已配对（bonded）设备里名字匹配的 [(MAC, Name)]。"""
    key = (name or "").strip().lower()
    if not key:
        return []
    return [(mac, dname) for mac, dname in _list_paired_devices()
            if key in dname.lower()]


def known_device_labels() -> list[str]:
    """
    已知蓝牙设备的一行式标签（按名字去重、保序），供云端注入设备上下文。

    只列**已配对**的设备：`devices` 的历史缓存里混着几十台路过的邻居设备
    （K3 实测 41 条，已配对的只有 1 条），把它们塞进云端 prompt 既费 token，
    又容易被模型当成"用户说要连的设备"。
    BLE 随机地址每次重新配对都会变（同一只 LOGI M240 B 留下过 ...:42/:43/:44/:45/:46
    六个地址），所以给名字、不给历史地址 —— bt_connect 会按名字重新发现设备。
    未广播名称、被 bluez 用 MAC 当名字的设备仍然给出地址，否则模型没有线索。
    """
    labels: list[str] = []
    seen: set[str] = set()
    for mac, name in _list_paired_devices():
        label = mac if _is_placeholder_name(name, mac) else name
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return labels


def _connect_hint(raw: str) -> str:
    """把 bluez 的失败文本翻成一句人话；没命中就返回空串。"""
    text = raw.lower()
    for needle, hint in _CONNECT_ERROR_HINTS:
        if needle in text:
            return hint
    return ""


def _info_fields(mac: str) -> dict[str, str]:
    """读 `info {mac}` 的关键字段（connected/paired/trusted/servicesresolved）。"""
    fields: dict[str, str] = {}
    s = _session([(f"info {mac}", 0.6)], timeout=10)
    if not s.started:
        return fields
    for line in _iter_lines(s.raw, drop=s.echoed):
        m = re.match(
            r"^(Connected|Paired|Trusted|Bonded|ServicesResolved):\s*(yes|no)$",
            line, re.IGNORECASE,
        )
        if m:
            fields[m.group(1).lower()] = m.group(2).lower()
    return fields


def _connected_state(mac: str, attempts: int = 4,
                     interval: float = 1.0) -> dict[str, bool]:
    """
    轮询 `info {mac}` 直到 `Connected: yes`，返回最后一次读到的状态。

    bluetoothctl 的 connect 输出不稳定（有时只有 "Attempting to connect"，
    成功文案也可能不给），所以成败一律以 `info` 的 Connected 字段为准。
    """
    state: dict[str, bool] = {}
    for attempt in range(attempts):
        fields = _info_fields(mac)
        if fields:
            state = {k: v == "yes" for k, v in fields.items()}
        if state.get("connected"):
            return state
        if attempt != attempts - 1:
            _sleep_interruptible(interval)
    return state


def _cached_matches(name: str) -> list[tuple[str, str]]:
    """bluez 已知设备（`devices` 缓存）里名字匹配查询串的条目。"""
    key = name.strip().lower()
    return [(mac, dname) for mac, dname in _list_known_devices()
            if key in dname.lower()]


def _match_name(candidate: str, query: str, mac: str) -> bool:
    """设备名匹配：不区分大小写子串；BLE 未广播名称时的 MAC 占位名不算。"""
    if not candidate or _is_placeholder_name(candidate, mac):
        return False
    return query.strip().lower() in candidate.strip().lower()


def _connect_mac(mac: str, dname: str) -> ExecResult:
    """trust + connect 指定地址，并用 `info` 二次核实是否真的连上。"""
    s = _session([(f"trust {mac}", 0.4), (f"connect {mac}", 6.0)], timeout=25)
    if not s.started:
        return ExecResult(False, "连接失败", s.error, detail_format="text")
    state = _connected_state(mac)
    if state.get("connected"):
        detail = "" if state.get("servicesresolved") else \
            "设备服务尚未就绪（HID/音频可能还需几秒）"
        return ExecResult(True, f"已连接蓝牙：{dname}", detail)
    hint = _connect_hint(s.raw)
    return ExecResult(False, f"连接失败：{dname}（{hint or '设备未响应或拒绝连接'}）",
                      _clean_detail(s.raw, s.echoed), detail_format="text")


def _connect_via_discovery(device: str) -> ExecResult:
    """
    按设备名连接：先在本次发现窗口里定位「当前地址」，再在同一会话里配对/连接。

    为什么必须这样（K3 实测）：
      - BLE 设备用随机地址，且每次重新配对都会换一个。同一只 LOGI M240 B 在
        bluez 缓存里留下过 ...:42/:43/:44/:45/:46 六个地址；拿缓存里的旧地址
        connect 一定失败（not available / le-connection-abort-by-local），
        看起来却像"设备未响应"，其实设备就在旁边。
      - 一旦离开发现窗口，bluez 对刚发现的地址直接回 `Device ... not available`，
        所以 pair/trust/connect 必须和 `scan on` 共用同一个 bluetoothctl 会话。
    """
    picked: dict[str, str] = {}

    def _pick_and_pair(raw: str) -> str | None:
        found = _parse_discovered(raw, since="scan on")
        known = _parse_device_list(raw)
        cands: list[tuple[str, str, int]] = []
        for mac, info in found.items():
            dname = str(info.get("name") or "") or known.get(mac, "")
            if not _match_name(dname, device, mac):
                continue
            rssi = info.get("rssi")
            cands.append((mac, dname, rssi if isinstance(rssi, int) else -999))
        if not cands:
            return None
        # 信号最强的就是当前真正在广播的那台（历史地址不会有 RSSI）
        cands.sort(key=lambda c: -c[2])
        picked["mac"], picked["name"] = cands[0][0], cands[0][1]
        # 新地址没有配对记录，必须趁发现窗口还在时配对
        return f"pair {picked['mac']}"

    def _then(cmd: str):
        def step(_raw: str) -> str | None:
            return f"{cmd} {picked['mac']}" if picked.get("mac") else None
        return step

    s = _session(
        [("power on", 0.5), ("show", 0.5),
         ("scan off", 0.4),
         ("scan on", _CONNECT_SCAN_SECONDS),
         ("devices", 0.6),
         (_pick_and_pair, 8.0),
         (_then("trust"), 0.4),
         (_then("connect"), 6.0),
         ("scan off", 0.3)],
        timeout=_CONNECT_SCAN_SECONDS + 40,
    )
    if not s.started:
        return ExecResult(False, "连接失败", s.error, detail_format="text")
    mac = picked.get("mac", "")
    if not mac:
        # 名字在缓存里有、但本次窗口里没在广播 → 缓存里的地址已经过期
        hint = ("请确认设备已开机并处于可连接/可配对状态；"
                "BLE 设备重新配对后地址会变，历史记录里的地址已经失效")
        if _discovering(s.raw, s.echoed) is True:
            hint += "；另外适配器上已有其它扫描在进行，本次结果可能不完整，可稍后再试"
        return ExecResult(False, f"未发现正在广播的蓝牙设备：{device}", hint)

    state = _connected_state(mac)
    dname = picked.get("name") or device
    if state.get("connected"):
        detail = f"设备地址：{mac}（本次扫描窗口内发现）"
        if not state.get("servicesresolved"):
            detail += "\n设备服务尚未就绪（HID/音频可能还需几秒）"
        return ExecResult(True, f"已连接蓝牙：{dname}", detail)
    hint = _connect_hint(s.raw)
    return ExecResult(
        False, f"连接失败：{dname}（{hint or '设备未响应或拒绝连接'}）",
        "\n".join(x for x in (f"设备地址：{mac}", _clean_detail(s.raw, s.echoed)) if x),
        detail_format="text",
    )


def bt_connect(device: str = "", **_) -> ExecResult:
    """
    连接指定蓝牙设备（设备名模糊匹配 or MAC）。

    设备名分两步：先试 bluez 已知设备里的唯一同名条目（快，几秒出结果）；
    失败再开一次发现窗口、按名字找「当前正在广播」的地址重新配对连接 ——
    BLE 随机地址会变，只有这一步能救回"设备就在旁边却连不上"的情况。
    """
    device = device.strip()
    if not device or _looks_like_category(device):
        # 只说了类别词（"连接蓝牙耳机"）或没给名字 → 反问，并把已配对的设备摆出来
        # 让用户直接挑一个，而不是拿"耳机"去连（必然失败）。
        labels = known_device_labels()
        hint = "例如：连接蓝牙 AirPods  /  连接蓝牙 88:C9:E8:XX:XX:XX"
        if labels:
            hint = f"已配对的设备：{'、'.join(labels[:5])}\n{hint}"
        return ExecResult(
            False,
            "请告诉我要连接的蓝牙设备名或 MAC",
            hint,
        )

    # 明确给了 MAC：直接连（地址是否有效由结果如实说明）
    if _MAC_RE.match(device):
        return _connect_mac(device.upper(), device.upper())

    cached = _cached_matches(device)
    if not cached:
        reason = f"系统里没有名为「{device}」的蓝牙记录"
    elif len(cached) > 1:
        # 同名多条 = 该设备换过地址，缓存里的基本都是历史地址
        reason = f"系统里有 {len(cached)} 个同名历史设备（蓝牙随机地址会变）"
    else:
        mac, dname = cached[0]
        if _info_fields(mac).get("connected") == "yes":
            return ExecResult(True, f"已连接蓝牙：{dname}")
        r = _connect_mac(mac, dname)
        if r.ok:
            return r
        reason = f"已知设备 {dname} 直连失败：{r.message}"

    r = _connect_via_discovery(device)
    if r.ok:
        return r
    return ExecResult(False, r.message, "\n".join(x for x in (reason, r.detail) if x), detail_format=r.detail_format)


def bt_disconnect(**_) -> ExecResult:
    """断开当前所有蓝牙连接（蓝牙适配器仍开启）。"""
    s = _session([("disconnect", 1.0)], timeout=15)
    if not s.started:
        return ExecResult(False, "断开失败", s.error, detail_format="text")
    detail = _clean_detail(s.raw, s.echoed)
    # 未连接时 bluetoothctl 会输出 "Missing device address argument"
    if "missing device" in detail.lower():
        return ExecResult(True, "当前没有活动的蓝牙连接")
    if "failed" in detail.lower() or "not available" in detail.lower():
        return ExecResult(False, "断开失败", detail, detail_format="text")
    return ExecResult(True, "已断开当前蓝牙连接")


def bt_forget(device: str = "", **_) -> ExecResult:
    """取消配对（从系统蓝牙列表移除）。"""
    if not device or _looks_like_category(device):
        return ExecResult(
            False,
            "请告诉我要取消配对的设备名或 MAC",
            "例如：取消配对 AirPods",
        )
    mac, dname = _resolve_device(device)
    if not mac:
        return ExecResult(False, f"未找到已配对设备：{device}")
    s = _session([(f"remove {mac}", 1.5)], timeout=15)
    if not s.started:
        return ExecResult(False, f"取消配对失败：{dname}", s.error, detail_format="text")
    detail = _clean_detail(s.raw, s.echoed)
    if "not available" in detail.lower() or "failed" in detail.lower():
        return ExecResult(False, f"取消配对失败：{dname}", detail, detail_format="text")
    return ExecResult(True, f"已取消配对：{dname}")


# ── 蓝牙文件接收目录 ───────────────────────────────────────────────
# 说明：obex-agent（obexpushd / gnome-user-share）实现各异，此处只做
# 声明式记录：把目标目录写到用户级配置，具体后端由 systemd unit 消费。

_RECV_DIR_CONF = "~/.config/bibit/bluetooth_recv_dir"


def bt_set_recv_dir(path: str = "", **_) -> ExecResult:
    """
    设置蓝牙文件接收目录（Skill 层只落到配置文件，具体后端由 obex 服务读取）。
    """
    if not path:
        return ExecResult(False, "请提供目录路径", "例如：切换蓝牙接收目录到 ~/Desktop")
    from pathlib import Path
    target = Path(os.path.expanduser(path))
    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ExecResult(False, f"目录创建失败：{target}", str(e))
    conf = Path(os.path.expanduser(_RECV_DIR_CONF))
    try:
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(str(target) + "\n", encoding="utf-8")
    except OSError as e:
        return ExecResult(False, "写入配置失败", str(e))
    return ExecResult(True, f"蓝牙接收目录已设为：{target}",
                      "重启 obex 后端服务生效（如 obexpushd / gnome-user-share）")


# ── 分发表 ─────────────────────────────────────────────────────────
# 工具名 → 执行函数（Skill 加载时并入主 HANDLERS）
HANDLERS = {
    "bt_on": bt_on,
    "bt_off": bt_off,
    "bt_scan": bt_scan,
    "bt_connect": bt_connect,
    "bt_disconnect": bt_disconnect,
    "bt_set_recv_dir": bt_set_recv_dir,
    "bt_forget": bt_forget,
}
