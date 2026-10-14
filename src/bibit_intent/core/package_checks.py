from __future__ import annotations

from dataclasses import asdict
import os
import re
import shutil
import subprocess

from bibit_intent.core.package_state import query_package_state
from bibit_intent.core.package_target import has_apt_action_suffix, is_exact_package
from bibit_intent.execution.bounded_command import OutputLimitExceeded, run_bounded_command


def _query(args: list[str]):
    return run_bounded_command(args, timeout=30, env={**os.environ, "LC_ALL": "C"})


def _failure(message: str, detail: str = "") -> dict:
    return {"ok": False, "read_only": True, "message": message, "diagnostic": detail}


_ASSESSMENT_SCOPES = {"", "system", "user", "venv", "project", "global", "local"}


def _assessment_result(
    *,
    target: str,
    version: str,
    scope: str,
    architecture: str = "unknown",
    distribution: str = "unknown",
    apt: dict | None = None,
    ok: bool = True,
    diagnosis: str = "assessment_incomplete",
    installable: str = "unknown",
    message: str = "",
    diagnostic: str = "",
    distribution_query: str = "not_checked",
    machine: str = "unknown",
    machine_query: str = "not_checked",
    foreign_architectures: list[str] | None = None,
    foreign_architectures_query: str = "not_checked",
    abi: dict | None = None,
) -> dict:
    """Build the stable, non-authoritative result shape for delivery checks."""
    return {
        "ok": ok,
        "read_only": True,
        "target": target,
        "requested_version": version,
        "scope": scope,
        "architecture": architecture,
        "machine": machine,
        "machine_query": machine_query,
        "foreign_architectures": foreign_architectures or [],
        "foreign_architectures_query": foreign_architectures_query,
        "abi": abi or {"libc": "unknown", "libc_version": "unknown", "word_size": "unknown", "status": "unknown"},
        "distribution": distribution,
        "distribution_query": distribution_query,
        "apt": apt or {},
        "source_policy": "configured_apt_only",
        "third_party_candidates": [],
        "third_party_policy": "not_queried",
        "source_build": "not_assessed",
        "network_accessed": False,
        "write_performed": False,
        "diagnosis": diagnosis,
        "installable": installable,
        "message": message,
        "diagnostic": diagnostic,
    }


def _records(text: str) -> list[dict]:
    records = []
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if fields:
            records.append(fields)
    return records


def package_candidate(pkg: str) -> dict:
    if not is_exact_package(pkg):
        return _failure("请提供完整准确的单个包名，可带 :架构；不支持模式或批量目标。")
    try:
        policy = _query(["apt-cache", "policy", pkg])
        if policy.returncode:
            return _failure("无法查询软件源候选，未刷新缓存。", policy.stderr)
        # apt-cache/apt-get interpret a trailing `+` or `-` as an action
        # suffix.  A package such as `git-` can therefore return the policy
        # for an unrelated package instead of the requested name.  Validate
        # the policy header before trusting its candidate; legitimate names
        # such as `g++` still pass because the header remains `g++:`.
        if has_apt_action_suffix(pkg):
            expected_name = pkg.partition(":")[0]
            headers = re.findall(r"^(\S+):\s*$", policy.stdout, re.M)
            if headers != [expected_name]:
                return _failure("APT 未返回与目标完全一致的软件包，可能把 + / - 解释成操作后缀；未执行包操作。", policy.stdout)
        candidates = re.findall(r"^\s*Candidate:\s*(\S+)\s*$", policy.stdout, re.M)
        if len(candidates) != 1 or candidates[0] == "(none)":
            return {"ok": True, "read_only": True, "package": pkg, "candidate": None, "installability": "unknown", "policy": policy.stdout, "message": f"{pkg} 无唯一可用候选；可能是虚拟包、缓存缺失或多个架构，请明确目标。未安装或更新软件源。"}
        candidate = candidates[0]
        shown = _query(["apt-cache", "show", "--no-all-versions", f"{pkg}={candidate}"])
        native = _query(["dpkg", "--print-architecture"])
        foreign = _query(["dpkg", "--print-foreign-architectures"])
        if shown.returncode or native.returncode or foreign.returncode:
            return _failure("候选元数据或本机架构查询失败，不判定可安装。", shown.stderr + native.stderr + foreign.stderr)
        allowed = {"all", native.stdout.strip(), *foreign.stdout.split()}
        base, _, requested_arch = pkg.partition(":")
        records = [record for record in _records(shown.stdout) if record.get("Package") == base and record.get("Version") == candidate and (not requested_arch or record.get("Architecture") in {requested_arch, "all"})]
        if not records or not native.stdout.strip():
            return _failure("候选信息不完整或与目标不一致，不判定可安装。")
        items = [{"version": candidate, "architecture": record.get("Architecture", "unknown"), "architecture_compatible": record.get("Architecture") in allowed, "essential": record.get("Essential", "unknown"), "protected": record.get("Protected", "unknown")} for record in records]
        state = query_package_state(pkg)
        lines = [f"{pkg} 的 APT 候选版本：{candidate}。"]
        for item in items:
            compatibility = "架构已启用" if item["architecture_compatible"] else "架构未知或未启用"
            lines.append(f"- {item['architecture']}：{compatibility}")
        lines.append(f"本机状态：{state.status}" + (f"，版本 {state.version}" if state.version else "") + ("，hold 已锁定" if state.held else ""))
        lines.append("仅查询本地 APT 缓存；来源/优先级见详情 policy。架构匹配不代表依赖可满足或一定可安装，未执行任何安装。")
        return {"ok": True, "read_only": True, "package": pkg, "candidate": candidate, "items": items, "local_state": asdict(state), "policy": policy.stdout, "installability": "not_verified", "message": "\n".join(lines)}
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
        return _failure("候选查询不可用、超时或超过资源上限；未执行包操作。")


