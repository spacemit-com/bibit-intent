from __future__ import annotations

import re


def parse_locale_request(text: str) -> tuple[str, dict] | None:
    text = text.strip().rstrip("？?。！!")
    if re.search(r"个人|用户|\buser\b|\bpersonal\b", text, re.I):
        return None
    if re.fullmatch(r"(?:查看|查询|列出)(?:可用|支持|可选)(?:的)?(?:语言|区域|地区|语言和区域)(?:列表)?|(?:list|show)\s+(?:available|supported)\s+(?:languages|regions|locales)", text, re.I):
        return "list_locale_options", {}
    current = re.fullmatch(r"(?:当前|现在)(?:的)?(?:系统|电脑|设备)?(?:语言|区域|地区|语言和区域)(?:是)?(?:什么|哪个)", text)
    if current:
        return "get_locale_settings", {"scope": "system"}
    query = re.fullmatch(
        r"(?:请|请帮我|帮我)?(?:查看|查询|获取|显示)(?:一下)?(?:当前)?(?P<scope>系统默认|系统|电脑|设备|个人|用户)?(?:的)?(?:语言和区域|语言与区域|语言|区域|地区)(?:设置)?", text)
    if query:
        return "get_locale_settings", {"scope": "system"}
    query = re.fullmatch(r"(?:what\s+is|show|get|check)\s+(?:the\s+)?(?:current\s+)?(?P<scope>system\s+default\s+)?(?:system\s+)?(?:language|region|locale|language\s+and\s+region)(?:\s+settings)?", text, re.I)
    if query:
        return "get_locale_settings", {"scope": "system"}
    change = re.fullmatch(
        r"(?:请|请帮我|帮我)?(?:(?:把|将)?(?P<scope>系统默认|系统|电脑|设备|个人|用户)?(?:的)?(?P<kind>语言|区域|地区)(?:设置|修改|切换|更改|改|设)(?:为|成|到)|"
        r"(?:设置|修改|切换|更改)(?P<scope2>系统默认|系统|电脑|设备|个人|用户)?(?:的)?(?P<kind2>语言|区域|地区)(?:为|成|到))(?P<value>[a-zA-Z_\.\-0-9]+|简体中文|中文|英文|英语|中国大陆|中国|美国|英国|日本|德国)", text)
    if change:
        kind = change.group("kind") or change.group("kind2")
        return ("set_system_language" if kind == "语言" else "set_system_region"), {
            "locale": change.group("value"), "scope": "system"}
    change = re.fullmatch(r"(?:please\s+)?(?:set|change|switch)\s+(?:the\s+)?(?P<scope>system\s+default\s+)?(?:system\s+)?(?P<kind>language|region)\s+to\s+(?P<value>[a-zA-Z_\.\-0-9]+|simplified chinese|united states|united kingdom)", text, re.I)
    if change:
        return ("set_system_language" if change.group("kind").lower() == "language" else "set_system_region"), {
            "locale": change.group("value"), "scope": "system"}
    return None


def is_locale_request(text: str) -> bool:
    return bool(re.search(r"(?:系统|电脑|设备|个人|用户|默认)(?:的)?(?:语言|区域|地区)|"
                          r"(?:设置|修改|切换|更改)(?:一下)?(?:语言|区域|地区)|"
                          r"(?:语言|区域|地区)(?:设置|修改|切换|更改|改成|改为)|"
                          r"\b(?:system\s+(?:default\s+)?(?:language|region|locale)|(?:set|change|switch)\s+(?:the\s+)?(?:language|region))\b", text, re.I))
