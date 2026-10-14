"""进模型前的一词多义确定性守卫。

集中处理规则触发词在不同语境下的语义冲突。规则匹配前先判断是否属于
“本地单工具无法安全表达”的歧义控制，命中则建议升级云端澄清。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PolysemyGuard:
    name: str
    reason: str
    pattern: re.Pattern[str]


_POLYSEMY_GUARDS: tuple[PolysemyGuard, ...] = (
    PolysemyGuard(
        "bare_service_action",
        "ambiguous_control",
        re.compile(r"^(?:重启|重新启动|启动|停止|关闭|停掉|结束)\s*服务$"),
    ),
    PolysemyGuard(
        "bare_process_action",
        "ambiguous_control",
        re.compile(r"^(?:结束|终止|杀掉|关闭|停止|停掉)\s*进程$"),
    ),
    PolysemyGuard(
        "ambiguous_open_object",
        "ambiguous_control",
        # wifi/WiFi 已实现规则直通（wifi_on），不再算歧义；裸"无线"仍可能是
        # 无线设置项/无线设备，保留升级。
        re.compile(
            r"^(?:帮我)?(?:打开|启动|开启|运行)\s*"
            r"(?:进程|文件|服务|网络|无线)$"
        ),
    ),
    PolysemyGuard(
        "ambiguous_restart_object",
        "ambiguous_restart",
        re.compile(
            r"^(?:重启|重新启动)\s*"
            r"(?!设备|系统|机器|电脑|服务器)"
            r"(?P<obj>[A-Za-z0-9_\-一-龥]+)(?<!服务)$"
        ),
    ),
    PolysemyGuard("english_open_object", "ambiguous_control", re.compile(r"^(?:open|launch|start)\s+(?:the\s+)?(?:file|process|service)$", re.I)),
    PolysemyGuard("english_service_action", "ambiguous_control", re.compile(r"^(?:restart|stop|start|kill|close)\s+(?:the\s+)?(?:service|process)$", re.I)),
)


def escalate_reason(text: str) -> str | None:
    """返回首个命中的升级 reason，未命中返回 None。"""
    text = (text or "").strip()
    for guard in _POLYSEMY_GUARDS:
        if guard.pattern.search(text):
            return guard.reason
    return None
