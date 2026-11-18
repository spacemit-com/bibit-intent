"""用桌面 portal 传递已授权的文件描述符，避免桌面启动前路径被替换。"""
import subprocess

from .execution.cancellation import raise_if_cancelled
from .file_results import FileResultError, authorized_open, parse_reference

# 系统 Python 提供 GI；保持脚本固定，用户路径/动作不拼入代码或 shell。
_PORTAL = r'''
import sys
from gi.repository import Gio, GLib
fd, method = int(sys.argv[1]), sys.argv[2]
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
loop = GLib.MainLoop()
responses = {}
handle = None
result = 3

def response(connection, sender, path, interface, signal, parameters, data):
    global result
    responses[path] = int(parameters.unpack()[0])
    if path == handle:
        result = responses[path]
        loop.quit()

subscription = bus.signal_subscribe('org.freedesktop.portal.Desktop', 'org.freedesktop.portal.Request',
    'Response', None, None, Gio.DBusSignalFlags.NONE, response, None)
fdlist = Gio.UnixFDList.new()
index = fdlist.append(fd)
reply, unused = bus.call_with_unix_fd_list_sync('org.freedesktop.portal.Desktop',
    '/org/freedesktop/portal/desktop', 'org.freedesktop.portal.OpenURI', method,
    GLib.Variant('(sha{sv})', ('', index, {'handle_token': GLib.Variant('s', 'bibit_' + str(__import__('os').getpid()))})),
    GLib.VariantType.new('(o)'), Gio.DBusCallFlags.NONE, 10000, fdlist, None)
handle = reply.unpack()[0]
if handle in responses:
    result = responses[handle]
else:
    GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
    loop.run()
bus.signal_unsubscribe(subscription)
sys.exit(result)
'''


def file_action(payload: dict) -> dict:
    from .execution.system_tools import _find_desktop_session_env, _gui_child_env

    ref = parse_reference(payload)
    action = payload.get("action")
    if action not in {"open", "reveal"} or set(payload) != {"action", "device_id", "path", "version"}:
        raise FileResultError("只支持打开文件或所在目录。")
    with authorized_open(ref["path"], device=ref["device_id"], version=ref["version"]) as (fd, _, _):
        raise_if_cancelled()
        try:
            process = subprocess.run(
                ["/usr/bin/python3", "-c", _PORTAL, str(fd), "OpenFile" if action == "open" else "OpenDirectory"],
                pass_fds=(fd,), env=_gui_child_env(_find_desktop_session_env()),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=32,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FileResultError("桌面打开请求失败或超时，请检查桌面会话和 portal 服务。") from exc
    if process.returncode:
        raise FileResultError("桌面未接受打开请求，可能已取消或 portal 服务不可用。")
    return {"ok": True, "message": "已请求打开文件。" if action == "open" else "已请求打开所在目录。"}
