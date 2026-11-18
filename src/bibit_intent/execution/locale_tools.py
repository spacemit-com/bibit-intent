from __future__ import annotations

import json
import re
import shlex

from . import system_tools
from .system_tools import ExecResult, _reply, _run


LANGUAGE_KEYS = ("LANG", "LC_MESSAGES", "LC_CTYPE")
REGION_KEYS = ("LC_NUMERIC", "LC_MONETARY", "LC_MEASUREMENT", "LC_COLLATE",
               "LC_PAPER", "LC_ADDRESS", "LC_TELEPHONE", "LC_NAME", "LC_TIME")
LANGUAGES = {"zh_CN": "简体中文 / Simplified Chinese", "en_US": "英文 / English"}
LANGUAGE_ALIASES = {"中文": "zh_CN", "简体中文": "zh_CN", "chinese": "zh_CN",
                    "simplified chinese": "zh_CN", "英文": "en_US", "英语": "en_US", "english": "en_US"}
REGION_ALIASES = {"中国": "CN", "中国大陆": "CN", "china": "CN", "美国": "US",
                  "united states": "US", "英国": "GB", "united kingdom": "GB",
                  "日本": "JP", "japan": "JP", "德国": "DE", "germany": "DE"}
LANGUAGE_NAMES = {"zh_CN": ("简体中文", "Simplified Chinese"),
                  "zh_SG": ("简体中文", "Simplified Chinese"),
                  "zh_TW": ("繁体中文", "Traditional Chinese"),
                  "zh_HK": ("繁体中文", "Traditional Chinese")}
REGION_NAMES = {
    "CN": ("中国大陆", "Mainland China"), "HK": ("中国香港", "Hong Kong"),
    "TW": ("中国台湾", "Taiwan"), "SG": ("新加坡", "Singapore"),
    "US": ("美国", "United States"), "GB": ("英国", "United Kingdom"),
    "CA": ("加拿大", "Canada"), "AU": ("澳大利亚", "Australia"),
    "NZ": ("新西兰", "New Zealand"), "IE": ("爱尔兰", "Ireland"),
    "DK": ("丹麦", "Denmark"), "IN": ("印度", "India"),
    "IL": ("以色列", "Israel"), "PH": ("菲律宾", "Philippines"),
    "ZA": ("南非", "South Africa"), "BW": ("博茨瓦纳", "Botswana"),
    "NG": ("尼日利亚", "Nigeria"), "ZM": ("赞比亚", "Zambia"),
    "ZW": ("津巴布韦", "Zimbabwe"), "AG": ("安提瓜和巴布达", "Antigua and Barbuda"),
    "JP": ("日本", "Japan"), "DE": ("德国", "Germany"), "FR": ("法国", "France"),
}


def _locale_label(value: str, kind: str, language: str | None = None) -> str:
    base = _base(value)
    if not base:
        return _reply("未配置", "Not configured", language)
    if base in ("C", "POSIX"):
        return _reply("标准默认格式", "Standard default locale", language)
    if kind == "region":
        names = REGION_NAMES.get(base.rpartition("_")[2])
    else:
        names = LANGUAGE_NAMES.get(base)
        if names is None and base.split("_", 1)[0] == "en":
            names = ("英文", "English")
    return _reply(*names, language) if names else value


def _base(value: str) -> str:
    return value.split(".", 1)[0].split("@", 1)[0]


def _installed() -> dict[str, str]:
    result = _run(["locale", "-a"])
    if not result.ok:
        raise ValueError(result.detail or result.message)
    installed = {}
    for value in result.detail.splitlines():
        if re.fullmatch(r"[a-z]{2,3}_[A-Z]{2}\.(?:utf8|UTF-8)", value):
            installed[_base(value)] = value
    return installed


def _system_values() -> dict[str, str]:
    result = _run(["busctl", "--system", "get-property", "org.freedesktop.locale1",
                   "/org/freedesktop/locale1", "org.freedesktop.locale1", "Locale"])
    if not result.ok:
        raise ValueError(result.detail or result.message)
    parts = shlex.split(result.detail)
    if len(parts) < 2 or parts[0] != "as" or not parts[1].isdigit() or int(parts[1]) != len(parts) - 2:
        raise ValueError("Invalid locale1 Locale response")
    return dict(entry.split("=", 1) for entry in parts[2:] if "=" in entry)


def _read(scope: str):
    if scope != "system":
        raise ValueError("Only global system scope is supported; sandbox/user settings are not used")
    values = _system_values()
    language = values.get("LANG", "")
    region = values.get("LC_NUMERIC") or language
    return values, language, region