def software_delivery_assess(target: str = "", version: str = "", scope: str = "") -> dict:
    """Read-only assessment of local RISC-V software delivery options."""
    if not isinstance(target, str) or not target or not is_exact_package(target):
        return _assessment_result(
            target=str(target or ""), version=str(version or ""), scope=str(scope or ""), ok=False,
            diagnosis="invalid_target", message="请提供完整准确的软件包名；评估仅查询本地 APT，不支持模式或下载。",
        )
    if not isinstance(version, str) or len(version) > 128 or (version and not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", version)):
        return _assessment_result(
            target=target, version=str(version or ""), scope=str(scope or ""), ok=False,
            diagnosis="invalid_version", message="版本参数不是有效的 Debian 版本格式；未执行任何包操作。",
        )
    if not isinstance(scope, str) or len(scope) > 32 or any(ord(char) < 32 for char in scope) or scope not in _ASSESSMENT_SCOPES:
        return _assessment_result(
            target=target, version=version, scope=str(scope or ""), ok=False,
            diagnosis="invalid_scope", message="作用域仅支持 system、user、venv、project、global 或 local；未执行任何包操作。",
        )
    try:
        arch = _query(["dpkg", "--print-architecture"])
        if arch.returncode:
            return _assessment_result(
                target=target, version=version, scope=scope, ok=False,
                diagnosis="architecture_unknown", diagnostic=arch.stderr,
                message="无法确定本机架构，不能评估软件交付；未执行任何写操作。",
            )
        architecture = arch.stdout.strip()
        if not architecture or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", architecture):
            return _assessment_result(
                target=target, version=version, scope=scope, ok=False,
                diagnosis="architecture_unknown", diagnostic=arch.stdout,
                message="本机架构查询结果无效，不能评估软件交付；未执行任何写操作。",
            )

        machine = _query(["uname", "-m"])
        foreign = _query(["dpkg", "--print-foreign-architectures"])
        libc = _query(["getconf", "GNU_LIBC_VERSION"])
        word_size = _query(["getconf", "LONG_BIT"])
        release = _query(["lsb_release", "-ds"])

        machine_name = machine.stdout.strip() if machine.returncode == 0 and machine.stdout.strip() else "unknown"
        machine_query = "ok" if machine.returncode == 0 and machine.stdout.strip() else "failed"
        foreign_architectures = (
            foreign.stdout.split()
            if foreign.returncode == 0
            else []
        )
        foreign_architectures_query = "ok" if foreign.returncode == 0 else "failed"
        libc_text = libc.stdout.strip() if libc.returncode == 0 and libc.stdout.strip() else "unknown"
        libc_name, separator, libc_version = libc_text.partition(" ")
        if not separator:
            libc_name, libc_version = (libc_text, "unknown") if libc_text != "unknown" else ("unknown", "unknown")
        word_size_value = word_size.stdout.strip() if word_size.returncode == 0 and word_size.stdout.strip() else "unknown"
        abi_status = "complete" if libc_name != "unknown" and libc_version != "unknown" and word_size_value != "unknown" else "partial"
        abi = {
            "libc": libc_name,
            "libc_version": libc_version,
            "word_size": word_size_value,
            "status": abi_status,
        }

        candidate = package_candidate(target)
        distribution = release.stdout.strip() if release.returncode == 0 and release.stdout.strip() else "unknown"
        distribution_query = "ok" if release.returncode == 0 and release.stdout.strip() else "failed"
        if not candidate.get("ok"):
            return _assessment_result(
                target=target, version=version, scope=scope, architecture=architecture,
                distribution=distribution, distribution_query=distribution_query, apt=candidate,
                machine=machine_name, machine_query=machine_query,
                foreign_architectures=foreign_architectures,
                foreign_architectures_query=foreign_architectures_query, abi=abi,
                ok=False, diagnosis="repository_configuration_unknown", diagnostic=candidate.get("diagnostic", ""),
                message="无法确认当前 APT 软件源或候选元数据；未执行刷新、下载或安装。",
            )

        result = _assessment_result(
            target=target, version=version, scope=scope, architecture=architecture,
            distribution=distribution, distribution_query=distribution_query, apt=candidate,
            machine=machine_name, machine_query=machine_query,
            foreign_architectures=foreign_architectures,
            foreign_architectures_query=foreign_architectures_query, abi=abi,
        )
        candidate_version = candidate.get("candidate")
        if not candidate_version:
            result.update(
                diagnosis="candidate_not_found",
                installable="unknown",
                message=f"{target} 在本机当前配置的 APT 中没有可用候选；未查询或下载第三方来源。",
            )
            return result

        items = candidate.get("items") or []
        compatible = any(item.get("architecture_compatible") for item in items)
        if not compatible:
            result.update(
                diagnosis="repo_package_unavailable_for_arch",
                installable="unknown",
                message=f"{target} 有 APT 候选，但没有与本机 {architecture} 架构匹配的已查询元数据；未安装。",
            )
            return result

        if version and candidate_version != version:
            result.update(
                diagnosis="repo_candidate_found_version_differs",
                installable="unknown",
                message=f"{target} 的 APT 当前候选是 {candidate_version}，与请求版本 {version} 不同；未宣称目标版本可安装。",
            )
            return result

        result.update(
            diagnosis="repo_candidate_found",
            installable="not_verified",
            message=f"{target} 在本机已配置 APT 中存在与 {architecture} 匹配的候选；仅完成架构和候选查询，未安装。",
        )
        return result
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded) as exc:
        return _assessment_result(
            target=target, version=version, scope=scope, ok=False,
            diagnosis="assessment_unavailable", diagnostic=str(exc),
            message="软件交付评估失败、超时或超过资源上限；未执行任何写操作。",
        )


