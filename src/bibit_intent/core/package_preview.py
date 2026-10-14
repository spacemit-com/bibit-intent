from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from bibit_intent.core.package_target import is_exact_package
from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command as run_command


PREVIEW_SCOPES = {"软件源": "available", "已安装": "installed"}
PREVIEW_MODES = {"通配符": "glob", "正则": "regex", "正则表达式": "regex"}


def preview_slots(text: str) -> dict | None:
    match = re.fullmatch(r"(?:软件包预览|包预览|package preview)(?:\s+(.*))?", text.strip())
    if match is None:
        return None
    remaining = (match[1] or "").strip()
    slots = {}
    for key, aliases, values in [("scope", PREVIEW_SCOPES, {"available", "installed"}), ("mode", PREVIEW_MODES, {"glob", "regex"})]:
        parts = remaining.split(maxsplit=1)
        token = parts[0] if parts else ""
        if token in aliases or token in values:
            slots[key] = aliases.get(token, token)
            remaining = parts[1] if len(parts) > 1 else ""
    if remaining:
        slots["pattern"] = remaining
    return slots


_MATCHER = """
import fnmatch, json, re, sys
payload = json.load(sys.stdin)
try:
    expression = re.compile(payload['pattern']) if payload['mode'] == 'regex' else None
except re.error:
    print(json.dumps({'error': 'invalid_pattern'}))
    sys.exit(0)
matches = []
for name in payload['names']:
    matched = expression.fullmatch(name) if expression is not None else fnmatch.fnmatchcase(name, payload['pattern'])
    if matched:
        matches.append(name)
        if len(matches) > payload['limit']:
            break
print(json.dumps({'matches': matches[:payload['limit']], 'truncated': len(matches) > payload['limit']}))
"""


def format_preview_message(preview: dict) -> str:
    if not preview["ok"]:
        return preview["message"]
    scope = "软件源名称缓存" if preview["scope"] == "available" else "本机已安装及异常包"
    mode = "通配符" if preview["mode"] == "glob" else "正则表达式"
    lines = [preview["message"], f"范围：{scope}；方式：{mode}（完整包名匹配、区分大小写）。"]
    labels = {"installed": "已安装", "absent": "未安装", "config_only": "仅残留配置", "incomplete": "未配置完成或状态异常"}
    for item in preview["items"]:
        states = []
        for state in item["local_states"]:
            parts = [labels.get(state["status"], "状态未知")]
            if len(item["local_states"]) > 1 or state.get("package", item["package"]) != item["package"]:
                parts.insert(0, state.get("package", item["package"]))
            if state.get("version"):
                parts.append(f"版本 {state['version']}")
            if state.get("held"):
                parts.append("已锁定（hold）")
            states.append(" / ".join(parts))
        lines.append(f"- {item['package']}：{'；'.join(states)}")
    if not preview["items"]:
        lines.append("请检查范围和表达式：通配符前缀匹配用 bash*，正则前缀匹配用 bash.*。")
    lines.append("结果仅为当前快照，不构成安装或卸载授权；本工具不会自动执行包操作。")
    return "\n".join(lines)


def preview_packages(pattern: str, mode: str, scope: str, limit: int = 20) -> dict:
    def failure(code: str, message: str) -> dict:
        return {"ok": False, "error": code, "message": message, "read_only": True}

    if mode not in {"glob", "regex"} or scope not in {"available", "installed"}:
        return failure("invalid_mode", "请指定 glob/regex 和 available/installed，不自动猜测匹配语法。")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 128 or any(ord(char) < 32 for char in pattern):
        return failure("invalid_pattern", "表达式不能为空，最多 128 个字符，且不能含控制字符。")
    if type(limit) is not int or not 1 <= limit <= 50:
        return failure("invalid_limit", "预览条数须为 1–50。")
    environment = {**os.environ, "LC_ALL": "C"}
    try:
        installed = run_command(
            ["dpkg-query", "-W", "-f=${binary:Package}\t${db:Status-Abbrev}\t${Version}\n"],
            timeout=20, env=environment,
        )
        if installed.returncode or len(installed.stdout) > 8_000_000:
            return failure("state_query_failed", "无法完整读取本机软件包状态，未进行匹配。")
        records = {}
        for line in installed.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or not is_exact_package(fields[0]) or len(fields[1]) != 3:
                return failure("state_query_failed", "本机软件包状态格式异常，未进行匹配。")
            name, status, version = fields
            state = "installed" if status[1:] == "i " else "config_only" if status[1:] == "c " else "absent" if status[1:] == "n " else "incomplete"
            records[name] = {"package": name, "status": state, "version": version, "held": status[0] == "h"}
        if scope == "available":
            catalog = run_command(["apt-cache", "pkgnames"], timeout=30, env=environment)
            if catalog.returncode or len(catalog.stdout) > 8_000_000:
                return failure("catalog_failed", "无法读取本地 APT 名称缓存，未刷新软件源。")
            names = sorted({name for name in catalog.stdout.splitlines() if is_exact_package(name)})
        else:
            names = sorted(name for name, record in records.items() if record["status"] in {"installed", "incomplete"})
        if len(names) > 200_000:
            return failure("catalog_too_large", "候选集合过大，请缩小预览范围。")
        matched = run_command(
            [sys.executable, "-I", "-c", _MATCHER], timeout=2,
            input=json.dumps({"pattern": pattern, "mode": mode, "names": names, "limit": limit}),
        )
        if matched.returncode:
            return failure("match_failed", "匹配进程失败，未执行任何包操作。")
        result = json.loads(matched.stdout)
        if result.get("error"):
            return failure("invalid_pattern", "正则表达式无效，请检查语法。")
    except OutputLimitExceeded:
        return failure("output_limit", "查询或匹配输出超过资源上限，已停止子进程；请缩小范围。")
    except subprocess.TimeoutExpired:
        return failure("preview_timeout", "查询或匹配超时；请缩小范围或简化表达式，未执行安装/卸载。")
    except (FileNotFoundError, OSError, ValueError):
        return failure("preview_unavailable", "预览工具不可用或返回格式异常，未执行安装/卸载。")
    items = []
    for name in result["matches"]:
        states = [record for key, record in records.items() if key == name or (":" not in name and key.split(":", 1)[0] == name)]
        items.append({"package": name, "local_states": states or [{"status": "absent", "version": ""}]})
    return {
        "ok": True, "read_only": True, "pattern": pattern, "mode": mode, "scope": scope,
        "match_semantics": "full_name", "items": items, "returned_count": len(items),
        "truncated": result["truncated"],
        "message": "仅匹配预览，未执行安装或卸载。" + ("未找到匹配的软件包。" if not items else f"返回 {len(items)} 项匹配。")
        + ("匹配过多，已截断，请缩小表达式。" if result["truncated"] else "")
        + ("APT 名称缓存可能包含虚拟包或过期记录，不代表可安装、架构兼容或依赖可满足。" if scope == "available" else "范围包含已安装及未配置完成的包，不包含仅残留配置的包。"),
    }