def _failure(error: Exception, language: str | None) -> ExecResult:
    return ExecResult(False, _reply("无法读取或修改语言与区域设置。", "Could not read or update locale settings.", language),
                      str(error), detail_format="text")


def get_locale_settings(scope: str = "system", language: str | None = None, **_) -> ExecResult:
    blocked = system_tools._settings_host_guard(language)
    if blocked is not None:
        return blocked
    try:
        values, current_language, region = _read(scope)
        language_name = _locale_label(current_language, "language", language)
        region_name = _locale_label(region, "region", language)
        detail = {"scope": "system", "channel": "host",
                  "language": current_language, "region": region, "configured_values": values,
                  "language_name": language_name, "region_name": region_name,
                  "effective_after": "next login; existing applications may retain their old locale"}
        return ExecResult(True, _reply(
            f"系统语言：{language_name}；区域：{region_name}。",
            f"System language: {language_name}; region: {region_name}.", language),
            json.dumps(detail, ensure_ascii=False, indent=2), detail_format="internal")
    except (OSError, ValueError) as error:
        return _failure(error, language)


def list_locale_options(language: str | None = None, **_) -> ExecResult:
    blocked = system_tools._settings_host_guard(language)
    if blocked is not None:
        return blocked
    try:
        installed = _installed()
        detail = {"languages": {base: label for base, label in LANGUAGES.items() if base in installed},
                  "regions": sorted(installed), "installed_locales": installed,
                  "region_names": {base: _locale_label(base, "region", language) for base in sorted(installed)}}
        languages = "、".join(_locale_label(base, "language", language) for base in detail["languages"])
        regions = "、".join(dict.fromkeys(detail["region_names"].values()))
        return ExecResult(True, _reply(
            f"可用语言：{languages}。\n可用区域：{regions}。",
            f"Available languages: {languages}.\nAvailable regions: {regions}.", language),
            json.dumps(detail, ensure_ascii=False, indent=2), detail_format="internal")
    except (OSError, ValueError) as error:
        return _failure(error, language)


def _resolve(value: str, kind: str, installed: dict[str, str]) -> str:
    base = _base(value.strip())
    if kind == "language":
        base = LANGUAGE_ALIASES.get(value.strip().lower(), base)
        if base not in LANGUAGES:
            raise ValueError("Only Simplified Chinese (zh_CN) and English (en_US) are supported")
    elif base not in installed:
        territory = REGION_ALIASES.get(value.strip().lower(), value.strip().upper())
        matches = [name for name in installed if name.endswith("_" + territory)]
        if len(matches) != 1:
            raise ValueError("Region is unavailable or ambiguous; select an exact locale from list_locale_options")
        base = matches[0]
    if base not in installed:
        raise ValueError(f"Locale is not installed: {base}. Use list_locale_options")
    return installed[base]


def _set(value: str, kind: str, scope: str, language: str | None) -> ExecResult:
    blocked = system_tools._settings_host_guard(language)
    if blocked is not None:
        return blocked
    try:
        if scope != "system":
            raise ValueError("Only global system scope is supported")
        target = _resolve(value, kind, _installed())
        values, current_language, region = _read(scope)
        if not current_language or not region:
            raise ValueError("Current language/region cannot be determined; refusing to guess")
        if kind == "language":
            updates = {key: target for key in LANGUAGE_KEYS}
            updates.update({key: values.get(key) or region for key in REGION_KEYS})
        else:
            updates = {key: target for key in REGION_KEYS}
            updates.update({key: values.get(key) or current_language for key in LANGUAGE_KEYS})
        values.update(updates)
        result = _run(["localectl", "--no-ask-password", "set-locale",
                       *(f"{key}={value}" for key, value in sorted(values.items()))])
        if not result.ok:
            return result
        label = "界面语言" if kind == "language" else "区域格式"
        target_name = _locale_label(target, kind, language)
        return ExecResult(True, _reply(
            f"已保存全局{label}配置为{target_name}。请注销后重新登录生效；未自动注销或重启。",
            f"Saved global {kind} as {target_name}. Log out and back in to apply; no logout or restart was performed.", language))
    except (OSError, ValueError, TypeError) as error:
        return _failure(error, language)


def set_system_language(locale: str, scope: str = "system", language: str | None = None, **_) -> ExecResult:
    return _set(locale, "language", scope, language)


def set_system_region(locale: str, scope: str = "system", language: str | None = None, **_) -> ExecResult:
    return _set(locale, "region", scope, language)


HANDLERS = {"get_locale_settings": get_locale_settings, "list_locale_options": list_locale_options,
            "set_system_language": set_system_language, "set_system_region": set_system_region}
system_tools.HANDLERS.update(HANDLERS)
