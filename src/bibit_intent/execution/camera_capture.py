#!/usr/bin/env python3
"""camera_capture.py — 用 AT-SPI 触发 snapshot 的「拍摄照片」动作。

snapshot（GNOME Camera）没有命令行拍照入口，D-Bus 也不导出 take-picture；
但它把窗口动作 ``win.take-picture``（即"拍摄照片"按钮背后的同一个动作）
暴露在无障碍树上，本脚本据此触发拍照，产物路径/EXIF 与手点按钮完全一致。

为什么是独立脚本而不是 handlers 里的函数：bibit-intent 跑在
``/opt/bibit-intent/.venv``（Python 3.14，无 PyGObject），而 AT-SPI 需要
系统 ``/usr/bin/python3`` 的 ``gi.repository.Atspi``。因此由 handlers 以
子进程方式调用本脚本，环境变量注入桌面会话（XDG_RUNTIME_DIR /
WAYLAND_DISPLAY / DBUS_SESSION_BUS_ADDRESS / HOME）。

用法（stdout 输出单行 JSON）：
  camera_capture.py status            查看 snapshot 是否在跑、当前停在哪个页面
  camera_capture.py take              切回相机页并触发拍照，返回新文件路径
  camera_capture.py to-camera         仅切回相机页（相册页时用）
  camera_capture.py await-permission  提示用户点授权框的「允许」并等待结果

首次使用会弹系统授权框（xdg-desktop-portal-gtk），用户点「允许」之前
``win.take-picture`` 照旧挂在树上，但 snapshot 只回
"viewfinder is not in the READY state" 且不写文件。所以 ``take`` 必须先
识别这个状态（见 ``_auth_state``），否则会白等 12 秒再报 no_photo_written。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import re
import subprocess
import sys
import time

APP_NAME = "snapshot"
TAKE_ACTION = "win.take-picture"
GALLERY_ACTION = "win.toggle-gallery"
CAMERA_MARK = ("grouping", "相机")
GALLERY_MARK = ("grouping", "相册")
BACK_BTN = ("button", "Back")

# 摄像头授权状态存在 portal 的 PermissionStore 里（devices/camera/<app_id>），
# 值 yes/no；无记录即"还没问过用户"。与 xdg-desktop-portal 的
# AccessCamera 是同一份数据源，所以它比 UI 状态更早、更准。
PERMISSION_STORE = "org.freedesktop.impl.portal.PermissionStore"
PERMISSION_STORE_PATH = "/org/freedesktop/impl/portal/PermissionStore"
PERMISSION_TABLE = "devices"
PERMISSION_ID = "camera"
SNAPSHOT_APP_ID = "org.gnome.Snapshot"

# 授权框按钮：不同 portal 版本/locale 下是 Grant Access 或 允许/同意/授权
_GRANT_BUTTON_RE = re.compile(r"允许|同意|授权|allow|grant|permit", re.I)

_PUMP_SECONDS = 0.6
_ACTION_WAIT_SECONDS = 12.0
_POLL_INTERVAL = 0.4
_DIALOG_PUMP_SECONDS = 1.0
_GRANT_POLL_INTERVAL = 0.4

_NOTIFY_TITLE = "相机需要授权 / Camera permission required"
_NOTIFY_BODY = (
    "请在弹窗中点击「允许」，点完我会自动继续拍照。"
    " / Click “Allow” in the dialog; the photo will continue automatically."
)


def _fail(reason: str, **extra) -> dict:
    payload = {"ok": False, "reason": reason}
    payload.update(extra)
    return payload


def _home_dir() -> str | None:
    """当前(会话)用户的家目录：HOME 优先，缺失时按 uid 查 passwd。

    不能用 /root 之类的固定值兜底：账号名不一定是 bianbu，多用户设备上
    会话属主才是正确来源（handlers 已按会话属主注入 HOME）。
    """
    home = os.environ.get("HOME")
    if home:
        return home
    try:
        return pwd.getpwuid(os.getuid()).pw_dir or None
    except (KeyError, OSError):
        return None


def _xdg_pictures_dir(home: str | None) -> str | None:
    """读 ~/.config/user-dirs.dirs 里的 XDG_PICTURES_DIR（随用户名/本地化变）。

    只接受其中的 $HOME 相对形式与绝对路径两种官方写法；文件不存在时返回 None。
    """
    if not home:
        return None
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    path = os.path.join(config_home, "user-dirs.dirs")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_PICTURES_DIR="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        value = value.replace("$HOME", home)
        if not os.path.isabs(value):
            value = os.path.join(home, value)
        return os.path.normpath(value)
    return None


def _pictures_dir() -> str:
    """snapshot 实际写入的相册目录。

    snapshot 用 g_get_user_special_dir(DIRECTORY_PICTURES)（读 user-dirs.dirs）
    再拼 Camera/，无视 XDG_PICTURES_DIR 环境变量。这里按同一规则解析，
    保证 glob 到的就是它真正写入的位置。
    """
    home = _home_dir()
    return os.path.join(_xdg_pictures_dir(home) or os.path.join(home or "/tmp", "Pictures"), "Camera")


def _photo_files() -> list[str]:
    return sorted(glob.glob(os.path.join(_pictures_dir(), "*.jpeg")))


def _snapshot_permission() -> str:
    """snapshot 的摄像头授权状态：yes / no / unset / unknown。

    只读查询，任何异常都收敛成 unknown（helper 拿不到值时不误判为未授权，
    退回旧行为）。D-Bus 不可用、PermissionStore 未起、签名变化都归 unknown。
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            PERMISSION_STORE,
            PERMISSION_STORE_PATH,
            PERMISSION_STORE,
            "Lookup",
            GLib.Variant("(ss)", (PERMISSION_TABLE, PERMISSION_ID)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        permissions, _ = reply.unpack()
        values = (permissions or {}).get(SNAPSHOT_APP_ID) or []
        if not values:
            return "unset"
        first = str(values[0]).strip().lower()
        if first in ("yes", "no"):
            return first
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _portal_dialog(Atspi, GLib, settle: float = _DIALOG_PUMP_SECONDS):
    """返回 portal 授权框节点（含授权按钮）与按钮列表；没有则 (None, []).

    只认 xdg-desktop-portal-gtk 的 alert：它才是模态、才带 Grant/Deny。
    """
    _pump(GLib, settle)
    desk = Atspi.get_desktop(0)
    for index in range(desk.get_child_count()):
        app = desk.get_child_at_index(index)
        if app is None:
            continue
        try:
            name = (app.get_name() or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if "portal" not in name:
            continue
        alert: list = []
        buttons: list = []

        def visit(node, depth: int = 0) -> None:
            if depth > 20:
                return
            try:
                role = node.get_role_name()
                if role in ("alert", "dialog") and not alert:
                    alert.append(node)
                if role == "button":
                    buttons.append(node)
            except Exception:  # noqa: BLE001
                pass
            try:
                count = node.get_child_count()
            except Exception:  # noqa: BLE001
                count = 0
            for child_index in range(count):
                try:
                    child = node.get_child_at_index(child_index)
                except Exception:  # noqa: BLE001 - 节点可能在遍历中销毁
                    child = None
                if child is not None:
                    visit(child, depth + 1)

        visit(app)
        if alert:
            return alert[0], [b for b in buttons if b is not None]
    return None, []


def _auth_state(Atspi, GLib) -> dict:
    """统一判定"是否卡在摄像头授权上"。

    判据是 PermissionStore 值 + 授权框在场：
      - 已授权（yes）：正常，不拦
      - 有记录但非 yes（no/unset）：用户必须动手，只有对话框在场才算"卡住"
      - unknown（拿不到 store）：只在对话框在场时判为等待，避免误伤
    """
    permission = _snapshot_permission()
    if permission == "yes":
        return {"awaiting": False, "permission": permission, "dialog": False, "can_grant": False}
    _, buttons = _portal_dialog(Atspi, GLib)
    dialog = bool(buttons)
    can_grant = any(_GRANT_BUTTON_RE.search(b.get_name() or "") for b in buttons)
    return {
        "awaiting": dialog,
        "permission": permission,
        "dialog": dialog,
        "can_grant": can_grant,
    }


def _notify(GLib, title: str, body: str) -> bool:
    """桌面通知提示用户去点授权框；失败不影响主流程。"""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant(
                "(susssasa{sv}i)",
                ("bibit-camera", 0, "camera-photo-symbolic", title, body, [], {}, -1),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _load_atspi():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    return Atspi, GLib


def _pump(GLib, seconds: float) -> None:
    """驱动 GLib 主循环，让 AT-SPI 事件/缓存更新被处理。

    不 pump 时读到的树可能缺子节点（实测冷缓存下只有 frame 一层），
    「相机页/相册页」判定会失真。
    """
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def _snapshot_app(Atspi):
    desk = Atspi.get_desktop(0)
    for index in range(desk.get_child_count()):
        app = desk.get_child_at_index(index)
        if app is None:
            continue
        try:
            name = (app.get_name() or "").strip().lower()
        except Exception:  # noqa: BLE001 - 无障碍对象随时可能失效
            continue
        if name == APP_NAME:
            return app
    return None


def _first_frame(app) -> object | None:
    frames: list = []

    def visit(node, depth: int = 0) -> None:
        if depth > 16:
            return
        try:
            if node.get_role_name() == "frame":
                frames.append(node)
        except Exception:  # noqa: BLE001
            pass
        try:
            count = node.get_child_count()
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(count):
            child = node.get_child_at_index(index)
            if child is not None:
                visit(child, depth + 1)

    visit(app)
    return frames[0] if frames else None


def _actions(node) -> list[str]:
    try:
        iface = node.get_action_iface()
        if iface is None:
            return []
        return [iface.get_action_name(i) for i in range(iface.get_n_actions())]
    except Exception:  # noqa: BLE001
        return []


def _markers(frame) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()

    def visit(node, depth: int = 0) -> None:
        if depth > 20:
            return
        try:
            seen.add((node.get_role_name(), node.get_name() or ""))
        except Exception:  # noqa: BLE001
            pass
        try:
            count = node.get_child_count()
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(count):
            child = node.get_child_at_index(index)
            if child is not None:
                visit(child, depth + 1)

    visit(frame)
    return seen


def _inspect(Atspi, GLib, settle: float = _PUMP_SECONDS) -> dict:
    """返回 snapshot 当前状态：是否在跑、页面、是否可直接拍照。"""
    _pump(GLib, settle)
    app = _snapshot_app(Atspi)
    if app is None:
        return {"running": False, "page": None, "can_take": False}
    frame = _first_frame(app)
    if frame is None:
        return {"running": True, "page": None, "can_take": False}
    actions = _actions(frame)
    can_take = TAKE_ACTION in actions
    marks = _markers(frame)
    if can_take:
        page = "camera"
    elif GALLERY_MARK in marks or BACK_BTN in marks:
        page = "gallery"
    elif CAMERA_MARK in marks:
        page = "camera"
    else:
        page = None
    return {
        "running": True,
        "page": page,
        "can_take": can_take,
        "actions": actions,
    }


def _frame_and_actions(Atspi, GLib):
    _pump(GLib, _PUMP_SECONDS)
    app = _snapshot_app(Atspi)
    if app is None:
        return None, None
    frame = _first_frame(app)
    if frame is None:
        return None, None
    return frame, _actions(frame)


def cmd_status(_args) -> dict:
    Atspi, GLib = _load_atspi()
    state = _inspect(Atspi, GLib)
    auth = _auth_state(Atspi, GLib)
    state["awaiting_authorization"] = auth["awaiting"]
    state["permission"] = auth["permission"]
    state["permission_dialog"] = auth["dialog"]
    state["ok"] = True
    state["pictures_dir"] = _pictures_dir()
    return state


def cmd_to_camera(_args) -> dict:
    Atspi, GLib = _load_atspi()
    state = _inspect(Atspi, GLib)
    if not state["running"]:
        return _fail("camera_not_running")
    if state["can_take"]:
        return {"ok": True, "changed": False, "page": "camera"}
    frame, actions = _frame_and_actions(Atspi, GLib)
    if frame is None or GALLERY_ACTION not in actions:
        return _fail("gallery_action_unavailable", page=state.get("page"))
    frame.do_action(actions.index(GALLERY_ACTION))
    deadline = time.monotonic() + _ACTION_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        state = _inspect(Atspi, GLib, settle=0.2)
        if state["can_take"]:
            return {"ok": True, "changed": True, "page": "camera"}
    return _fail("camera_page_not_reached", page=state.get("page"))


def cmd_take(_args) -> dict:
    Atspi, GLib = _load_atspi()
    auth = _auth_state(Atspi, GLib)
    if auth["awaiting"]:
        # 用户还没在授权框上点「允许」。此时按快门只会得到
        # "viewfinder is not in the READY state"，白等 12 秒后报错；
        # 直接返回原因，让上层去提示用户。
        return _fail(
            "awaiting_authorization",
            permission=auth["permission"],
            dialog=auth["dialog"],
            can_grant=auth["can_grant"],
            pictures_dir=_pictures_dir(),
        )
    before = set(_photo_files())
    state = _inspect(Atspi, GLib)
    if not state["running"]:
        return _fail("camera_not_running")
    if not state["can_take"]:
        return _fail("camera_page_not_active", page=state.get("page"))
    frame, actions = _frame_and_actions(Atspi, GLib)
    if frame is None or TAKE_ACTION not in actions:
        return _fail("take_action_unavailable", page=state.get("page"))
    frame.do_action(actions.index(TAKE_ACTION))
    deadline = time.monotonic() + _ACTION_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        new = set(_photo_files()) - before
        if new:
            return {"ok": True, "path": sorted(new)[-1]}
    # 没写出文件：如果 store 里本来就没授权，这次失败其实是卡在授权上
    # （授权框可能比首帧检测晚一拍才出现），报 no_photo_written 会误导用户。
    permission = _snapshot_permission()
    if permission in ("unset", "no"):
        dialog, _ = _portal_dialog(Atspi, GLib, settle=0.2)
        return _fail(
            "awaiting_authorization",
            permission=permission,
            dialog=bool(dialog),
            pictures_dir=_pictures_dir(),
        )
    return _fail("no_photo_written", pictures_dir=_pictures_dir())


def cmd_await_permission(args) -> dict:
    """发通知提示用户点「允许」，然后等待授权结果。

    返回 reason：
      granted             用户点了允许
      denied              用户点了拒绝（或 store 变 no）
      no_dialog           当前没有授权框，无需等待
      permission_timeout  等到超时用户仍未操作
    不替用户点「允许」：授权决定必须由用户自己下。
    """
    Atspi, GLib = _load_atspi()
    deadline = time.monotonic() + max(1.0, float(args.timeout))
    notified = _notify(GLib, _NOTIFY_TITLE, _NOTIFY_BODY) if args.notify else False
    first = True
    seen_dialog = False
    while True:
        permission = _snapshot_permission()
        if permission == "yes":
            return {"ok": True, "reason": "granted", "notified": notified}
        dialog, _ = _portal_dialog(
            Atspi, GLib, settle=_DIALOG_PUMP_SECONDS if first else 0.2
        )
        first = False
        if dialog:
            seen_dialog = True
        elif args.require_dialog:
            # 框没了：用户点过拒绝 → 记录成 no；否则就当没这回事
            reason = "denied" if (seen_dialog and permission == "no") else "no_dialog"
            return {
                "ok": False,
                "reason": reason,
                "permission": permission,
                "notified": notified,
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "reason": "permission_timeout",
                "permission": _snapshot_permission(),
                "notified": notified,
            }
        time.sleep(_GRANT_POLL_INTERVAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("take")
    sub.add_parser("to-camera")
    await_parser = sub.add_parser("await-permission")
    await_parser.add_argument("--timeout", type=float, default=20.0)
    await_parser.add_argument("--notify", action="store_true")
    await_parser.add_argument("--require-dialog", action="store_true")
    args = parser.parse_args(argv)
    handlers = {
        "status": cmd_status,
        "take": cmd_take,
        "to-camera": cmd_to_camera,
        "await-permission": cmd_await_permission,
    }
    try:
        result = handlers[args.command](args)
    except Exception as exc:  # noqa: BLE001 - 无 gi / AT-SPI 不可用时给出可读原因
        result = _fail("helper_error", detail=f"{type(exc).__name__}: {exc}")
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
