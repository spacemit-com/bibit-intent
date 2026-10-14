from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command as run_command


@dataclass(frozen=True)
class PackageState:
    status: str
    version: str = ""
    held: bool = False
    detail: str = ""


@dataclass(frozen=True)
class DpkgAudit:
    status: str
    detail: str = ""
    # 需要重新安装的包名（eflag=reinstreq 或 half-installed）
    reinstreq: tuple[str, ...] = ()
    # 是否存在需要 dpkg --configure -a / --pending 的未完成配置
    configure: bool = False


def query_package_state(package: str) -> PackageState:
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?", package):
        return PackageState("unknown", detail="需要完整准确的单个软件包名")
    try:
        result = run_command(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Version}\n", package],
            timeout=15,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
        return PackageState("unknown", detail="软件包状态查询不可用或超时")
    if result.returncode:
        if result.returncode == 1 and result.stderr.strip() == f"dpkg-query: no packages found matching {package}":
            return PackageState("absent")
        return PackageState("unknown", detail=result.stderr.strip())
    rows = result.stdout.splitlines()
    if len(rows) != 1:
        return PackageState("unknown", detail="软件包状态为空或匹配多个架构，请明确包名和架构")
    abbreviation, separator, version = rows[0].partition("\t")
    if not separator or len(abbreviation) != 3:
        return PackageState("unknown", detail="软件包状态格式异常")
    if abbreviation[2] != " ":
        return PackageState("incomplete", version.strip(), detail=abbreviation)
    if abbreviation[1] == "i" and version.strip():
        return PackageState("installed", version.strip(), abbreviation[0] == "h")
    if abbreviation[1] == "c":
        return PackageState("config_only", detail=abbreviation)
    if abbreviation[1] == "n":
        return PackageState("absent")
    return PackageState("incomplete", version.strip(), detail=abbreviation)


def _read_dpkg_status_file() -> str:
    """读取 dpkg 状态数据库。

    独立成函数便于测试注入；实际文件通常为 /var/lib/dpkg/status，普通用户可读。
    """
    return Path("/var/lib/dpkg/status").read_text(encoding="utf-8", errors="replace")


# 这些状态/触发器表示 dpkg 数据库里有未完成事务，安装前需要先修复。
_DPKG_BROKEN_STATUSES = frozenset({"half-installed", "half-configured", "unpacked"})


def _parse_dpkg_status(text: str) -> tuple[list[str], list[str], list[str]]:
    """从 dpkg status 文本中抽出异常包。

    返回 (reinstreq_packages, configure_packages, detail_lines)。
    只解析 Package/Status/Triggers 字段，不执行 dpkg 子进程，因此不要求 root。
    """
    reinstreq: list[str] = []
    configure: list[str] = []
    details: list[str] = []

    package = ""
    status_words: list[str] = []
    triggers_pending = False
    triggers_awaited = False

    def flush() -> None:
        nonlocal package, status_words, triggers_pending, triggers_awaited
        if not package:
            return
        eflag = status_words[1] if len(status_words) >= 2 else ""
        pkg_status = status_words[2] if len(status_words) >= 3 else ""
        if eflag == "reinstreq" or pkg_status == "half-installed":
            reinstreq.append(package)
            details.append(f"{package} 需要重新安装（dpkg status: {' '.join(status_words)}）")
        elif pkg_status in _DPKG_BROKEN_STATUSES or triggers_pending or triggers_awaited:
            configure.append(package)
            trigger = " triggers-pending" if triggers_pending else ""
            if triggers_awaited:
                trigger += " triggers-awaited"
            details.append(f"{package} 存在未完成的 dpkg 配置/触发器（dpkg status: {' '.join(status_words)}{trigger}）")
        package = ""
        status_words = []
        triggers_pending = False
        triggers_awaited = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith("Package:"):
            package = line.split(":", 1)[1].strip()
        elif line.startswith("Status:"):
            status_words = line.split(":", 1)[1].strip().split()
        elif line.startswith("Triggers-Pending:"):
            triggers_pending = bool(line.split(":", 1)[1].strip())
        elif line.startswith("Triggers-Awaited:"):
            triggers_awaited = bool(line.split(":", 1)[1].strip())
    flush()
    return reinstreq, configure, details


def query_dpkg_audit() -> DpkgAudit:
    """检查 dpkg 数据库是否有未完成配置/严重异常包。

    Runtime/MCP worker 通常以普通用户运行，而 ``dpkg --audit`` 需要读取
    ``/var/lib/dpkg/lock``，会因权限不足直接失败。这里改为解析
    ``/var/lib/dpkg/status``（普通用户可读），返回 healthy / interrupted /
    unknown 三态，并给出可自动修复的包清单。
    """
    try:
        text = _read_dpkg_status_file()
    except OSError as exc:
        return DpkgAudit("unknown", detail=f"无法读取 dpkg 状态数据库：{exc}")
    if not text.strip():
        return DpkgAudit("unknown", detail="dpkg 状态数据库为空")
    reinstreq, configure, details = _parse_dpkg_status(text)
    if reinstreq or configure:
        return DpkgAudit(
            "interrupted",
            detail="\n".join(details),
            reinstreq=tuple(reinstreq),
            configure=bool(configure),
        )
    return DpkgAudit("healthy")
