import re


_APT_INSTALL_SPEC_RE = re.compile(
    r"(?P<name>[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?)"
    r"(?:=(?P<version>[0-9][A-Za-z0-9.+:~-]*))?$"
)


def is_exact_package(target: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?", target) is not None


def split_exact_packages(targets: str) -> list[str] | None:
    """把一次安装/卸载请求里的多个包名拆成精确包名列表。

    支持空格、逗号、中文顿号/分号等分隔，以及英文 ``and``。任一项不是完整
    包名（含通配符等）时返回 None，由调用方拒绝执行，避免只装一半。
    重复项保留第一次出现；末尾 + / - 的歧义由调用方核实完整包名及版本。
    """
    if not isinstance(targets, str) or not targets.strip():
        return None
    normalized = re.sub(r"(?i)\s+and\s+", " ", targets.strip())
    parts = [part for part in re.split(r"[\s,，、;；]+", normalized) if part]
    if not parts or not all(is_exact_package(part) for part in parts):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part not in seen:
            seen.add(part)
            result.append(part)
    return result


def split_exact_install_specs(targets: str) -> list[tuple[str, str | None]] | None:
    """把安装请求拆成 (包名, 可选目标版本)，供 apt 显式版本安装/降级使用。

    与 split_exact_packages 相同，支持空格、逗号、中文顿号/分号和英文 and。
    版本仅接受 ``name=version`` 的完整 Debian 版本形式；重复包名保留第一次。
    """
    if not isinstance(targets, str) or not targets.strip():
        return None
    normalized = re.sub(r"(?i)\s+and\s+", " ", targets.strip())
    parts = [part for part in re.split(r"[\s,，、;；]+", normalized) if part]
    if not parts:
        return None
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for part in parts:
        matched = _APT_INSTALL_SPEC_RE.fullmatch(part)
        if matched is None:
            return None
        name = matched.group("name")
        version = matched.group("version")
        if name not in seen:
            seen.add(name)
            result.append((name, version))
    return result


def has_apt_action_suffix(target: str) -> bool:
    return target.endswith(("+", "-")) or target.partition(":")[0].endswith(("+", "-"))


def has_package_pattern(target: str) -> bool:
    return any(character in target for character in "*?^$[](){}|\\")


def package_pattern_request(text: str) -> tuple[str, str] | None:
    if not has_package_pattern(text):
        return None
    matched = re.match(
        r"^(?:(?:请|帮我|请帮我)\s*)?(?:sudo\s+)?(?:apt(?:-get)?\s+)?"
        r"(?P<action>安装|卸载|移除|删除|install\b|remove\b|uninstall\b)\s*(?P<target>.+)$",
        text.strip(), re.IGNORECASE,
    )
    if not matched:
        suffix = re.fullmatch(r"(?:请|帮我|请帮我)?\s*把?\s*(?P<target>.+?)\s*(?:卸载|卸掉|移除|删掉)(?:了)?", text.strip())
        return ("pkg_remove", suffix.group("target").strip()) if suffix else None
    action = matched.group("action").lower()
    tool = "pkg_install" if action in {"安装", "install"} else "pkg_remove"
    return tool, matched.group("target").strip()