def apt_install_target(pkg: str) -> str | None:
    if not is_exact_package(pkg):
        return None
    if not has_apt_action_suffix(pkg):
        return pkg
    candidate = package_candidate(pkg)
    if not candidate.get("ok") or not candidate.get("candidate"):
        return None
    return f"{pkg}={candidate['candidate']}"


def package_operation_preview(pkg: str, operation: str) -> dict:
    if not is_exact_package(pkg) or operation not in {"install", "remove"}:
        return _failure("预检仅接受准确的单个包名及 install/remove 操作。")
    try:
        state = query_package_state(pkg)
        if state.status == "unknown":
            return _failure("本机包状态未知，无法完整预检。", state.detail)
        target = apt_install_target(pkg) if operation == "install" else pkg
        if operation == "remove" and has_apt_action_suffix(pkg):
            target = f"{pkg}={state.version}" if state.status == "installed" and state.version else None
        if target is None:
            return _failure("目标包含可能被 APT 解释为操作的后缀，且无法核实完整包名和版本；未执行预检。")
        flags = ["--no-remove"] if operation == "install" else []
        simulated = _query(["apt-get", "-s", "-o", "Debug::NoLocking=1", *flags, operation, "--", target])
        if simulated.returncode:
            diagnostic = simulated.stdout + simulated.stderr
            message = "APT 只读预演失败；未执行安装、卸载或修复。"
            if "unmet dependencies" in diagnostic.lower():
                reasons = [line.strip()[:240] for line in diagnostic.splitlines() if "Depends:" in line or "PreDepends:" in line or "Conflicts:" in line or "Breaks:" in line]
                message += "当前系统存在未满足的依赖，预检不能继续。"
                if reasons:
                    message += "\n" + "\n".join("- " + reason for reason in reasons[:6])
                message += "\n请先核对软件包发布版本和依赖关系；本工具不会自动执行 fix-broken 或重装。"
            return _failure(message, diagnostic)
        changes = []
        unparsed = []
        for line in simulated.stdout.splitlines():
            if not re.match(r"^(Inst|Remv|Conf)\b", line):
                continue
            matched = re.fullmatch(r"(Inst|Remv|Conf) (\S+)(?: \[([^\]]+)\])?(?: \(([^ )]+).*\))?", line)
            if not matched or not is_exact_package(matched[2]):
                unparsed.append(line)
                continue
            action, name, old, new = matched.groups()
            changes.append({"action": {"Inst": "install_or_upgrade", "Remv": "remove", "Conf": "configure"}[action], "package": name, "old_version": old, "new_version": new if action != "Remv" else None})
        if len(changes) > 2000:
            return _failure("变更集合过大，不生成不完整计划。")
        lines = [f"{pkg} 的{('安装' if operation == 'install' else '卸载')}只读预检：{len(changes)} 条变更记录（含配置步骤，非包总数）。"]
        labels = {"install_or_upgrade": "安装/升级", "remove": "移除", "configure": "配置"}
        for change in changes[:50]:
            lines.append(f"- {labels[change['action']]} {change['package']}：{change['old_version'] or '未知/无旧版本'} → {change['new_version'] or '无目标版本'}")
        if len(changes) > 50:
            lines.append("仅展示前 50 条，完整记录见详情。")
        if state.held:
            lines.append("目标处于 hold 锁定状态，预演不构成解除锁定的授权。")
        if unparsed:
            lines.append("存在未解析步骤，不能视为完整计划。")
        lines.append("未执行任何变更；未验证受保护包策略，下载体积和磁盘需求未知。预演不锁定系统状态，执行前必须重新检查，不是授权凭证。")
        return {"ok": not unparsed, "read_only": True, "package": pkg, "operation": operation, "local_state": asdict(state), "changes": changes, "parse_complete": not unparsed, "unparsed": unparsed, "download_bytes": None, "disk_delta_bytes": None, "protected_policy": "not_checked", "diagnostic": simulated.stdout + simulated.stderr, "message": "\n".join(lines)}
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
        return _failure("预检不可用、超时或超过资源上限；未执行包操作。")


