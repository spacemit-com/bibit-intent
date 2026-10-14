"""Closed command grammars for operations that must not accept open objects."""

import re


_PREFIX = r"(?:(?:请|麻烦)(?:你)?\s*)?(?:帮我\s*)?"
_SUFFIX = r"(?:一下|吧)?[。！!]?"
_ACCOUNT = r"[a-zA-Z_][a-zA-Z0-9_-]{0,29}"
_BODIES = {
    "pkg_update": (
        r"(?:更新|升级)(?:一下)?\s*(?:系统|系统软件|软件|软件包|所有软件包|所有软件|已安装的软件)(?:列表|到最新)?",
        r"(?:系统|系统软件)(?:更新|升级)(?:一下)?",
        r"把(?:系统|软件|软件包)(?:都)?(?:更新|升级)(?:一下|了)?",
        r"(?:update|upgrade)\s+(?:the\s+)?(?:package\s+lists?|system|all\s+packages)",
        r"apt(?:-get)?\s+(?:update|upgrade)",
    ),
    "pkg_clean": (
        r"(?:清理|清除|清掉)(?:一下)?\s*(?:所有)?(?:软件包|软件|安装包|包|apt)\s*(?:缓存|旧版本缓存)",
        r"(?:clean|clear)\s+(?:the\s+)?(?:apt|package)\s+cache",
        r"apt(?:-get)?\s+clean",
    ),
    "proc_zombie": (
        r"(?:清理|清除|杀掉)(?:一下)?\s*(?:僵尸进程|zombie\s+processes)",
        r"(?:clean|clear|kill)\s+(?:the\s+)?zombie\s+processes",
    ),
    "proc_kill": (
        r"(?:杀掉|结束|终止|杀死)\s*(?:进程\s*[1-9][0-9]*|[1-9][0-9]*(?:号)?进程)",
        r"kill\s+[1-9][0-9]*",
    ),
    "disk_format": (
        r"(?:格式化|mkfs)\s*(?:分区\s*)?/dev/[a-zA-Z0-9]+(?:分区)?",
    ),
}
for _tool, _chinese, _english in (
    ("user_add", "新建|创建|增加|添加", "add|create"),
    ("user_del", "删除|移除|删掉", "delete|remove"),
    ("user_lock", "锁定|禁用", "lock|disable"),
    ("user_unlock", "解锁|启用", "unlock|enable"),
):
    _BODIES[_tool] = (
        rf"(?:{_chinese})\s*(?:一个)?(?:用户|账号)\s*{_ACCOUNT}",
        rf"(?:{_english})\s+(?:user|account)\s+{_ACCOUNT}",
    )

COMMAND_CONTRACTS = {
    tool: re.compile(
        rf"(?:{_PREFIX}(?:{'|'.join(bodies)}){_SUFFIX}|"
        rf"please\s+(?:{'|'.join(bodies)})[.!]?)", re.I
    )
    for tool, bodies in _BODIES.items()
}

_OPEN_ADMIN_REQUEST = re.compile(
    rf"^{_PREFIX}(?:更新|升级|清理|清除|清掉|格式化|"
    r"(?:新建|创建|增加|添加|删除|移除|删掉|锁定|禁用|解锁|启用)\s*(?:一个)?(?:用户|账号))"
    r"|^(?:please\s+)?(?:(?:update|upgrade|mkfs|kill|terminate|format)\b|"
    r"(?:更新|升级|清理|清除|清掉|格式化|杀掉|结束|终止|杀死)|"
    r"(?:delete|remove|add|create|lock|unlock|disable|enable)\s+(?:user|account)\b)",
    re.I,
)

_DOMAIN_ANCHORS = {
    "pkg_update": ("系统", "软件", "软件包", "package", "packages", "apt"),
    "pkg_clean": ("缓存", "软件包", "安装包", "apt", "cache"),
    "proc_zombie": ("僵尸进程", "zombie"),
    "proc_kill": ("进程", "process", "kill"),
    "disk_format": ("/dev/", "分区", "partition", "mkfs"),
    "user_add": ("用户", "账号", "user", "account"),
    "user_del": ("用户", "账号", "user", "account"),
    "user_lock": ("用户", "账号", "user", "account"),
    "user_unlock": ("用户", "账号", "user", "account"),
}
_UNVERIFIED_SUFFIX = re.compile(
    r"(?:的(?:说明文档|聊天记录|操作记录|原理|进度|结果)|并|然后|之后|以后|的安排|的课程|[：:])",
    re.I,
)

_WIFI_CONNECT_ACTION = re.compile(
    r"(?:连接|接入|加入|连上|连一下|connect|join|attach)", re.I
)
_WIFI_CONNECT_OBJECT = re.compile(
    r"(?:wifi|wi-fi|无线(?:网络|网)?|网络|network)", re.I
)


def open_admin_request(text: str) -> bool:
    text = text.strip()
    if not _OPEN_ADMIN_REQUEST.search(text):
        return False
    if any(contract.fullmatch(text) for contract in COMMAND_CONTRACTS.values()):
        return False
    if _UNVERIFIED_SUFFIX.search(text):
        return True
    return not any(anchor.lower() in text.lower() for anchor in _DOMAIN_ANCHORS.get("pkg_update", ()))


def domain_anchor_allows(tool_name: str, text: str) -> bool:
    text = text.strip()
    if _UNVERIFIED_SUFFIX.search(text):
        return False
    if tool_name == "pkg_clean" and not any(x in text.lower() for x in ("软件", "软件包", "安装包", "apt", "package")):
        return False
    if tool_name == "proc_zombie" and text.startswith("有"):
        return False
    if not re.match(r"^(?:请|麻烦|帮我|更新|升级|清理|清除|清掉|删除|移除|删掉|杀掉|结束|终止|杀死|格式化|创建|新建|添加|锁定|禁用|解锁|启用|update|upgrade|delete|remove|kill|format|create|add|lock|unlock|disable|enable)", text, re.I):
        return False
    return any(anchor.lower() in text.lower() for anchor in _DOMAIN_ANCHORS.get(tool_name, ()))


def command_contract_allows(tool_name: str, text: str) -> bool:
    contract = COMMAND_CONTRACTS.get(tool_name)
    return contract is None or contract.fullmatch(text.strip()) is not None


def side_effect_contract_allows(tool_name: str, text: str) -> bool:
    """Require explicit action evidence before executing sensitive tools."""
    if tool_name != "wifi_connect":
        return True
    value = text.strip()
    return bool(
        _WIFI_CONNECT_ACTION.search(value)
        and _WIFI_CONNECT_OBJECT.search(value)
    )
