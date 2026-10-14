"""
tools/window.py — 窗口管理（切换/最小化/最大化/窗口列表）

对应 tools.yaml 中 window_list / window_activate / window_minimize /
window_maximize 四个工具。

labwc（Wayland）没有 xdotool/wmctrl 那套 X11 窗口工具，这里统一走
wlrctl toplevel（wlroots foreign-toplevel 管理协议）：
  - window_list     → wlrctl toplevel list
  - window_activate → wlrctl toplevel activate app_id:<id>
  - window_minimize → wlrctl toplevel minimize app_id:<id>
  - window_maximize → wlrctl toplevel maximize app_id:<id>

⚠️ 注意：
  - wlrctl 的 app_id/title 匹配是 strcmp 全等（大小写敏感、无子串），
    本模块先把用户口语（"终端/文件管理器"）或 app_id 子串解析成真实
    app_id 再交给 wlrctl，避免"终端"无法命中 qterminal。
  - minimize/maximize 是设置（非切换），没有 unminimize/unmaximize。
"""
from __future__ import annotations

import os
import sys

from .system_tools import (
    ExecResult,
    _app_aliases,
    _find_desktop_session_env,
    _get_desktop_apps,
    _match_desktop_app,
    _run,
)


def _wayland_env() -> tuple[dict | None, str]:
    """构造 wlrctl 可用的环境（与 display_tools 一致）。"""
    env = _find_desktop_session_env()
    if env is None:
        return None, "未找到桌面会话（缺 WAYLAND_DISPLAY/XDG_RUNTIME_DIR），无法操作窗口"
    full = dict(os.environ)
    full.update(env)
    return full, ""


def _wlrctl(args: list[str], timeout: int = 5) -> tuple[ExecResult | None, str]:
    """执行 `wlrctl toplevel <args>`。成功返回 (ExecResult, "")，失败返回 (None, 原因)。"""
    env, err = _wayland_env()
    if env is None:
        return None, err
    r = _run(["wlrctl", "toplevel"] + args, timeout=timeout, env=env)
    if r.ok:
        return r, ""
    if "命令不存在" in r.message:
        return None, "命令不存在：wlrctl（当前桌面可能不是 wlroots）"
    detail = (r.detail or "").strip().lower()
    if "interface not found" in detail or "not found" in detail:
        return None, "当前桌面会话不支持窗口管理（缺少 foreign-toplevel 协议）"
    return None, f"窗口操作失败：{r.detail or r.message}"


def _list_windows() -> tuple[list[dict] | None, str]:
    """解析 `wlrctl toplevel list` 输出为 [{app_id, title}, ...]。"""
    r, err = _wlrctl(["list"])
    if r is None:
        return None, err
    windows: list[dict] = []
    for line in r.detail.splitlines():
        line = line.strip()
        if not line:
            continue
        app_id, sep, title = line.partition(":")
        if not sep:
            continue
        app_id = app_id.strip()
        if not app_id:
            continue
        windows.append({"app_id": app_id, "title": title.strip()})
    return windows, ""


def _resolve_window(target: str, windows: list[dict]) -> tuple[str | None, str]:
    """把用户口语/子串解析成真实 app_id。返回 (app_id, "") 或 (None, 原因)。"""
    t = target.strip()
    if not t:
        return None, "窗口名称为空"
    # 去掉口语里的"窗口"后缀（"浏览器窗口"→"浏览器"）
    if t.endswith("窗口"):
        t = t[:-2].strip()
    if not t:
        return None, "窗口名称为空"
    # 显式 app_id:xxx（用户直接给协议匹配串）
    if ":" in t:
        return t, ""
    t_l = t.casefold()

    # 1. 内置中文别名（终端→qterminal / 浏览器→chromium ...）
    aliases = _app_aliases()
    mapped = aliases.get(target.casefold()) or aliases.get(t.casefold())
    if mapped:
        for w in windows:
            if w["app_id"].casefold() == mapped.casefold():
                return w["app_id"], ""

    # 2. .desktop 中文名 → stem(app_id)（如"文件管理器"→pcmanfm-qt）
    desktop = _match_desktop_app(t, _get_desktop_apps())
    if desktop:
        for w in windows:
            if w["app_id"].casefold() == desktop["stem"].casefold():
                return w["app_id"], ""

    # 3. 精确 app_id
    for w in windows:
        if w["app_id"].casefold() == t_l:
            return w["app_id"], ""
    # 4. 精确 title
    for w in windows:
        if w["title"].casefold() == t_l:
            return w["app_id"], ""
    # 5. app_id 子串（宽松兜底）
    for w in windows:
        if t_l in w["app_id"].casefold():
            return w["app_id"], ""
    # 6. title 子串（宽松兜底）
    for w in windows:
        if t_l in w["title"].casefold():
            return w["app_id"], ""

    return None, f"没有找到匹配的窗口「{target}」"


def _window_names(windows: list[dict]) -> str:
    """当前窗口名列表（提示用）。"""
    return "、".join(w["app_id"] for w in windows)


def _apply_window_action(verb: str, done: str, target: str | None) -> ExecResult:
    """解析目标窗口并执行 activate/minimize/maximize。"""
    if not target or not isinstance(target, str):
        return ExecResult(False, "还差一个参数：窗口名称或应用名", need_param="target")
    windows, err = _list_windows()
    if windows is None:
        return ExecResult(False, err)
    if not windows:
        return ExecResult(False, "当前没有打开的窗口，无法执行窗口操作")
    app_id, reason = _resolve_window(target, windows)
    if app_id is None:
        return ExecResult(False, f"{reason}。当前窗口：{_window_names(windows)}")
    r, err2 = _wlrctl([verb, f"app_id:{app_id}"])
    if r is None:
        return ExecResult(False, err2)
    return ExecResult(True, f"已{done}窗口：{app_id}")


def window_list(**_) -> ExecResult:
    """查询当前打开的窗口/应用列表（只读）。"""
    windows, err = _list_windows()
    if windows is None:
        return ExecResult(False, err)
    if not windows:
        return ExecResult(True, "当前没有打开的窗口")
    detail = "\n".join(f"{w['app_id']}: {w['title']}" for w in windows)
    return ExecResult(True, f"当前窗口：{_window_names(windows)}", detail)


def window_activate(target: str | None = None, **_) -> ExecResult:
    """切换/激活指定窗口。"""
    return _apply_window_action("activate", "激活", target)


def window_minimize(target: str | None = None, **_) -> ExecResult:
    """最小化指定窗口。"""
    return _apply_window_action("minimize", "最小化", target)


def window_maximize(target: str | None = None, **_) -> ExecResult:
    """最大化指定窗口。"""
    return _apply_window_action("maximize", "最大化", target)


HANDLERS = {
    "window_list": window_list,
    "window_activate": window_activate,
    "window_minimize": window_minimize,
    "window_maximize": window_maximize,
}

# 反向注册：本模块复用 system_tools 的 ExecResult/_run/_find_desktop_session_env，
# 两模块互相引用；若本模块被先导入，system_tools 末尾那句
# `from .window_tools import HANDLERS` 会 ImportError 跳过，这里补上。
_sys_mod = sys.modules.get("bibit_intent.execution.system_tools")
if _sys_mod is not None and hasattr(_sys_mod, "HANDLERS"):
    _sys_mod.HANDLERS.update(HANDLERS)
