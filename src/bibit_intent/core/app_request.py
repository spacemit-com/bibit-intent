"""Normalize explicit polite application requests without rewriting other questions."""

import re


_POLITE_APP_REQUEST = re.compile(
    r"^(?:你能|您能|能不能|能否|可以|能)(?:帮我|替我)?\s*"
    r"(?P<command>(?:打开|启动|开启|关闭|退出)\s*.+?)"
    r"(?:吗|么|好吗|可以吗)?[？?！!。]*$"
)

_MISSING_APP_TARGET = re.compile(
    r"^(?:请帮我|帮我|请)?(?:打开|启动|运行|开启|关闭|退出|停止|结束)\s*"
    r"(?:一下)?\s*(?:(?:一个|那个|这个|某个)?(?:应用|软件|程序))?"
    r"\s*(?:吗|么)?[？?！!。]*$"
    r"|^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:open|launch|start|close|quit|exit|stop)"
    r"(?:\s+(?:an?|the|some))?(?:\s+(?:app|application|program|software))?[?!.]*$",
    re.I,
)


def normalize_app_request(text: str) -> str:
    match = _POLITE_APP_REQUEST.fullmatch(text.strip())
    return match["command"] if match else text


def missing_app_target_request(text: str) -> bool:
    return _MISSING_APP_TARGET.fullmatch(normalize_app_request(text).strip()) is not None