def verify_package(pkg: str, expected_version: str = "", executable: str = "") -> dict:
    if not is_exact_package(pkg) or not isinstance(expected_version, str) or len(expected_version) > 128 or (expected_version and not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", expected_version)):
        return _failure("请提供准确单包名和有效 Debian 目标版本；不执行包操作。")
    if not isinstance(executable, str) or (executable and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,127}", executable)):
        return _failure("可选 executable 仅接受单个命令名，不接受路径、参数或脚本。")
    state = query_package_state(pkg)
    matched = None
    try:
        if state.status == "installed" and expected_version:
            compared = _query(["dpkg", "--compare-versions", state.version, "eq", expected_version])
            if compared.returncode not in (0, 1):
                return _failure("版本比较失败，不能确认安装结果。", compared.stderr)
            matched = compared.returncode == 0
    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
        return _failure("版本比较不可用、超时或超过资源上限。")
    ok = state.status == "installed" and matched is not False
    status = {"installed": "已安装", "absent": "未安装", "config_only": "仅残留配置", "incomplete": "未配置完成或状态异常", "unknown": "状态未知"}[state.status]
    message = f"{pkg}：{status}" + (f"，实际版本 {state.version}" if state.version else "")
    if expected_version:
        message += f"；目标版本 {expected_version}：" + ("匹配" if matched is True else "不匹配" if matched is False else "未能验证")
    else:
        message += "；未提供目标版本，仅验证包安装状态"
    command_check = {"status": "not_checked", "executable": executable or None, "path": None}
    if executable:
        path = shutil.which(executable)
        command_check.update(status="not_found" if path is None else "ownership_unknown", path=path)
        if path and state.status == "installed":
            try:
                owner = _query(["dpkg-query", "-S", path])
                for line in owner.stdout.splitlines() if owner.returncode == 0 else []:
                    packages, separator, filename = line.rpartition(": ")
                    owners = packages.split(", ")
                    if separator and filename == path and any(name == pkg or (":" not in pkg and name.split(":", 1)[0] == pkg) for name in owners):
                        command_check["status"] = "executable_present"
                        break
            except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded):
                pass
        ok = ok and command_check["status"] == "executable_present"
        message += f"；命令 {executable}：" + {"not_found": "当前 PATH 中未找到", "ownership_unknown": "存在但未能核实属于目标包", "executable_present": "存在且属于目标包"}[command_check["status"]]
    message += "。未验证应用启动或功能可用性，未自动启动应用或重新安装。"
    return {"ok": ok, "read_only": True, "package": pkg, "local_state": asdict(state), "expected_version": expected_version or None, "version_matches": matched, "command_check": command_check, "usability": "not_checked", "message": message}
