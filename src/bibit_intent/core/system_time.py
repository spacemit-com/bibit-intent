from __future__ import annotations

import re
from datetime import datetime, date, time


_STAMP = r"(?P<datetime>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?|\d{4}年\d{1,2}月\d{1,2}日\s*(?:[01]?\d|2[0-3])(?:时|点)\d{1,2}分(?:\d{1,2}秒)?)"
_REQUESTS = (
    re.compile(r"^(?:请|请帮我|帮我)?(?:把|将)?(?:系统|电脑|设备)(?:日期和时间|日期时间|时间|时钟)(?:设置|设定|调整|修改|改|调|设)(?:到|为|成)\s*" + _STAMP + r"[。！!]?\s*$"),
    re.compile(r"^(?:请|请帮我|帮我)?(?:设置|设定|调整|修改)(?:系统|电脑|设备)(?:日期和时间|日期时间|时间|时钟)(?:到|为|成)\s*" + _STAMP + r"[。！!]?\s*$"),
    re.compile(r"^(?:please\s+)?(?:set|change|adjust)\s+(?:the\s+)?(?:system|computer|device)\s+(?:date\s+and\s+time|time|clock)\s+to\s+" + _STAMP + r"[.!]?\s*$", re.I),
)
_PARTIAL_VALUE = r"(?P<value>\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日|\d{2}:\d{2}(?::\d{2})?|\d{1,2}(?:时|点)\d{1,2}分(?:\d{1,2}秒)?)"
_PARTIAL_REQUESTS = (
    re.compile(r"(?:请|请帮我|帮我)?(?:把|将)?(?:系统|电脑|设备)?(?P<kind>日期|时间|时钟)(?:设置|设定|调整|修改|改|调|设)(?:到|为|成)\s*" + _PARTIAL_VALUE + r"[。！!]?"),
    re.compile(r"(?:请|请帮我|帮我)?(?:设置|设定|调整|修改)(?:系统|电脑|设备)?(?P<kind>日期|时间|时钟)(?:到|为|成)\s*" + _PARTIAL_VALUE + r"[。！!]?"),
    re.compile(r"(?:please\s+)?(?:set|change|adjust)\s+(?:the\s+)?(?:system|computer|device)\s+(?P<kind>date|time|clock)\s+to\s+" + _PARTIAL_VALUE + r"[.!]?", re.I),
)
_TIME_REQUEST = re.compile(
    r"(?:系统|电脑|设备)(?:日期和时间|日期时间|时间|时钟|日期)|"
    r"(?:设置|设定|调整|修改|改|调)(?:一下)?(?:时间|时钟|日期)|"
    r"(?:把|将)?(?:时间|时钟|日期)(?:设置|设定|调整|修改|改|调)|"
    r"\b(?:system|computer|device)\s+(?:time|clock|date)\b|"
    r"\b(?:set|change|adjust)\b.{0,24}\b(?:time|clock|date)\b", re.I,
)
_TIME_QUERY = re.compile(
    r"(?:请|请帮我|帮我)?(?:"
    r"(?:现在|当前)(?:是)?几点(?:钟)?(?:了)?|今天(?:是)?(?:几月几[日号]|几[日号]|什么日期)|"
    r"(?:查看|查询|获取|显示|告诉我)(?:一下)?(?:当前|现在)?(?:的)?(?:系统|电脑|设备)?(?:日期和时间|日期时间|时间|日期|时区)|"
    r"(?:当前|现在)?(?:系统|电脑|设备)(?:日期和时间|日期时间|时间|日期|时区)(?:是什么|是多少|是几点)?"
    r")[？?。！!]?|"
    r"(?:please\s+)?(?:"
    r"what\s+time\s+is\s+it(?:\s+now)?|"
    r"what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?(?:system\s+)?(?:time|date|timezone)(?:\s+(?:now|today))?|"
    r"(?:show|get|check)\s+(?:the\s+)?(?:current\s+)?(?:system\s+)?(?:date\s+and\s+time|time|date|timezone)"
    r")[?.!]?", re.I,
)


def is_system_time_query(text: str) -> bool:
    return bool(_TIME_QUERY.fullmatch(text.strip()))


def normalize_system_datetime(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
            return time.fromisoformat(value).strftime("%H:%M:%S")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", value):
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return None


def resolve_system_datetime(value: str, current: datetime) -> str:
    if len(value) == 10:
        return f"{value} {current:%H:%M:%S}"
    if len(value) == 8:
        return f"{current:%Y-%m-%d} {value}"
    return value


def parse_system_time_request(text: str) -> str | None:
    for pattern in _REQUESTS:
        match = pattern.fullmatch(text.strip())
        if match:
            value = match.group("datetime")
            if "年" in value:
                parts = [int(part) for part in re.findall(r"\d+", value)]
                try:
                    return datetime(*parts).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
            return normalize_system_datetime(value)
    for pattern in _PARTIAL_REQUESTS:
        match = pattern.fullmatch(text.strip())
        if match is None:
            continue
        value = match.group("value")
        if "年" in value:
            parts = [int(part) for part in re.findall(r"\d+", value)]
            value = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
        elif "分" in value:
            parts = [int(part) for part in re.findall(r"\d+", value)]
            value = f"{parts[0]:02d}:{parts[1]:02d}:{parts[2] if len(parts) == 3 else 0:02d}"
        target = normalize_system_datetime(value)
        if target is not None and match.group("kind").lower() in ("日期", "date") and len(target) != 10:
            return None
        return target
    return None


def is_system_time_request(text: str) -> bool:
    return bool(_TIME_REQUEST.search(text))
