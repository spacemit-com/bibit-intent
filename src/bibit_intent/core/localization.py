"""Lightweight localization for user-visible execution replies."""
from __future__ import annotations
import re

def localize_reply(message: str, language: str) -> str:
    if language != "en" or not message:
        return message
    exact = {
        "已静音": "Muted", "已取消静音": "Unmuted", "当前没有打开的窗口": "No windows are currently open",
        "当前没有打开的窗口，无法执行窗口操作": "No windows are currently open; the window operation cannot be performed",
        "已锁屏": "Screen locked", "正在重启设备...": "Restarting the device...",
    }
    if message in exact:
        return exact[message]
    patterns = [
        (r"^音量已设为\s*(.+)$", r"Volume set to \1"),
        (r"^亮度已设为\s*(.+)$", r"Brightness set to \1"),
        (r"^已启动应用:\s*(.+)$", r"Launched application: \1"),
        (r"^已关闭应用:\s*(.+)$", r"Closed application: \1"),
        (r"^未找到运行中的应用:\s*(.+)$", r"Application is not running: \1"),
        (r"^命令不存在：(.+)$", r"Command not found: \1"),
        (r"^未知工具：(.+)$", r"Unknown tool: \1"),
        (r"^缺少应用名称$", "Application name is required"),
        (r"^参数错误：(.+)$", r"Invalid parameter: \1"),
        (r"^命令失败\(码(.+)\)$", r"Command failed (code \1)"),
        (r"^命令超时：(.+)$", r"Command timed out: \1"),
        (r"^无权限启动：(.+)$", r"Permission denied when launching: \1"),
        (r"^启动失败：(.+)$", r"Launch failed: \1"),
        (r"^应用未找到: (.+) \(请检查应用是否已安装\)$", r"Application not found: \1 (check whether it is installed)"),
        (r"^无效的应用名称: (.+)$", r"Invalid application name: \1"),
        (r"^无法获取系统信息\(item=(.+)\)$", r"Unable to retrieve system information (item=\1)"),
        (r"^还差一个参数：(.+)$", r"Missing parameter: \1"),
    (
        r"^已登记应用 (\d+) 个（(\d+) 个前置齐备，(\d+) 个已通过 riscv64 验收）：$",
        r"\1 apps registered (\2 ready, \3 riscv64-verified):",
    ),
    (r"^没有匹配 category=(.+) 的应用$", r"No app matches category=\1"),
    (r"^没有登记任何应用$", "No app is registered"),
    (r"^应用注册表不可用：(.+)$", r"App registry is unavailable: \1"),
    ]
    for pattern, replacement in patterns:
        if re.match(pattern, message):
            return re.sub(pattern, replacement, message)
    return message
