from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

from bibit_intent.core.appcli_execution import is_sandboxed
from . import system_tools
from .system_tools import ExecResult, _reply


def _desktop_processes() -> list[tuple[int, int]]:
    candidates = []
    for directory in Path("/proc").glob("[0-9]*"):
        try:
            owner = directory.stat().st_uid
            if os.getuid() not in (0, owner):
                continue
            command = (directory / "cmdline").read_bytes().decode().split("\0")
            if Path(command[0]).name == "pcmanfm-qt" and "--desktop" in command:
                candidates.append((int(directory.name), owner))
        except (OSError, UnicodeError, ValueError):
            continue
    return candidates


def set_wallpaper(path: str = "", language: str | None = None, **_) -> ExecResult:
    if is_sandboxed():
        return ExecResult(False, _reply("设置壁纸必须在宿主桌面执行，请转交宿主通道。",
                                       "Setting wallpaper requires the host desktop channel.", language),
                          escalate="needs_host_channel")
    if not isinstance(path, str) or not path.strip():
        return ExecResult(False, _reply("请提供要设为壁纸的图片完整路径。",
                                       "Please provide the full path to the wallpaper image.", language), need_param="path")
    try:
        candidates = _desktop_processes()
        if len(candidates) != 1:
            raise ValueError("Expected one PCManFM-Qt desktop session; found " + str(len(candidates)))
        process_id, owner = candidates[0]
        helper = Path(__file__).with_name("wallpaper_helper.py")
        command = ["/usr/bin/python3", "-I", str(helper), str(process_id), path]
        if os.getuid() != owner:
            command = ["runuser", "-u", pwd.getpwuid(owner).pw_name, "--", *command]
        result = system_tools._run(command, timeout=20)
        if not result.ok:
            raise ValueError(result.detail or result.message)
        payload = json.loads(result.detail)
        if not isinstance(payload, dict):
            raise ValueError("Invalid wallpaper helper response")
        if not payload.get("ok"):
            raise ValueError(payload.get("error", "Wallpaper helper failed"))
        return ExecResult(True, _reply("已将指定图片设为宿主桌面壁纸。",
                                       "The selected image is now the host desktop wallpaper.", language),
                          json.dumps(payload, ensure_ascii=False), detail_format="text")
    except (OSError, KeyError, ValueError, TypeError) as error:
        return ExecResult(False, _reply("设置壁纸失败，未确认桌面已更新。",
                                       "Could not confirm the wallpaper update.", language), str(error), detail_format="text")


HANDLERS = {"set_wallpaper": set_wallpaper}
system_tools.HANDLERS.update(HANDLERS)
