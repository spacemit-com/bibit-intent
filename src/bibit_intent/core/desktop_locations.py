"""Known file-manager locations and directory aliases.

``DESKTOP_LOCATIONS`` holds gvfs virtual locations (trash/computer) that
``launch_app`` opens via ``gio open`` (falling back to ``xdg-open``), so they
dispatch to whatever the user's default file manager is. ``resolve_directory``
maps everyday XDG directory aliases (desktop/documents/downloads/...) to real
filesystem paths so ``open_folder`` can open them without any filesystem-path
inference by a model.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

DESKTOP_LOCATIONS = {
    "trash": "trash:///",
    "垃圾箱": "trash:///",
    "回收站": "trash:///",
    "垃圾桶": "trash:///",
    "computer": "computer:///",
    "my computer": "computer:///",
    "我的电脑": "computer:///",
    "此电脑": "computer:///",
    "计算机": "computer:///",
}

# 别名 → XDG 键；HOME/ROOT 是哨兵值，不走 xdg-user-dir。
_XDG_ALIASES: dict[str, str] = {
    "桌面": "DESKTOP",
    "desktop": "DESKTOP",
    "文档": "DOCUMENTS",
    "document": "DOCUMENTS",
    "documents": "DOCUMENTS",
    "下载": "DOWNLOAD",
    "download": "DOWNLOAD",
    "downloads": "DOWNLOAD",
    "主目录": "HOME",
    "主文件夹": "HOME",
    "家目录": "HOME",
    "home": "HOME",
    "根目录": "ROOT",
    "根文件系统": "ROOT",
    "root": "ROOT",
    "/": "ROOT",
}


def _xdg_user_dir(key: str) -> str | None:
    """查询 XDG 用户目录（本地化目录名），失败返回 None。"""
    try:
        out = subprocess.run(
            ["xdg-user-dir", key],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if out and Path(out).is_absolute():
        return out
    return None


def resolve_directory(name: str) -> str | None:
    """把目录别名解析成实际路径；无法识别返回 None。

    支持 XDG 标准位置（桌面/文档/下载）与主目录/根目录，中英文均可。
    媒体目录（图片/音乐/视频）刻意不收：裸「打开音乐/视频」与播放媒体歧义，
    留给云端澄清。显式路径不在这里处理（由调用方 expanduser + is_absolute 校验）。
    """
    alias = _XDG_ALIASES.get(str(name or "").strip().casefold())
    if alias == "HOME":
        return str(Path.home())
    if alias == "ROOT":
        return "/"
    if alias:
        return _xdg_user_dir(alias) or str(Path.home())
    return None


# 桌面占位快捷方式的通用显示名（与用户名无关的部分）。用户主目录的显示名
# 是当前用户名（如 user-home.desktop 的 Name=<username>），不能硬编码，由
# resolve_desktop_shortcut 在运行时用 Path.home().name 动态补齐。
_HOME_SHORTCUT_NAMES = frozenset({
    "user-home", "user home", "home",
    "主目录", "主文件夹", "家目录",
})


def resolve_desktop_shortcut(name: str) -> str | None:
    """把桌面占位快捷方式名解析成打开目标（路径或 gvfs URI），无法识别返回 None。

    覆盖「打开桌面的 bianbu / 计算机 / 回收站 文件夹」这类把桌面上的
    .desktop 快捷方式当目录打开的场景；返回的可能是真实路径（HOME 哨兵）
    或 gvfs URI（trash:/// / computer:///），由调用方区分处理。用户名是
    动态的（不同用户主目录名不同），因此用 Path.home().name 匹配而不是硬编码。
    """
    key = str(name or "").strip().casefold()
    home = Path.home()
    if key in _HOME_SHORTCUT_NAMES or key == home.name.casefold():
        return str(Path.home())
    return DESKTOP_LOCATIONS.get(key)
