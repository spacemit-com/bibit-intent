"""
tools/display.py — 屏幕显示控制（分辨率/刷新率/旋转/缩放/外接屏模式）

对应 tools.yaml 中 set_resolution / set_refresh_rate / rotate_screen /
set_aspect_ratio / set_display_mode / display_modes 六个工具。

实现路径与设置中心（/usr/bin/bianbu-displays，控制中心插件
libbianbu-displays-plugin.so）保持一致，两条路各管一段：

  即时生效：wlr-randr（wlroots output 管理协议）
  持久化  ：写 ~/.config/kanshi/config，再 pkill --signal SIGHUP kanshi 重载

四项 UI 设置与命令行参数的对应（Resolution / Refresh Rate / Rotation / Scale）：

  分辨率 + 刷新率  同一个 --mode 参数，refresh 是 mode 的一部分
                   wlr-randr --output DP-1 --mode 1920x1080@60.000Hz
  旋转             wlr-randr --output DP-1 --transform 90
  缩放             wlr-randr --output DP-1 --scale 1.25

⚠️ 安全（对照 design.md §12 + security-notes.md）：
  - 全部走参数化 subprocess（列表形式），绝不 shell 字符串拼接
  - kanshi 配置写入前先备份 config.bak，与设置中心行为一致
  - 未探测到 Wayland 会话 / 无输出设备时返回明确错误，不静默失败
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .system_tools import ExecResult, _find_desktop_session_env, _run
from .md_format import md_table as _md_table

# wlr-randr --transform 合法值
_TRANSFORMS = ("normal", "90", "180", "270",
               "flipped", "flipped-90", "flipped-180", "flipped-270")

# tools.yaml 里 rotate_screen 的 angle(int) → --transform 值
_ANGLE2TRANSFORM = {0: "normal", 90: "90", 180: "180", 270: "270"}

# set_aspect_ratio 支持的缩放档位（与 tools.yaml enum 一致）
_SCALE_STEPS = ("1.00", "1.25", "1.50", "1.75", "2.00")

# 相对档位；绝对值（Hz / WxH）走 rate / size 参数
_REL_MODES = ("up", "down", "max", "min")

# 刷新率匹配容差（wlr-randr 报 59.939999 这类值，用户会说 60）
_HZ_TOLERANCE = 0.6


# ── 会话环境 ──────────────────────────────────────────────────────────

def _wayland_env() -> tuple[dict | None, str]:
    """
    构造 wlr-randr 可用的环境。

    从 SSH / systemd 进来时 XDG_SESSION_TYPE 是 tty，缺
    XDG_RUNTIME_DIR / WAYLAND_DISPLAY，wlr-randr 连不上 compositor。
    复用 lock_screen 那套会话探测（读桌面会话进程的 /proc/<pid>/environ）。
    返回 (env, err)；env 为 None 时 err 是给用户看的原因。
    """
    env = _find_desktop_session_env()
    if env is None:
        return None, "未找到桌面会话（缺 WAYLAND_DISPLAY/XDG_RUNTIME_DIR），无法调整显示设置"
    full = dict(os.environ)
    full.update(env)
    return full, ""


# ── wlr-randr 查询 ────────────────────────────────────────────────────

def _query_outputs() -> tuple[list[dict] | None, str]:
    """
    `wlr-randr --json` 解析出输出列表。失败返回 (None, 原因)。

    只保留脚本需要的字段，结构对齐 wlr-randr 的 JSON：
      name, enabled, transform, scale, modes[{width,height,refresh,preferred,current}]
    """
    env, err = _wayland_env()
    if env is None:
        return None, err
    r = _run(["wlr-randr", "--json"], timeout=10, env=env)
    if not r.ok:
        if "命令不存在" in r.message:
            return None, "命令不存在：wlr-randr（该设备可能不是 wlroots 桌面）"
        return None, f"读取显示信息失败：{r.detail or r.message}"
    try:
        data = json.loads(r.detail)
    except ValueError:
        return None, "wlr-randr --json 输出无法解析"
    if not isinstance(data, list) or not data:
        return None, "未检测到任何显示输出"
    return data, ""


def _pick_output(outputs: list[dict]) -> dict | None:
    """选择要操作的输出：优先已启用的第一个，否则列表第一个。"""
    for o in outputs:
        if o.get("enabled"):
            return o
    return outputs[0] if outputs else None


def _fmt_refresh(hz: float) -> str:
    """
    刷新率格式化成 wlr-randr 接受的写法。

    JSON 里是 59.939999 这类浮点，--mode 需要 1920x1080@59.939999Hz；
    整数值写成 60.000Hz（与 kanshi 配置里设置中心写的格式一致）。
    """
    return f"{hz:.3f}" if abs(hz - round(hz)) > 1e-6 else f"{round(hz):d}.000"


def _mode_str(m: dict) -> str:
    return f"{m['width']}x{m['height']}@{_fmt_refresh(float(m.get('refresh', 0) or 0))}Hz"


def _current_mode(out: dict) -> dict | None:
    modes = out.get("modes") or []
    for m in modes:
        if m.get("current"):
            return m
    for m in modes:
        if m.get("preferred"):
            return m
    return modes[0] if modes else None


def _distinct_modes(out: dict) -> list[dict]:
    """dedup 后的 mode 列表：同一 (宽,高,刷新率) 只留一条，优先 current/preferred。

    内核常把同一档位重复上报（实测 1920x1080@60 有两条），按列表下标取
    「相邻档位」会取到重复项，表现为「调了没反应」。
    """
    seen: dict[tuple[int, int, int], dict] = {}
    for m in out.get("modes") or []:
        w, h = int(m.get("width", 0)), int(m.get("height", 0))
        hz = round(float(m.get("refresh", 0) or 0), 3)
        if w <= 0 or h <= 0 or hz <= 0:
            # 有些驱动会多报一条没有刷新率/宽高的空 mode；带进来会渲染成
            # "…/0/60Hz"，也会让 _find_hz 把 want≈0 的请求匹配到它上面。
            continue
        key = (w, h, int(round(hz * 1000)))
        cur = seen.get(key)
        if cur is None or (
            (m.get("current") or m.get("preferred"))
            and not (cur.get("current") or cur.get("preferred"))
        ):
            seen[key] = m
    return [seen[k] for k in sorted(seen)]


def _rates_of(out: dict, w: int, h: int) -> list[dict]:
    """某分辨率下可用刷新率（升序、已去重）。"""
    same = [
        m for m in _distinct_modes(out)
        if int(m.get("width", 0)) == w and int(m.get("height", 0)) == h
    ]
    return sorted(same, key=lambda m: float(m.get("refresh", 0) or 0))


def _hz_list(modes: list[dict]) -> str:
    return "/".join(f"{float(m.get('refresh', 0) or 0):g}" for m in modes)


def _find_hz(out: dict, w: int, h: int, want: float) -> tuple[dict | None, list[dict]]:
    """在 (w,h) 内找目标刷新率；返回 (命中的 mode, 该分辨率全部档位)。"""
    modes = _rates_of(out, w, h)
    best: dict | None = None
    gap = None
    for m in modes:
        d = abs(float(m.get("refresh", 0) or 0) - want)
        if d <= _HZ_TOLERANCE and (gap is None or d < gap):
            best, gap = m, d
    return best, modes


def _parse_size(raw: str) -> tuple[int, int, float | None] | None:
    """解析 "1920x1080" / "1920*1080" / "1920×1080@50" → (宽,高,Hz|None)。"""
    m = re.match(
        r"^\s*(\d{3,4})\s*[x×*]\s*(\d{3,4})\s*(?:@\s*(\d{2,3}(?:\.\d+)?)\s*(?:hz)?)?\s*$",
        str(raw or ""), re.IGNORECASE,
    )
    if not m:
        return None
    hz = float(m.group(3)) if m.group(3) else None
    return int(m.group(1)), int(m.group(2)), hz


def _res_label(m: dict) -> str:
    return f"{int(m.get('width', 0))}x{int(m.get('height', 0))}"


def _available_resolutions(out: dict) -> str:
    return "/".join(dict.fromkeys(_res_label(m) for m in _distinct_modes(out)))


def _apply(args: list[str]) -> ExecResult:
    """执行一次 wlr-randr 调整。args 不含 wlr-randr 自身。"""
    env, err = _wayland_env()
    if env is None:
        return ExecResult(False, err)
    r = _run(["wlr-randr"] + args, timeout=15, env=env)
    if r.ok:
        return ExecResult(True, "ok", r.detail)
    if "命令不存在" in r.message:
        return ExecResult(False, "命令不存在：wlr-randr（该设备可能不是 wlroots 桌面）")
    return ExecResult(False, f"设置失败：{r.detail or r.message}")


# ── kanshi 持久化 ─────────────────────────────────────────────────────

def _kanshi_config_path(env: dict | None) -> Path | None:
    """kanshi 配置路径 ~/.config/kanshi/config，HOME 取会话用户的。"""
    home = (env or {}).get("HOME") or os.environ.get("HOME")
    if not home:
        return None
    return Path(home) / ".config" / "kanshi" / "config"


def _persist(out_name: str, **fields) -> str:
    """
    把当前设置写进 ~/.config/kanshi/config 并给 kanshi 发 SIGHUP。

    与设置中心一致：先备份 config.bak，再整段重写 profile。fields 可含
    mode / transform / scale / position，缺的字段沿用文件里已有值。
    返回给用户的补充说明（空串表示持久化成功且无需提示）。
    """
    env, _ = _wayland_env()
    cfg = _kanshi_config_path(env)
    if cfg is None:
        return "（未能定位 kanshi 配置，重启后可能失效）"

    # 读旧值：只解析我们关心的 output 行
    old: dict[str, str] = {}
    text = ""
    if cfg.exists():
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("output "):
                continue
            if out_name not in s:
                continue
            for key in ("mode", "transform", "scale", "position"):
                m = re.search(rf"\b{key}\s+(\S+)", s)
                if m:
                    old[key] = m.group(1)
            break

    merged = {**old, **{k: v for k, v in fields.items() if v is not None}}
    parts = [f"output {out_name} enable"]
    for key in ("mode", "position", "transform", "scale"):
        if merged.get(key):
            parts.append(f"{key} {merged[key]}")
    body = "profile {\n    " + " ".join(parts) + "\n}\n"

    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        if text:
            cfg.with_suffix(".bak").write_text(text, encoding="utf-8")
        cfg.write_text(body, encoding="utf-8")
    except OSError as e:
        return f"（已即时生效，但写 kanshi 配置失败：{e}，重启后会复原）"

    # kanshi 在跑才发信号；没跑不算错误（下次启动会读新配置）
    if _run(["pgrep", "kanshi"], timeout=5).ok:
        _run(["pkill", "--signal", "SIGHUP", "kanshi"], timeout=5)
    return ""


# ── 工具实现 ──────────────────────────────────────────────────────────

def _mode_by_step(out: dict, direction: str) -> tuple[dict | None, str]:
    """
    按分辨率档位挑 mode。direction: up/down/max/min。

    排序键用像素总数（width*height），同分辨率保留刷新率最高的一档，
    避免"调高分辨率"在同一分辨率的不同刷新率之间空转、或档位倒挂。
    """
    modes = out.get("modes") or []
    if not modes:
        return None, "该输出没有可用显示模式"
    best: dict[tuple[int, int], dict] = {}
    for m in modes:
        key = (int(m.get("width", 0)), int(m.get("height", 0)))
        cur = best.get(key)
        if cur is None or float(m.get("refresh", 0) or 0) > float(cur.get("refresh", 0) or 0):
            best[key] = m
    keys = sorted(best, key=lambda k: (k[0] * k[1], k[0]))
    ordered = [best[k] for k in keys]
    if direction == "max":
        return ordered[-1], ""
    if direction == "min":
        return ordered[0], ""
    idx = 0
    cur = _current_mode(out)
    if cur is not None:
        ck = (int(cur.get("width", 0)), int(cur.get("height", 0)))
        idx = keys.index(ck) if ck in keys else 0
    nxt = idx + 1 if direction == "up" else idx - 1
    if nxt < 0 or nxt >= len(ordered):
        edge = "最高" if direction == "up" else "最低"
        return None, f"已经是{edge}分辨率了（当前 {_res_label(ordered[idx])}）"
    return ordered[nxt], ""


def _mode_for_size(out: dict, w: int, h: int) -> dict | None:
    """给定分辨率（用户没指定刷新率）挑一档 mode。

    _distinct_modes 是按 (宽,高,刷新率) 升序排的，直接取第一条会拿到最低刷新率
    （实测 1920x1080 会变 29.97Hz，画面发暗发闪）。偏好顺序：
    ① 同分辨率下的当前刷新率（最不打扰）→ ② preferred 档 → ③ 最高刷新率。
    """
    rates = _rates_of(out, w, h)
    if not rates:
        return None
    cur = _current_mode(out)
    if cur is not None and int(cur.get("width", 0)) == w and int(cur.get("height", 0)) == h:
        cur_hz = float(cur.get("refresh", 0) or 0)
        same = next(
            (m for m in rates if abs(float(m.get("refresh", 0) or 0) - cur_hz) < 1e-6),
            None,
        )
        if same is not None:
            return same
    return next((m for m in rates if m.get("preferred")), None) or rates[-1]


def set_resolution(mode: str | None = None, size: str | None = None, **_) -> ExecResult:
    """分辨率调节：size 给绝对值（1920x1080，可带 @50），mode 给相对档位。"""
    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    out = _pick_output(outputs)
    if out is None:
        return ExecResult(False, "未检测到可用显示输出")

    # 兼容把绝对值塞进 mode 的调用方（云端/端侧模型只填了 mode）
    if not size and mode and str(mode).strip().lower() not in _REL_MODES:
        size, mode = str(mode).strip(), None

    if size:
        parsed = _parse_size(size)
        if parsed is None:
            return ExecResult(
                False,
                f"分辨率要写成 WxH（如 1920x1080，可带刷新率 1920x1080@50）；"
                f"当前屏幕可选 {_available_resolutions(out)}",
            )
        w, h, hz = parsed
        if hz is None:
            target = _mode_for_size(out, w, h)
        else:
            target, _ = _find_hz(out, w, h, hz)
        if target is None:
            if hz is None:
                return ExecResult(
                    False,
                    f"该屏幕不支持 {w}x{h}；可选分辨率 {_available_resolutions(out)}",
                )
            return ExecResult(
                False,
                f"{w}x{h} 不支持 {hz:g}Hz；可用刷新率 {_hz_list(_rates_of(out, w, h))}Hz",
            )
        ms = _mode_str(target)
        r = _apply(["--output", out["name"], "--mode", ms])
        if not r.ok:
            return r
        note = _persist(out["name"], mode=ms)
        hz_now = float(target.get("refresh", 0) or 0)
        return ExecResult(
            True, f"分辨率已设为 {_res_label(target)}@{hz_now:.2f}Hz{note}",
        )

    if mode is None:
        return ExecResult(
            False,
            f"还差一个参数：目标分辨率（如 1920x1080）或 up/down/max/min；"
            f"当前可选 {_available_resolutions(out)}",
            need_param="size",
        )
    if str(mode).strip().lower() not in _REL_MODES:
        return ExecResult(
            False,
            f"{mode} 不是分辨率；分辨率请给 WxH（如 1920x1080），"
            f"调节方向用 up/down/max/min，当前可选 {_available_resolutions(out)}",
        )
    mode = str(mode).strip().lower()
    target, err = _mode_by_step(out, mode)
    if target is None:
        return ExecResult(False, err)
    ms = _mode_str(target)
    r = _apply(["--output", out["name"], "--mode", ms])
    if not r.ok:
        return r
    note = _persist(out["name"], mode=ms)
    return ExecResult(True, f"分辨率已设为 {target['width']}x{target['height']}{note}")


def _apply_refresh(out: dict, target: dict, w: int, h: int) -> ExecResult:
    ms = _mode_str(target)
    r = _apply(["--output", out["name"], "--mode", ms])
    if not r.ok:
        return r
    note = _persist(out["name"], mode=ms)
    hz = float(target.get("refresh", 0) or 0)
    return ExecResult(True, f"刷新率已设为 {hz:.2f}Hz（{w}x{h}）{note}")


def set_refresh_rate(mode: str | None = None, rate: float | None = None, **_) -> ExecResult:
    """
    刷新率调节：rate 给绝对值（Hz），mode 给相对档位。

    wlr-randr 没有独立的 refresh 参数，refresh 是 mode 字符串的一部分；
    绝对值必须先在该分辨率的可用档位里命中，不命中就明确报可选值，
    不再悄悄改成别的档位。
    """
    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    out = _pick_output(outputs)
    if out is None:
        return ExecResult(False, "未检测到可用显示输出")
    cur = _current_mode(out)
    if cur is None:
        return ExecResult(False, "该输出没有可用显示模式")
    w, h = int(cur.get("width", 0)), int(cur.get("height", 0))
    rates = _rates_of(out, w, h)
    if not rates:
        return ExecResult(False, f"{w}x{h} 没有可用刷新率")

    # 兼容把 Hz 塞进 mode 的调用方（云端/端侧模型只填了 mode）
    if rate is None and mode is not None and str(mode).strip().lower() not in _REL_MODES:
        # 必须连小数一起取，"59.94" 只取到 "59" 就调错了档
        digits = re.search(r"\d{2,3}(?:\.\d+)?", str(mode))
        if digits:
            rate, mode = float(digits.group(0)), None

    if rate is not None:
        want = float(rate)
        hit, rates = _find_hz(out, w, h, want)
        if hit is None:
            return ExecResult(
                False,
                f"{w}x{h} 不支持 {want:g}Hz；可用刷新率 {_hz_list(rates)}Hz。"
                f"逐档调节可以说「刷新率up / 刷新率down」",
            )
        return _apply_refresh(out, hit, w, h)

    if mode is None:
        return ExecResult(
            False,
            f"还差一个参数：目标刷新率（Hz，如 50）或 up/down/max/min；"
            f"{w}x{h} 当前 {float(cur.get('refresh', 0) or 0):.2f}Hz，"
            f"可用 {_hz_list(rates)}Hz",
            need_param="rate",
        )
    if str(mode).strip().lower() not in _REL_MODES:
        return ExecResult(
            False,
            f"{mode} 不是刷新率档位；可用 up/down/max/min，或直接给 Hz（如 50），"
            f"{w}x{h} 可用 {_hz_list(rates)}Hz",
        )
    mode = str(mode).strip().lower()
    if len(rates) < 2:
        return ExecResult(
            False, f"{w}x{h} 只有 {_hz_list(rates)}Hz 一个刷新率可选，无法上下调节",
        )
    cur_hz = float(cur.get("refresh", 0) or 0)
    ci = next(
        (i for i, m in enumerate(rates)
         if abs(float(m.get("refresh", 0) or 0) - cur_hz) < 1e-6),
        0,
    )
    if mode == "max":
        if ci >= len(rates) - 1:
            return ExecResult(False, f"已经是{w}x{h} 最高刷新率了（{_hz_list(rates)}Hz）")
        target = rates[-1]
    elif mode == "min":
        if ci <= 0:
            return ExecResult(False, f"已经是{w}x{h} 最低刷新率了（{_hz_list(rates)}Hz）")
        target = rates[0]
    else:
        ni = ci + 1 if mode == "up" else ci - 1
        if ni < 0 or ni >= len(rates):
            edge = "最高" if mode == "up" else "最低"
            return ExecResult(False, f"已经是{w}x{h} {edge}刷新率了（{_hz_list(rates)}Hz）")
        target = rates[ni]
    return _apply_refresh(out, target, w, h)


def rotate_screen(angle: int = 90, **_) -> ExecResult:
    """旋转：wlr-randr --transform normal|90|180|270。"""
    try:
        angle = int(angle) % 360
    except (TypeError, ValueError):
        return ExecResult(False, f"无效的旋转角度：{angle!r}")
    tf = _ANGLE2TRANSFORM.get(angle)
    if tf is None:
        return ExecResult(False, f"只支持 0/90/180/270 度，收到 {angle}")
    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    out = _pick_output(outputs)
    if out is None:
        return ExecResult(False, "未检测到可用显示输出")
    r = _apply(["--output", out["name"], "--transform", tf])
    if not r.ok:
        return r
    note = _persist(out["name"], transform=tf)
    label = "恢复正常方向" if angle == 0 else f"旋转 {angle} 度"
    return ExecResult(True, f"屏幕已{label}{note}")


def _norm_scale(raw: str) -> tuple[str | None, str]:
    """缩放比例归一化：1 / 1.5 / 125% / 150 / 2倍 → 五档规范值。

    返回 (规范值 | None, 备注)；None 表示既不是五档也换算不出来。
    用户在设置里能点的是 1.00/1.25/1.50/1.75/2.00，所以：
      - 百分比（125% / 不带 % 的 125）先 /100；
      - 落不到档位上的（1.2）取最近档并说明，避免静默改档或直接报错。
    """
    s = str(raw or "").strip().lower().replace("倍", "").replace("×", "x").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        v = float(s)
    except ValueError:
        return None, ""
    if pct or v > 2.5:
        v /= 100.0
    if not (0.5 <= v <= 2.5):
        return None, ""
    steps = [float(x) for x in _SCALE_STEPS]
    best = min(steps, key=lambda t: abs(t - v))
    if abs(best - v) < 1e-6:
        return f"{best:.2f}", ""
    return f"{best:.2f}", f"（{raw} 不在 {'/'.join(_SCALE_STEPS)} 里，已按最接近的 {best:.2f} 处理）"


def set_aspect_ratio(ratio: str | None = None, **_) -> ExecResult:
    """缩放（设置中心里的「屏幕比例调节」）：wlr-randr --scale。"""
    if ratio is None:
        return ExecResult(
            False,
            f"还差一个参数：缩放比例（{'/'.join(_SCALE_STEPS)}，也可说 1 / 1.5 / 125%）",
            need_param="ratio",
        )
    val, note = _norm_scale(ratio)
    if val is None:
        return ExecResult(
            False,
            f"缩放比例只支持 {'/'.join(_SCALE_STEPS)}（也可写 1 / 1.5 / 125%），收到 {ratio}",
        )
    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    out = _pick_output(outputs)
    if out is None:
        return ExecResult(False, "未检测到可用显示输出")
    r = _apply(["--output", out["name"], "--scale", val])
    if not r.ok:
        return r
    persist_note = _persist(out["name"], scale=val)
    return ExecResult(True, f"屏幕缩放已设为 {val}{note}{persist_note}")


def set_display_mode(mode: str | None = None, **_) -> ExecResult:
    """
    外接屏显示模式：extend / mirror / primary / secondary。

    wlr-randr 没有 --extend/--mirror 这类开关，用多个 --output 段组合：
      extend    两屏都开，第二屏 position 放到第一屏右侧
      mirror    两屏都开，position 都设 0,0（wlroots 下同位置即镜像）
      primary   只开主屏，关掉其余
      secondary 只开外接屏，关掉主屏
    """
    if mode is None:
        return ExecResult(False, "还差一个参数：显示模式（扩展/复制/主屏/副屏）",
                          need_param="mode")
    mode = str(mode).strip()
    if mode not in ("extend", "mirror", "primary", "secondary"):
        return ExecResult(False, f"不支持的显示模式：{mode}")

    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    if len(outputs) < 2 and mode in ("extend", "mirror", "secondary"):
        return ExecResult(False, "只检测到一个显示输出，没有外接屏可切换")

    primary, secondary = outputs[0], (outputs[1] if len(outputs) > 1 else None)

    if mode == "primary":
        args = ["--output", primary["name"], "--on"]
        for o in outputs[1:]:
            args += ["--output", o["name"], "--off"]
        r = _apply(args)
        return r if not r.ok else ExecResult(
            True, f"已切换到仅主屏显示（{primary['name']}）")

    if mode == "secondary":
        args = ["--output", secondary["name"], "--on",
                "--output", primary["name"], "--off"]
        r = _apply(args)
        return r if not r.ok else ExecResult(
            True, f"已切换到仅外接屏显示（{secondary['name']}）")

    if mode == "mirror":
        args = ["--output", primary["name"], "--on", "--pos", "0,0",
                "--output", secondary["name"], "--on", "--pos", "0,0"]
        r = _apply(args)
        if not r.ok:
            return r
        note = _persist(primary["name"], position="0,0")
        return ExecResult(True, f"已切换到复制模式（{primary['name']} / {secondary['name']}）{note}")

    # extend：第二屏挪到第一屏右边，x 偏移取主屏当前宽度
    cur = _current_mode(primary)
    x = int(cur.get("width", 0)) if cur else 0
    args = ["--output", primary["name"], "--on", "--pos", "0,0",
            "--output", secondary["name"], "--on", "--pos", f"{x},0"]
    r = _apply(args)
    if not r.ok:
        return r
    note = _persist(primary["name"], position="0,0")
    return ExecResult(True, f"已切换到扩展模式（{secondary['name']} 位于右侧）{note}")


# ── 只读查询 ──────────────────────────────────────────────────────────

def current_display_info() -> ExecResult | None:
    """
    当前输出与分辨率/刷新率（只读）。

    复用 Wayland 会话环境；无桌面会话时返回 None，由调用方决定降级
    （如 xrandr / 明确告知没有图形会话）。
    """
    outputs, err = _query_outputs()
    if outputs is None:
        return None if "未找到桌面会话" in err else ExecResult(False, err)
    out = _pick_output(outputs)
    if out is None:
        return ExecResult(False, "未检测到可用显示输出")
    rows: list[list[str]] = [
        ["输出", str(out.get("name") or "-")],
        ["状态", "已启用" if out.get("enabled") else "已关闭"],
    ]
    cur = _current_mode(out)
    if cur is not None:
        rows.append(["当前分辨率", _res_label(cur)])
        rows.append(["当前刷新率", f"{float(cur.get('refresh', 0) or 0):.2f}Hz"])
    rows.append(["旋转", str(out.get("transform") or "normal")])
    rows.append(["缩放", f"{float(out.get('scale', 1) or 1):.2f}"])
    return ExecResult(True, "屏幕分辨率", _md_table(["项目", "数值"], rows))


def display_modes(**_) -> ExecResult:
    """列出每个输出的分辨率与刷新率档位，并标出当前档（只读）。"""
    outputs, err = _query_outputs()
    if outputs is None:
        return ExecResult(False, err)
    blocks: list[str] = []
    for out in outputs:
        modes = _distinct_modes(out)
        if not modes:
            blocks.append(f"### {out.get('name') or '-'}\n没有可用显示模式")
            continue
        cur = _current_mode(out)
        cur_txt = (
            f"{_res_label(cur)}@{float(cur.get('refresh', 0) or 0):.2f}Hz"
            if cur is not None else "-"
        )
        by_res: dict[str, list[dict]] = {}
        for m in modes:
            by_res.setdefault(_res_label(m), []).append(m)
        rows = [
            [
                res,
                "/".join(f"{float(m.get('refresh', 0) or 0):g}" for m in ms),
                "当前" if any(m.get("current") for m in ms) else "",
            ]
            for res, ms in by_res.items()
        ]
        blocks.append(
            f"### {out.get('name') or '-'}（当前 {cur_txt}）\n"
            + _md_table(["分辨率", "刷新率(Hz)", "备注"], rows)
        )
    return ExecResult(True, "屏幕支持的显示模式", "\n\n".join(blocks))


# ── 分发表 ────────────────────────────────────────────────────────────
HANDLERS = {
    "set_resolution": set_resolution,
    "set_refresh_rate": set_refresh_rate,
    "rotate_screen": rotate_screen,
    "set_aspect_ratio": set_aspect_ratio,
    "set_display_mode": set_display_mode,
    "display_modes": display_modes,
}

# 反向注册：本模块与 tools/system.py 互相引用（复用其 ExecResult/_run/
# _find_desktop_session_env）。若本模块被先导入，system.py 末尾那句
# `from bibit_intent.execution.display_tools import HANDLERS` 会因本模块尚未建完而 ImportError
# 并跳过，届时 system.HANDLERS 里会缺这五个工具。这里补上，保证无论
# 谁先被导入，system.HANDLERS 都是全的。
_sys_mod = sys.modules.get("bibit_intent.system_tools")
if _sys_mod is not None and hasattr(_sys_mod, "HANDLERS"):
    _sys_mod.HANDLERS.update(HANDLERS)
