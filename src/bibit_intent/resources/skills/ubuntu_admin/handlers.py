"""
skills/ubuntu_admin/handlers.py — Ubuntu 系统管理 Skill 的执行实现

依据 wiki.ubuntu.org.cn/UbuntuSkills 提炼的「修改类」运维命令。
安全：本文件只做参数化执行；所有危险操作由执行层二次确认把关
（skill.yaml 的 confirm: true → skill_loader 透传 → Executor 确认回调）。
非交互（管道/EOF）环境下确认回调默认拒绝，不会误执行。
"""
from __future__ import annotations

import os
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bibit_intent.execution.cancellation import (
    EdgeExecutionCancelled,
    end_execution_without_side_effect,
    raise_if_cancelled,
    run_command,
)
from bibit_intent.core import package_state
from bibit_intent.core.progress import report_progress
from bibit_intent.core.package_checks import apt_install_target, verify_package
from bibit_intent.core.pip_index import PIP_INDEX_URL as _PIP_INDEX_URL
from bibit_intent.core.package_target import (
    has_apt_action_suffix,
    is_exact_package,
    split_exact_install_specs,
    split_exact_packages,
)
from bibit_intent.execution.system_tools import _find_desktop_session_env
from bibit_intent.execution import progress as install_progress
from bibit_intent.routing.pkg_resolver import suggest_install_packages


@dataclass
class ExecResult:
    ok: bool
    message: str
    detail: str = ""
    # 需要升级云端：非 None 表示本地结果不足以作答（与 router 判空一致）。
    escalate: str | None = None
    detail_format: Literal["text", "markdown"] = "markdown"
    # 需要用户补充/确认的参数名；执行层缺参多轮机制会据此挂起会话。
    need_param: str | None = None
    # 该挂起不是缺参回填，而是“用户确认后执行”；router 会按确认挂起处理。
    confirm: bool = False


# 「系统更新」不能把当前正在执行更新的 Agent 栈一起升级：升级 bianbu-agent-runtime
# 会 stop Runtime，而 Runtime 的 systemd unit 使用 KillMode=control-group，会连同
# 当前 bibit-intent MCP 与它拉起的 apt-get 一起 SIGTERM，最终既报 EOFError，又把
# dpkg 留在 half-configured 状态。这些包必须交给独立的 systemd transient unit 在
# Runtime cgroup 之外分阶段升级。
_SELF_UPDATE_PACKAGES = frozenset({
    "agent-power-manager",
    "bi-bit",
    "bianbu-agent-memory",
    "bianbu-agent-memoryd",
    "bianbu-agent-runtime",
    "bianbu-agent-securityd",
    "bibit-agenthub-cli",
    "bibit-agenthub-plugin",
    "bibit-filesearch",
    "bibit-filesearch-plugin",
    "bibit-intent",
    "bibit-memory-plugin",
    "bibit-runtime-plugin",
    "bibit-voice",
})
_SELF_UPDATE_SCRIPT = "/opt/bibit-intent/bibit-agent-upgrade.sh"
_SELF_UPDATE_LOG = "/var/log/bibit-agent-upgrade.log"
_SYSTEM_UPDATE_STATE = Path("/var/lib/bibit-intent/system-update.status")
_SYSTEM_UPDATE_DETAIL = _SYSTEM_UPDATE_STATE.with_suffix(".json")
_SYSTEM_UPDATE_TERMINAL = {
    "succeeded",
    "succeeded_with_skips",
    "cancelled",
    "failed",
}
_SYSTEM_UPDATE_LABELS = {
    "refreshing": "正在刷新软件源",
    "checking": "正在检查更新",
    "awaiting_confirmation": "检测到降级，等待确认",
    "updating": "正在更新系统",
    "system_succeeded": "系统软件包更新完成，正在准备升级 Bibit 组件",
    "agent_upgrading": "正在升级 Bibit 组件（Runtime 将短暂断开）",
    "succeeded": "系统更新已完成",
    "succeeded_with_skips": "系统更新已完成，已跳过降级软件包",
    "cancelled": "已取消系统更新",
    "failed": "系统更新失败",
}
_SYSTEM_UPDATE_PERCENT = {
    "refreshing": 5,
    "checking": 15,
    "awaiting_confirmation": 30,
    "updating": 60,
    "system_succeeded": 70,
    "agent_upgrading": 75,
    "succeeded": 100,
    "succeeded_with_skips": 100,
    "cancelled": 100,
    "failed": 100,
}


def _run(cmd: list[str], timeout: int = 60) -> ExecResult:
    """参数化执行，cmd 必须是列表，杜绝 shell 注入。"""
    try:
        p = run_command(cmd, timeout=timeout)
        if p.returncode == 0:
            return ExecResult(True, "ok", p.stdout, detail_format="text")
        return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")
    except FileNotFoundError:
        return ExecResult(False, f"未安装命令 {cmd[0]}：请先 apt-get install 对应软件包")
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")


def _session_askpass_env() -> dict[str, str] | None:
    """组装可供 Qt 版 ssh-askpass 连接图形会话的子进程环境；不可用返回 None。

    agent 常在 systemd user 服务 / SSH 等上下文执行：环境里的 DISPLAY/
    WAYLAND_DISPLAY 可能是残留（缺 XAUTHORITY / XDG_RUNTIME_DIR），Qt 版
    askpass 连不上显示会话会刷一串 qt.qpa 报错且授权框永不出现。因此不盲信
    当前环境，沿用 execution.system_tools 的既有做法：从当前用户真实桌面会话
    进程（labwc/Xorg…）的 /proc/<pid>/environ 读取会话环境。主题/显示变量
    （QT_QPA_PLATFORMTHEME、QT_PLATFORM_PLUGIN、LXQT_SESSION_CONFIG、
    XDG_CURRENT_DESKTOP…）只存在于会话进程环境里，systemd user 服务不携带，
    故整体以会话环境覆盖，授权框才能引用系统主题。找不到可用会话时返回
    None，由调用方直接报错升级云端，避免启动注定失败的弹框。
    """
    env = os.environ.copy()
    session = _find_desktop_session_env()
    if session is not None:
        env.update(session)
        return env
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    # 无会话进程命中时的保守兜底：仅当显示环境自洽才放行，否则照样是 qt.qpa
    # 噪音。Wayland 需要 XDG_RUNTIME_DIR 下的 socket；X11 的 xcb 需要
    # XAUTHORITY（缺省读 ~/.Xauthority）。
    if env.get("WAYLAND_DISPLAY"):
        way = env["WAYLAND_DISPLAY"]
        sock = way if os.path.isabs(way) else str(Path(env.get("XDG_RUNTIME_DIR") or "", way))
        if Path(sock).is_socket():
            return env
        return None
    auth = env.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    return env if os.path.exists(auth) else None


_SESSION_ENV_KEYS = frozenset({
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "HOME",
    "LANG",
    "SDL_VIDEODRIVER",
})
_THEME_ENV_PREFIXES = ("QT_", "GTK_", "LXQT_", "XDG_", "LC_")


def _shell_quote(value: str) -> str:
    """POSIX shell 单引号转义，用于把值写进 env 文件/脚本。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _shell_sequence(commands: list[list[str]]) -> str:
    """把一组参数化命令拼成一个 ``set -e`` shell 脚本。

    用于把「先修复 dpkg、再执行 apt 安装」放进同一个 ``sudo -A`` 会话，
    保证只弹一次密码框。命令参数用 shlex.join 逐一引用，不拼接未转义字符串。
    任一命令是 apt/dpkg 时在脚本里导出稳定的包管理器环境。
    """
    lines = ["set -e"]
    if any(Path(cmd[0]).name in {"apt", "apt-get", "dpkg"} for cmd in commands):
        lines.append("export TERM=dumb DEBIAN_FRONTEND=noninteractive")
    lines.extend(shlex.join(cmd) for cmd in commands)
    return "\n".join(lines) + "\n"


def _install_askpass_wrapper(env: dict[str, str], real_askpass: str) -> str | None:
    """把 GUI askpass 包一层“先恢复会话环境”的脚本，返回脚本路径；失败 None。

    sudo -A 启动 askpass 前会按 env_reset 清空环境（实测 QT_*/GTK_*/LXQT_*/
    DISPLAY/WAYLAND_DISPLAY 等全丢，只剩 HOME/LANG/XDG_RUNTIME_DIR），导致
    授权框既不引用系统 Qt 主题、部分环境连显示都连不上。包装脚本先重新导入
    桌面会话的 Qt/主题/显示变量，再 exec 真正的 askpass，外观与桌面一致。
    """
    try:
        runtime = env.get("XDG_RUNTIME_DIR") or os.path.expanduser("~")
        base = Path(runtime) / "bibit-askpass"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        env_file = base / "env"
        wrapper = base / "askpass-wrapper.sh"

        lines: list[str] = []
        for key, value in env.items():
            if key in _SESSION_ENV_KEYS or key.startswith(_THEME_ENV_PREFIXES):
                lines.append(f"export {key}={_shell_quote(value)}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        env_file.chmod(0o600)

        script = (
            "#!/bin/sh\n"
            "# bibit-intent: sudo -A 会清空 askpass 环境，先恢复桌面会话的\n"
            "# Qt/主题/显示变量，再启动授权框（与桌面内主题外观一致）。\n"
            f". {_shell_quote(str(env_file))} 2>/dev/null || exit 1\n"
            f"exec {_shell_quote(real_askpass)} \"$@\"\n"
        )
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o700)
        return str(wrapper)
    except OSError:
        return None


_APT_INSTALL_TIMEOUT = 1800
# 便于测试改写：正常运行时只读当前进程的状态文件。
_PROC_STATUS_PATH = "/proc/self/status"


def _no_new_privs_active() -> bool:
    """当前进程是否带 no_new_privs 标志。

    ``bianbu-agent-runtime@.service`` 设了 ``NoNewPrivileges=true``，Control 与其
    拉起的 Worker、以及 Worker 再拉起的 MCP 子进程都会继承该标志。内核在有这个
    标志时忽略 setuid 位，``sudo`` 只会打印
    ``The "no new privileges" flag is set`` 而永远拿不到 root。
    """
    try:
        with open(_PROC_STATUS_PATH, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
    except OSError:
        return False
    return False


def _usr_mount_read_only() -> bool:
    """判断当前进程所在 mount namespace 的 ``/usr`` 是否只读。

    Runtime 的 Worker/MCP 会继承一个独立 mount namespace，其中 ``/usr`` 被
    绑定为只读（即使 root 也写不进去）。此时在 MCP 进程里直接 ``sudo -A``
    提权没有意义，必须和 ``NoNewPrivileges`` 一样改由 systemd 用户管理器在
    host mount namespace 中执行。
    """
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    best: tuple[str, str] | None = None
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 6:
            continue
        mount_point = fields[4]
        options = fields[5]
        prefix = mount_point if mount_point.endswith("/") else mount_point + "/"
        if "/usr" == mount_point or "/usr".startswith(prefix):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, options)
    if best is None:
        return False
    return "ro" in best[1].split(",")


def _user_manager_env(env: dict[str, str]) -> dict[str, str]:
    """用户管理器（systemd --user）总线所需的变量；缺失时按 uid 推导。

    Runtime 服务环境里没有 XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS，
    ``systemctl --user``/``systemd-run --user`` 找不到用户管理器。
    """
    result = dict(env)
    runtime_dir = result.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    result["XDG_RUNTIME_DIR"] = runtime_dir
    result.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    return result


def _stop_user_manager_unit(unit: str, env: dict[str, str]) -> None:
    """取消/超时后真的停掉 transient unit。

    实测 ``systemd-run --wait --pipe`` 的客户端被杀不会带走 unit（apt-get 会
    继续在后台跑完），所以必须显式 stop，否则「取消」会留下半成品安装。
    """
    try:
        run_command(
            ["systemctl", "--user", "stop", unit],
            timeout=15,
            env=_user_manager_env(env),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        # 清理是尽力而为：unit 可能已结束并被 --collect 回收，取消路径上也
        # 不能再抛异常盖掉真正的取消/超时结果。
        pass


def _sudo_auth_failure_detail(stdout: str, stderr: str) -> str | None:
    """判断 ``sudo -A <cmd>`` 的非零返回是否由授权失败导致，返回可展示详情。

    端侧使用 sudo-rs，``sudo -A -v`` 不会像 GNU sudo 那样可靠地写时间戳，
    在同一个任务里先 ``-v`` 再执行命令会弹两次密码框。这里改为只执行一次
    ``sudo -A <cmd>``，通过 stderr/stdout 识别「密码错误/取消/未授权」并撤回
    executor 的「已进入执行」标记；其它错误仍按目标命令失败处理。
    """
    detail = stderr or stdout or ""
    low = detail.lower()
    markers = (
        "incorrect password",
        "incorrect authentication",
        "authentication failure",
        "a password is required",
        "no password was provided",
        "not allowed to execute",
        "sorry, try again",
        "密码错误",
        "认证失败",
    )
    return detail if any(marker in low for marker in markers) else None


def _run_sudo_via_user_manager(
    cmd: list[str],
    timeout: int | None,
    env: dict[str, str],
    *,
    input: str | None = None,
    on_output=None,
    force_reauth: bool = False,
) -> ExecResult:
    """在带 no_new_privs 的进程里，把提权执行交给用户管理器。

    ``systemd --user`` 拉起的 transient unit 不继承 Runtime 服务的
    ``NoNewPrivileges``，因此 sudo 能正常读 setuid 位。授权框仍由用户在桌面上
    输入密码（``sudo -A`` + ssh-askpass），这里只是换了一个能提权的父进程。

    授权与目标命令共用一次 ``sudo -A``：授权失败不会启动目标命令，且不会出现
    「先 sudo -A -v、再 sudo -A」导致的两次密码框。
    """
    # 带 .service 后缀：停止时不必依赖 systemd 的单位名推导。
    unit = f"bibit-sudo-{os.getpid()}-{secrets.token_hex(4)}.service"
    script = 'exec sudo -A -k "$@"\n' if force_reauth else 'exec sudo -A "$@"\n'
    argv = [
        "systemd-run", "--user", "--quiet", "--collect", "--wait", "--pipe",
        "--service-type=exec", f"--unit={unit}",
        f"--setenv=SUDO_ASKPASS={env.get('SUDO_ASKPASS', '')}",
    ]
    for key in ("HOME", "LANG", "LC_ALL"):
        if env.get(key):
            argv.append(f"--setenv={key}={env[key]}")
    argv += ["/bin/sh", "-c", script, "sh", *cmd]
    try:
        p = run_command(
            argv,
            timeout=timeout,
            env=_user_manager_env(env),
            input=input,
            on_output=on_output,
        )
    except EdgeExecutionCancelled:
        _stop_user_manager_unit(unit, env)
        raise
    except FileNotFoundError:
        return ExecResult(False, "未找到 systemd-run：无法在受限服务里执行需要 root 的操作")
    except subprocess.TimeoutExpired:
        _stop_user_manager_unit(unit, env)
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")
    if p.returncode == 0:
        return ExecResult(True, "ok", p.stdout, detail_format="text")
    auth_detail = _sudo_auth_failure_detail(p.stdout, p.stderr)
    if auth_detail is not None:
        end_execution_without_side_effect()
        return ExecResult(False, "授权未完成，操作未执行", auth_detail, detail_format="text")
    return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")


def _authorize_sudo(env: dict[str, str], timeout: int | None) -> ExecResult | None:
    """只有已有授权时才免弹框；没有缓存凭据时返回 None，由调用方执行一次
    ``sudo -A <cmd>``。

    NOPASSWD 或已有时间戳时先走无害命令 ``sudo -n true`` 预检，避免无谓地
    拉起 GUI 密码框。端侧 sudo-rs 的 ``sudo -A -v`` 不会可靠缓存时间戳，
    若在这里先 ``-v`` 弹框，紧接着的目标命令会再弹一次密码框；因此这里不再
    主动弹框，改为让目标命令自己完成「授权 + 执行」。授权失败由
    ``_sudo_auth_failure_detail`` 从 stderr 判定，并同样撤回执行标记。
    """
    try:
        cached = run_command(
            ["sudo", "-n", "true"],
            timeout=5 if timeout is None else min(5, max(1, timeout)),
            env=env,
        )
    except EdgeExecutionCancelled:
        end_execution_without_side_effect()
        return ExecResult(False, "已取消：尚未完成授权，操作未执行")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        cached = None
    return None


def _run_sudo(cmd: list[str], timeout: int | None = 60, input: str | None = None, on_output=None, *, force_reauth: bool = False) -> ExecResult:
    """以 sudo -A 提权执行：SUDO_ASKPASS 指向 ssh-askpass，弹出 GUI 密码框，
    由用户在桌面输入密码，代码中不保存密码。"""
    # 无可用图形会话（agent 服务常驻 systemd、SSH 远程等）时不要启动 Qt 版
    # ssh-askpass：连不上 X/Wayland 会打一串 qt.qpa 报错且授权框弹不出来。
    # 返回明确错误并升级云端，由有桌面的入口弹自己的授权框/提示用户。
    env = _session_askpass_env()
    if env is None:
        return ExecResult(
            False,
            "当前没有可用的图形登录会话，无法弹出授权框执行需要 root 权限的"
            "操作；请在桌面会话（终端/控制中心）中重试",
            escalate="no_gui_askpass",
        )
    try:
        env.setdefault("SUDO_ASKPASS", "/usr/bin/ssh-askpass")
        if not os.path.exists(env["SUDO_ASKPASS"]):
            return ExecResult(
                False,
                "未找到 sudo 授权程序 ssh-askpass：请先安装 lxqt-openssh-askpass"
                "或其它 ssh-askpass 实现",
                escalate="no_gui_askpass",
            )
        # sudo 会清空 askpass 子进程的环境，主题/显示变量到不了授权框；
        # 用包装脚本恢复会话环境后再 exec 真 askpass（失败则退回裸调用）。
        real_askpass = os.path.realpath(env["SUDO_ASKPASS"])
        wrapper = _install_askpass_wrapper(env, real_askpass)
        if wrapper:
            env["SUDO_ASKPASS"] = wrapper
        host_package_command = bool(cmd) and Path(cmd[0]).name in {"apt", "apt-get", "dpkg"}
        if host_package_command:
            cmd = [
                "/usr/bin/env", "TERM=dumb", "DEBIAN_FRONTEND=noninteractive",
                *cmd,
            ]
        # 带 no_new_privs 的进程（Runtime Worker 拉起的 MCP 子进程）里 sudo
        # 永远提不了权；或者 MCP 继承的 mount namespace 把 /usr 绑成只读，
        # root 也无法写入。这两种情况都改由用户管理器的 transient unit 在
        # host mount namespace 中执行同一套授权 + 命令。
        if host_package_command or _no_new_privs_active() or _usr_mount_read_only():
            return _run_sudo_via_user_manager(cmd, timeout, env, input=input, on_output=on_output, force_reauth=force_reauth)
        denied = None if force_reauth else _authorize_sudo(env, timeout)
        if denied is not None:
            return denied
        p = run_command(
            ["sudo", "-A"] + (["-k"] if force_reauth else []) + cmd,
            timeout=timeout,
            env=env,
            input=input,
            on_output=on_output,
        )
        if p.returncode == 0:
            return ExecResult(True, "ok", p.stdout, detail_format="text")
        auth_detail = _sudo_auth_failure_detail(p.stdout, p.stderr)
        if auth_detail is not None:
            end_execution_without_side_effect()
            return ExecResult(False, "授权未完成，操作未执行", auth_detail, detail_format="text")
        return ExecResult(False, f"命令失败(码{p.returncode})", p.stderr, detail_format="text")
    except FileNotFoundError:
        return ExecResult(False, "未安装 sudo 或 ssh-askpass：请先安装 sudo 与 openssh-client")
    except subprocess.TimeoutExpired:
        return ExecResult(False, f"命令超时：{' '.join(cmd)}")


def _is_installed(pkg: str) -> bool:
    """单包状态查询：dpkg-query -W 输出 ii（install ok installed）才算已装。

    相比全量 apt list --installed，每次只查一个包，包一多也不拖慢判断。
    """
    try:
        p = run_command(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", pkg], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and p.stdout.strip() in {"ii", "hi"}


def _installed_version(pkg: str) -> str:
    """返回已安装包版本号（dpkg-query -W）；未安装/查询失败返回空串。"""
    try:
        p = run_command(["dpkg-query", "-W", "-f=${Version}", pkg], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _dpkg_version_relation(candidate: str, installed: str) -> str | None:
    """比较两个 Debian 版本，返回 eq/lt/gt；比较不可用返回 None。"""
    try:
        for relation in ("eq", "lt", "gt"):
            result = run_command(
                ["dpkg", "--compare-versions", candidate, relation, installed],
                timeout=15,
            )
            if result.returncode not in (0, 1):
                return None
            if result.returncode == 0:
                return relation
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return None


_PKG_NOT_LOCATABLE_HINTS = (
    "无法定位",
    "没有发现匹配",
    "找不到软件包",
    "没有找到软件包",
    "unable to locate",
    "no packages found",
    "no package found",
)


def _apt_not_located(detail: str) -> bool:
    """apt/dpkg 失败是否为“包不存在”，而非其它系统错误。"""
    low = (detail or "").lower()
    return any(hint.lower() in low for hint in _PKG_NOT_LOCATABLE_HINTS)


def _pkg_near_miss_reply(pkg: str, verb: str, detail: str) -> ExecResult | None:
    """装/卸尝试失败且包确实不存在时的相近候选提示（最后一步兜底）。

    只在 apt 报“找不到包”之后调用：给出相近候选，**绝不自动改名安装**；
    没有候选时返回 None，沿用原始失败信息。走云端时这是云端调用该工具失败后
    能拿到的最后答复，云端会把它转达给用户。
    """
    suggestions = suggest_install_packages(pkg) or []
    if not suggestions:
        return None
    return ExecResult(
        False,
        f"没有找到软件包“{pkg}”。相近的软件包：{'、'.join(suggestions)}；"
        f"如需{verb}请使用完整准确的包名，或让我先搜索确认",
        detail,
        detail_format="text",
    )


def _clip(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(截断)"


# ── 软件安装升级 ─────────────────────────────────────────────────────

def _installation_failure(result: ExecResult, *, preflight: bool = False) -> ExecResult:
    if result.ok or result.escalate:
        return result
    diagnostic = (result.detail or result.message).lower()
    summary = ""
    if any(marker in diagnostic for marker in (
        "no space left on device", "don't have enough free space",
        "设备上没有空间", "磁盘空间不足",
    )):
        summary = "存储空间不足。请先查看磁盘占用，再决定是否清理文件。"
    elif (
        ("lock" in diagnostic or "锁" in diagnostic)
        and any(marker in diagnostic for marker in (
            "held by process", "locked by another process", "resource temporarily unavailable",
            "another process using it", "持有", "另一个进程",
        ))
    ):
        summary = "包管理器正被其他进程占用。请等待该操作结束，不要删除锁文件或强行终止进程。"
    elif "without removal" in diagnostic or "--no-remove" in diagnostic:
        summary = "该安装计划需要移除其他软件，已阻止自动执行；请先查看变更清单并单独确认移除方案。"
    elif any(marker in diagnostic for marker in (
        "unmet dependencies", "unmet deps", "dependency problems", "broken packages",
        "未满足的依赖", "依赖关系无法满足",
    )):
        summary = "依赖无法满足。请查看依赖详情，确认处理方案后再重试，不要直接强制安装。"
    if not summary:
        return result
    status = "预检失败，未执行安装。" if preflight else "安装未完成，可能已有部分变更；请核验软件包状态后再重试。"
    return ExecResult(False, status + summary, result.detail, detail_format=result.detail_format)


def _dpkg_repair_plan() -> tuple[ExecResult | None, list[str] | None]:
    """安装前检查 dpkg 数据库，返回 (需中止的错误, 需预先执行的修复命令)。

    dpkg 被中断（进程被杀、超时、掉电）常见结果是有包已解包但未配置；这类状态
    可用 ``dpkg --configure -a`` 确定性补齐，不会新增或删除软件。为避免修复和
    安装各弹一次密码框，这里不立即执行修复，而是把它作为命令序列的第一条返回，
    由调用方与 apt/dpkg 安装放进同一个 ``sudo -A`` 会话。
    """
    audit = package_state.query_dpkg_audit()
    if audit.status == "unknown":
        return ExecResult(
            False,
            "无法检查 dpkg 软件包状态，未执行操作；请先检查 dpkg 状态数据库",
            audit.detail,
            detail_format="text",
        ), None
    if audit.status != "interrupted":
        return None, None
    # reinstreq/half-installed 不能靠 dpkg --configure -a 修复，必须重新下载并
    # 安装对应包；否则 set -e 会在 apt 安装前就失败。
    if audit.reinstreq:
        return None, [
            "apt-get", "install", "-y", "--no-remove", "--reinstall", "--",
            *audit.reinstreq,
        ]
    return None, ["dpkg", "--configure", "-a"]


def _dpkg_repair_audit() -> ExecResult | None:
    """修复命令执行后复查 dpkg；仍有严重异常包时阻止继续并给出处理建议。"""
    after = package_state.query_dpkg_audit()
    if after.status == "unknown":
        return ExecResult(
            False,
            "dpkg 修复后无法复查软件包状态，本次操作未执行；请运行 dpkg --audit 确认",
            after.detail,
            detail_format="text",
        )
    if after.status == "interrupted":
        return ExecResult(
            False,
            "修复命令已执行，但 dpkg 仍存在未修复的严重异常包；"
            "本次操作未完成。请先处理这些包（通常需要重新安装或显式移除），"
            "例如：apt-get install --reinstall <包名> 或 dpkg --remove "
            "--force-remove-reinstreq <包名>",
            after.detail,
            detail_format="text",
        )
    return None


def _apt_dpkg_in_flight() -> str | None:
    """检测是否已有 apt/dpkg 安装任务在运行，返回占用描述；无则返回 None。

    安装/卸载经由 ``_run_sudo_via_user_manager`` 以 ``bibit-sudo-*.service``
    用户 transient unit 执行；系统更新走 ``bibit-system-update.service`` 系统
    unit。边端 HTTP 与 MCP 是两个独立进程，超时回退后可能重复触发同一安装，
    这里用 unit 状态 + 进程扫描做跨进程守卫，避免第二次重复拉起 apt/dpkg。
    """
    env = _user_manager_env(dict(os.environ))
    try:
        p = run_command(
            ["systemctl", "--user", "list-units", "--type=service",
             "--no-legend", "--no-pager", "--state=active", "bibit-sudo-*"],
            timeout=10,
            env=env,
        )
        for line in p.stdout.splitlines():
            unit = line.split(None, 1)[0] if line.strip() else ""
            if unit.startswith("bibit-sudo-"):
                return f"安装任务 {unit} 仍在运行"
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        p = run_command(
            ["systemctl", "list-units", "--no-legend", "--no-pager",
             "--state=active", "bibit-system-update.service", "bibit-system-update.timer"],
            timeout=10,
        )
        if p.stdout.strip():
            return "系统更新任务仍在运行"
    except (OSError, subprocess.TimeoutExpired):
        pass
    for proc in ("apt-get", "apt", "dpkg"):
        try:
            p = run_command(["pgrep", "-x", proc], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if p.returncode == 0 and p.stdout.strip():
            return f"检测到 {proc} 正在运行，可能已有安装任务在执行"
    return None


def pkg_install(pkg: str = "", allow_downgrade: bool = False, **_) -> ExecResult:
    """安装软件：apt-get install -y pkg1 [pkg2 ...]。

    支持 ``name=version`` 的显式目标版本；显式版本低于已装版本时默认拦截，
    用户确认或 ``allow_downgrade=true`` 后才追加 ``--allow-downgrades``。
    """
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    specs = split_exact_install_specs(pkg)
    if specs is None:
        return ExecResult(False, "安装仅支持一个或多个完整准确的包名，可带 name=version；正则、通配符或 APT 操作后缀需先确认清单，未执行安装")
    allow = _as_bool(allow_downgrade)

    installed: list[str] = []
    pending: list[tuple[str, str, str | None]] = []
    incomplete: list[str] = []
    downgrade_notes: list[str] = []
    for name, requested_version in specs:
        state = package_state.query_package_state(name)
        if state.status == "installed":
            if requested_version is None:
                held = "，已锁定版本（hold）" if state.held else ""
                installed.append(f"{name}（版本 {state.version}{held}）")
                continue
            relation = _dpkg_version_relation(requested_version, state.version)
            if relation is None:
                return ExecResult(False, f"无法比较 {name} 的版本关系，未执行安装")
            if relation == "eq":
                held = "，已锁定版本（hold）" if state.held else ""
                installed.append(f"{name}（版本 {state.version}{held}）已安装相同版本，无需重复安装")
                continue
            if relation == "lt" and not allow:
                confirmed = _downgrade_dialog(name, state.version, requested_version)
                if confirmed is True:
                    downgrade_notes.append(f"{name}：{state.version} → {requested_version}")
                elif confirmed is False:
                    return ExecResult(False, "已取消降级")
                else:
                    return ExecResult(
                        False,
                        _downgrade_prompt(name, state.version, requested_version),
                        need_param="allow_downgrade",
                        confirm=True,
                    )
            elif relation == "gt" and state.held:
                return ExecResult(False, f"{name} 已锁定版本 {state.version}（hold），未执行升级到 {requested_version}")
            elif relation == "lt":
                downgrade_notes.append(f"{name}：{state.version} → {requested_version}")
            pending.append((name, f"{name}={requested_version}", requested_version))
            continue
        if state.status == "unknown":
            return ExecResult(False, f"{name} 状态查询失败，未执行安装；请先检查软件包状态", state.detail)
        if requested_version is not None:
            target = f"{name}={requested_version}"
        else:
            target = apt_install_target(name)
            if target is None:
                return ExecResult(False, f"无法核实 {name} 的候选版本，未执行安装；请确认包名，不要使用 APT 的 + / - 操作后缀")
        pending.append((name, target, requested_version))
        if state.status == "incomplete":
            incomplete.append(name)

    if not pending:
        message = "；".join(installed) + " 已安装，无需重复安装"
        if downgrade_notes:
            message = "；".join(downgrade_notes) + "；" + message
        return ExecResult(True, message)

    busy = _apt_dpkg_in_flight()
    if busy is not None:
        return ExecResult(
            True,
            f"{busy}。为避免重复执行，本次未再启动新的安装；请等待其结束后再查询结果。",
        )

    preflight, repair = _dpkg_repair_plan()
    if preflight is not None:
        return preflight

    apt_flags = ["install", "-y", "--no-remove"]
    if allow:
        apt_flags.append("--allow-downgrades")
    if incomplete:
        apt_flags.append("--fix-broken")
    install_progress.report(
        0,
        f"正在安装：{'、'.join(name for name, _, _ in pending)}；"
        "大型应用下载和配置可能需要较长时间，安装过程中请勿中断或关闭应用",
        phase="preparing",
    )
    apt_command = ["apt-get", *apt_flags, "--", *[target for _, target, _ in pending]]
    if repair:
        r = _run_sudo(
            ["/bin/sh", "-c", _shell_sequence([repair, apt_command]), "sh"],
            timeout=_APT_INSTALL_TIMEOUT,
            on_output=install_progress.observe_output,
        )
    else:
        r = _run_sudo(
            apt_command,
            timeout=_APT_INSTALL_TIMEOUT,
            on_output=install_progress.observe_output,
        )
    if not r.ok:
        if len(pending) == 1 and _apt_not_located(r.detail or r.message):
            near = _pkg_near_miss_reply(pending[0][0], "安装", r.detail)
            if near is not None:
                return near
        return _installation_failure(r)

    if repair:
        repaired = _dpkg_repair_audit()
        if repaired is not None:
            return repaired

    failed: list[str] = []
    for name, _, requested_version in pending:
        if requested_version is not None:
            version = _installed_version(name)
            if not version or _dpkg_version_relation(version, requested_version) != "eq":
                failed.append(f"{name}={requested_version}")
        elif not _is_installed(name):
            failed.append(name)
    if failed:
        return ExecResult(
            False,
            f"apt 安装命令已结束，但未检测到以下软件包安装成功：{'、'.join(failed)}",
            r.detail,
            detail_format=r.detail_format,
        )
    installed_parts: list[str] = []
    for name, _, requested_version in pending:
        version = _installed_version(name)
        installed_parts.append(f"{name}（版本 {version}）" if version else name)
    installed_names = "、".join(installed_parts)
    message = f"{installed_names} 安装完成"
    if downgrade_notes:
        message += "；" + "；".join(downgrade_notes)
    if incomplete:
        message += "，已恢复半安装状态"
    if installed:
        message += "；" + "；".join(installed)
    install_progress.report(100, message, phase="done")
    return ExecResult(True, message, r.detail, detail_format=r.detail_format)


def pkg_remove(pkg: str = "", **_) -> ExecResult:
    """卸载软件：apt-get remove -y pkg1 [pkg2 ...]"""
    if not pkg:
        return ExecResult(False, "缺少软件包名")
    targets = split_exact_packages(pkg)
    if targets is None:
        return ExecResult(False, "卸载仅支持一个或多个完整准确的包名；正则、通配符或 APT 操作后缀需先确认清单，未执行卸载")

    installed_names: list[str] = []
    remove_targets: list[str] = []
    incomplete: list[str] = []
    absent: list[str] = []
    for name in targets:
        state = package_state.query_package_state(name)
        if state.status in {"absent", "config_only"}:
            absent.append(name)
            continue
        if state.status == "incomplete":
            incomplete.append(name)
            continue
        if state.status == "unknown":
            return ExecResult(False, f"{name} 状态查询失败，未执行卸载；请先检查软件包状态", state.detail)
        target = name
        if has_apt_action_suffix(name):
            version = state.version
            if not version:
                return ExecResult(False, f"无法核实 {name} 的已安装版本，未执行卸载")
            target = f"{name}={version}"
        installed_names.append(name)
        remove_targets.append(target)

    if not remove_targets and not incomplete:
        return ExecResult(True, "、".join(absent) + " 未安装（已卸载），无需卸载")

    busy = _apt_dpkg_in_flight()
    if busy is not None:
        return ExecResult(
            True,
            f"{busy}。为避免重复执行，本次未再启动新的卸载；请等待其结束后再查询结果。",
        )

    commands: list[list[str]] = []
    if remove_targets:
        commands.append(["apt-get", "remove", "-y", "--", *remove_targets])
    if incomplete:
        commands.append(["dpkg", "--remove", "--force-remove-reinstreq", "--", *incomplete])
    details: list[str] = []
    messages: list[str] = []
    for index, command in enumerate(commands):
        r = _run_sudo(command, timeout=600)
        if r.detail:
            details.append(r.detail)
        if not r.ok:
            messages.append(r.message)
            if index + 1 < len(commands):
                messages.append("后续卸载步骤未执行")
            if len(targets) == 1 and _apt_not_located(r.detail or r.message):
                near = _pkg_near_miss_reply(targets[0], "卸载", r.detail)
                if near is not None:
                    messages.append(near.message)
            break

    removed: list[str] = []
    remaining: list[str] = []
    broken: list[str] = []
    unknown: list[str] = []
    for name in [*installed_names, *incomplete]:
        state = package_state.query_package_state(name)
        if state.status in {"absent", "config_only"}:
            removed.append(name)
        elif state.status == "installed":
            remaining.append(name)
        elif state.status == "incomplete":
            broken.append(name)
        else:
            unknown.append(name)
        if state.detail:
            details.append(f"{name}: {state.detail}")
    if removed:
        messages.append("、".join(removed) + " 已卸载")
    if remaining:
        messages.append("仍处于已安装状态：" + "、".join(remaining))
    if broken:
        messages.append("仍处于异常状态，未完成卸载：" + "、".join(broken))
    if unknown:
        messages.append("状态查询失败，无法确认卸载结果：" + "、".join(unknown))
    if absent:
        messages.append("、".join(absent) + " 原本未安装")
    return ExecResult(
        r.ok and not (remaining or broken or unknown),
        "；".join(messages),
        "\n".join(details),
        escalate=r.escalate,
        detail_format="text",
    )


# ── 生态软件安装（pip / npm / 本地安装）──────────────────────────────
# 与 apt 工具平级拆分：pip/npm 默认用户态安装（venv / ~/.local），规避
# PEP 668；系统级（--global / dpkg）必须走 sudo + 二次确认。所有命令均为
# 参数化列表执行，杜绝 shell 注入。

_PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9_@][A-Za-z0-9_\-\[\].=@]*")
# npm 作用域包（@scope/name）；除此之外包名里不允许出现 "/"。
_SCOPED_NPM_NAME_RE = re.compile(r"@[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_PIP_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?:\[[^\]]*\])?(?:==(?P<version>[A-Za-z0-9_.-]+))?$"
)
_NPM_TARBALL_VERSION_RE = re.compile(r"^(?P<name>.+)-(?P<version>v?\d[0-9A-Za-z.+-]*)$")


def _pip_package_spec(pkg: str) -> tuple[str, str | None]:
    """从 pip 请求中解析 (包名, 明确版本)；无明确版本返回 (name, None)。

    支持本地 .whl 文件名（wheel 的发行名和版本由 `-` 分隔）和 `name==version`
    形式；`latest` 等未指定版本、本地非 wheel 或不可解析时不返回版本，由调用方
    按「无降级信息」继续原流程，不做误伤。
    """
    raw = (pkg or "").strip()
    path = Path(raw)
    if raw.startswith("/") and raw.lower().endswith(".whl"):
        parts = path.name[:-4].split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[0].replace("_", "-").lower(), parts[1]
    match = _PIP_SPEC_RE.fullmatch(raw)
    if match is None:
        return "", None
    return match.group("name").lower().replace("_", "-"), match.group("version")


def _npm_package_spec(pkg: str) -> tuple[str, str | None]:
    """从 npm 请求中解析 (包名, 明确版本)；无明确版本返回 (name, None)。"""
    raw = (pkg or "").strip()
    path = Path(raw)
    if raw.startswith("/"):
        low = raw.lower()
        for suffix in (".tgz", ".tar.gz"):
            if low.endswith(suffix):
                match = _NPM_TARBALL_VERSION_RE.fullmatch(path.name[: -len(suffix)])
                if match is not None:
                    return match.group("name").lower(), match.group("version")
                return path.name[: -len(suffix)].lower(), None
    if raw.startswith("@") and "/" in raw:
        parts = raw.split("@")
        if len(parts) == 3 and parts[2]:
            return f"@{parts[1]}", parts[2]
        return raw, None
    if "@" in raw:
        name, version = raw.rsplit("@", 1)
        return name.lower(), version or None
    return raw.lower(), None


def _is_exact_version(value: str | None) -> bool:
    return bool(value) and re.match(r"^(?:v)?\d", value) is not None


def _npm_installed_version(name: str, target: str = "", global_install: bool = False) -> str | None:
    """查询 npm 已装版本；查询失败或未安装返回 None，不阻塞安装。"""
    if target:
        command = ["npm", "list", "--prefix", target, "--json", "--depth=0"]
    elif global_install:
        command = ["npm", "list", "-g", "--json", "--depth=0"]
    else:
        command = ["npm", "list", "-g", "--prefix", str(Path.home() / ".local"), "--json", "--depth=0"]
    try:
        result = run_command(command, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        dependencies = json.loads(result.stdout or "{}").get("dependencies") or {}
    except (ValueError, TypeError):
        return None
    node = dependencies.get(name)
    return node.get("version") if isinstance(node, dict) else None


def _pip_installed_version(python: str, name: str, target: str = "") -> str | None:
    """查询 pip 已装版本；查询失败或未安装返回 None，不阻塞安装。"""
    try:
        if target:
            result = run_command(
                [python, "-m", "pip", "list", "--path", target, "--format=json"],
                timeout=60,
            )
            if result.returncode != 0:
                return None
            try:
                items = json.loads(result.stdout or "[]")
            except (ValueError, TypeError):
                return None
            for item in items:
                if str(item.get("name") or "").lower().replace("_", "-") == name.lower().replace("_", "-"):
                    return item.get("version")
            return None
        result = run_command([python, "-m", "pip", "show", name], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def _pip_compare_versions(python: str, candidate: str, installed: str) -> str | None:
    """用目标解释器随 pip 自带的 packaging 比较 PEP 440 版本。

    生产 venv 不直接安装顶层 packaging，但 pip._vendor.packaging 始终可用；
    解析或比较失败返回 None，调用方按「无法判断」继续，不做误伤。
    """
    script = (
        "import sys\n"
        "try:\n"
        " from pip._vendor.packaging.version import Version, InvalidVersion\n"
        "except Exception:\n"
        " raise SystemExit(3)\n"
        "try:\n"
        " a = Version(sys.argv[1]); b = Version(sys.argv[2])\n"
        "except (InvalidVersion, Exception):\n"
        " raise SystemExit(2)\n"
        "print('lt' if a < b else 'eq' if a == b else 'gt')"
    )
    try:
        result = run_command([python, "-c", script, candidate, installed], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    relation = result.stdout.strip()
    return relation if relation in {"lt", "eq", "gt"} else None


def _semver_relation(candidate: str, installed: str) -> str | None:
    """按 Node SemVer 优先级比较两个版本；返回 lt/eq/gt，不可解析返回 None。"""

    def parse(value: str) -> tuple[list[int], list[tuple[int, int | str]]] | None:
        text = value.strip().lstrip("vV")
        if "+" in text:
            text = text.split("+", 1)[0]
        prerelease: list[tuple[int, int | str]] = []
        if "-" in text:
            base, pre = text.split("-", 1)
            for part in pre.split("."):
                if part.isdigit():
                    prerelease.append((0, int(part)))
                elif part:
                    prerelease.append((1, part.lower()))
                else:
                    return None
        else:
            base = text
        release: list[int] = []
        for part in base.split("."):
            if part.isdigit():
                release.append(int(part))
            else:
                return None
        if not release:
            return None
        return release, prerelease

    left = parse(candidate)
    right = parse(installed)
    if left is None or right is None:
        return None
    release_len = max(len(left[0]), len(right[0]))
    for index in range(release_len):
        left_part = left[0][index] if index < len(left[0]) else 0
        right_part = right[0][index] if index < len(right[0]) else 0
        if left_part != right_part:
            return "lt" if left_part < right_part else "gt"
    if left[1] == right[1]:
        return "eq"
    if not left[1]:
        return "gt"
    if not right[1]:
        return "lt"
    for index in range(max(len(left[1]), len(right[1]))):
        if index >= len(left[1]):
            return "lt"
        if index >= len(right[1]):
            return "gt"
        left_kind, left_item = left[1][index]
        right_kind, right_item = right[1][index]
        if left_kind != right_kind:
            return "lt" if left_kind < right_kind else "gt"
        if left_item != right_item:
            return "lt" if left_item < right_item else "gt"
    return "eq"


def _downgrade_dialog(package: str, installed: str, target: str) -> bool | None:
    """桌面弹窗询问是否允许降级；无桌面会话返回 None，交给自然语言确认。"""
    session = _session_askpass_env()
    if session is None:
        return None
    env = dict(os.environ)
    env.update(session)
    text = f"{package} 当前已安装 {installed}，请求安装 {target}。\n是否允许降级？"
    try:
        result = subprocess.run(
            [
                "zenity", "--question", "--title=允许降级？",
                "--text=" + text, "--ok-label=允许降级", "--cancel-label=取消",
                "--timeout=120",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=130,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _downgrade_prompt(package: str, installed: str, target: str) -> str:
    return (
        f"检测到降级：{package} 当前版本 {installed}，请求安装 {target}。"
        "回复「允许降级」继续，或回复「取消」；未确认前不会安装。"
    )


def _pip_target_python(target: str) -> str:
    """target 是 venv 时返回其 python，否则返回空串（按 --target 目录处理）。"""
    py = Path(target) / "bin" / "python"
    return str(py) if py.is_file() else ""


def _pip_downgrade_preflight(
    pkg: str,
    target: str = "",
    allow_downgrade: bool = False,
    python: str = "",
) -> ExecResult | None:
    """pip 安装前检测明确版本是否低于已装版本；是则拒绝并提示降级。"""
    name, version = _pip_package_spec(pkg)
    if not name or not version or not _is_exact_version(version):
        return None
    explicit_python = bool(python)
    if python:
        pass
    elif target:
        python = sys.executable
    else:
        base = _user_pip()
        if base is None:
            return ExecResult(False, "无法准备用户 Python 环境（venv 创建失败）")
        python = base[0]
    query_target = "" if explicit_python else target
    installed = _pip_installed_version(python, name, query_target)
    if not installed:
        return None
    relation = _pip_compare_versions(python, version, installed)
    if relation == "lt":
        if allow_downgrade:
            return None
        confirmed = _downgrade_dialog(name, installed, version)
        if confirmed is True:
            return None
        if confirmed is False:
            return ExecResult(False, "已取消降级")
        return ExecResult(
            False,
            _downgrade_prompt(name, installed, version),
            need_param="allow_downgrade",
            confirm=True,
        )
    return None


def _npm_downgrade_preflight(pkg: str, target: str = "", global_install: bool = False, allow_downgrade: bool = False) -> ExecResult | None:
    """npm 安装前检测明确版本是否低于已装版本；是则拒绝并提示降级。"""
    name, version = _npm_package_spec(pkg)
    if not name or not version or not _is_exact_version(version):
        return None
    installed = _npm_installed_version(name, target, global_install)
    if not installed:
        return None
    relation = _semver_relation(version, installed)
    if relation == "lt":
        if allow_downgrade:
            return None
        confirmed = _downgrade_dialog(name, installed, version)
        if confirmed is True:
            return None
        if confirmed is False:
            return ExecResult(False, "已取消降级")
        return ExecResult(
            False,
            _downgrade_prompt(name, installed, version),
            need_param="allow_downgrade",
            confirm=True,
        )
    return None


def _safe_package(name: str) -> str:
    """校验 pip/npm 包名，非法/超长返回空串。

    包名里只允许出现一个 `/`，且仅限 npm 作用域形式 `@scope/name`：否则
    `a/../../tmp/evil` 这类“包名”会被 pip/npm 当作本地路径解析，装到不该装
    的目录。本地 wheel/deb/tar 包由 install_local 走绝对路径分支，不走这里。
    """
    raw = (name or "").strip()
    if not raw or len(raw) > 300:
        return ""
    if _PACKAGE_NAME_RE.fullmatch(raw) or _SCOPED_NPM_NAME_RE.fullmatch(raw):
        return raw
    return ""


_USER_PYVENV: str | None = None


def _user_pip() -> list[str] | None:
    """返回用户级 pip 命令前缀（共享 venv，规避 PEP 668）；失败返回 None。"""
    global _USER_PYVENV
    if _USER_PYVENV is None:
        venv_dir = Path.home() / ".local" / "share" / "bibit-intent" / "pyvenv"
        python = venv_dir / "bin" / "python"
        if not python.exists():
            r = _run([sys.executable, "-m", "venv", str(venv_dir)], timeout=180)
            if not r.ok:
                return None
        _USER_PYVENV = str(python)
    return [_USER_PYVENV, "-m", "pip"]


def _pip_index_args() -> list[str]:
    """pip 安装统一走 SpacemiT PyPI 镜像（GitLab Package Registry simple index）。

    端侧从内部镜像拉包更快且与 Bianbu/riscv64 对齐；只附加在安装命令上，
    卸载等其它命令不携带镜像参数。
    """
    return ["-i", _PIP_INDEX_URL]


def pip_install(pkg: str = "", target: str = "", allow_downgrade: bool = False, **_) -> ExecResult:
    """安装 Python 包（PyPI，固定走 SpacemiT 内部镜像）：默认用户共享 venv；
    target 指定 venv/项目目录。"""
    pkg = (pkg or "").strip()
    if not pkg:
        return ExecResult(False, "缺少 Python 包名（pip install <包名>）")
    if not (_safe_package(pkg) or (pkg.startswith("/") and Path(pkg).is_file())):
        return ExecResult(False, f"无效的 Python 包名: {pkg}")
    allow = _as_bool(allow_downgrade)
    if target:
        t = str(target).strip()
        if not Path(t).is_dir():
            return ExecResult(False, f"目标目录不存在: {t}")
        venv_python = _pip_target_python(t)
        preflight = _pip_downgrade_preflight(
            pkg, "" if venv_python else t, allow, python=venv_python
        )
        if preflight is not None:
            return preflight
        if venv_python:
            return _run(
                [venv_python, "-m", "pip", "install", *_pip_index_args(), pkg],
                timeout=600,
            )
        return _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *_pip_index_args(),
                "--target",
                t,
                pkg,
            ],
            timeout=600,
        )
    preflight = _pip_downgrade_preflight(pkg, "", allow)
    if preflight is not None:
        return preflight
    base = _user_pip()
    if base is None:
        return ExecResult(False, "无法准备用户 Python 环境（venv 创建失败）")
    return _run(base + ["install", *_pip_index_args(), pkg], timeout=600)


def pip_uninstall(pkg: str = "", target: str = "", **_) -> ExecResult:
    """卸载 Python 包：默认用户共享 venv；target 为 venv 时在对应环境卸载。"""
    pkg = _safe_package(pkg)
    if not pkg:
        return ExecResult(False, "缺少 Python 包名（pip uninstall <包名>）")
    if target:
        py = Path(str(target).strip()) / "bin" / "python"
        if not py.is_file():
            return ExecResult(False, f"卸载仅支持 venv 目标目录或用户环境: {target}")
        return _run([str(py), "-m", "pip", "uninstall", "-y", pkg], timeout=300)
    base = _user_pip()
    if base is None:
        return ExecResult(False, "无法准备用户 Python 环境（venv 创建失败）")
    return _run(base + ["uninstall", "-y", pkg], timeout=300)


def npm_install(pkg: str = "", target: str = "", allow_downgrade: bool = False, **kwargs) -> ExecResult:
    """安装 Node 包（npm registry）：默认用户级 ~/.local；global 系统级 sudo。"""
    pkg = (pkg or "").strip()
    if not pkg:
        return ExecResult(False, "缺少 Node 包名（npm install <包名>）")
    if not (_safe_package(pkg) or (pkg.startswith("/") and Path(pkg).is_file())):
        return ExecResult(False, f"无效的 Node 包名: {pkg}")
    allow = _as_bool(allow_downgrade)
    if target:
        t = str(target).strip()
        if not Path(t).is_dir():
            return ExecResult(False, f"目标目录不存在: {t}")
        preflight = _npm_downgrade_preflight(
            pkg, t, bool(kwargs.get("global_install", False)), allow
        )
        if preflight is not None:
            return preflight
        return _run(["npm", "install", "--prefix", t, "--no-save", pkg], timeout=600)
    preflight = _npm_downgrade_preflight(
        pkg, "", bool(kwargs.get("global_install", False)), allow
    )
    if preflight is not None:
        return preflight
    if bool(kwargs.get("global_install", False)):
        return _run_sudo(["npm", "install", "-g", pkg], timeout=600)
    prefix = str(Path.home() / ".local")
    return _run(["npm", "install", "-g", "--prefix", prefix, pkg], timeout=600)


def npm_uninstall(pkg: str = "", target: str = "", **kwargs) -> ExecResult:
    """卸载 Node 包：默认用户级 ~/.local；global 系统级 sudo。"""
    pkg = (pkg or "").strip()
    if not pkg:
        return ExecResult(False, "缺少 Node 包名（npm uninstall <包名>）")
    if not (_safe_package(pkg) or (pkg.startswith("/") and Path(pkg).is_file())):
        return ExecResult(False, f"无效的 Node 包名: {pkg}")
    if target:
        t = str(target).strip()
        if not Path(t).is_dir():
            return ExecResult(False, f"目标目录不存在: {t}")
        return _run(["npm", "uninstall", "--prefix", t, pkg], timeout=300)
    if bool(kwargs.get("global_install", False)):
        return _run_sudo(["npm", "uninstall", "-g", pkg], timeout=300)
    prefix = str(Path.home() / ".local")
    return _run(["npm", "uninstall", "-g", "--prefix", prefix, pkg], timeout=300)


def _clean_dir_input(value: str) -> str:
    """清理目录参数的日常口语（“在 ~/Downloads 目录”“放到下载文件夹里”），
    还原成可拼接的路径。

    去掉头尾填充词后按目录解析；失败会有明确提示，不强行猜测。
    """
    v = (value or "").strip().strip("“”\"'` 　")
    if not v:
        return ""
    m = re.search(r"^(?:在|位于|放到|放在|文件(?:就)?在|路径是|目录是|是)\s*", v)
    if m:
        v = v[m.end():].strip()
    for tail in ("目录", "文件夹", "路径", "下", "里"):
        if v.endswith(tail):
            v = v[: -len(tail)].strip()
            break
    return v


def _dir_base_candidates(base: str) -> list[Path]:
    """把用户/模型给的目录参数还原成“真实存在”的候选路径。

    云端模型二次解析时可能把句尾的“目录”当成目录名一起传入
    （如 /home/bianbu/workspace/目录），或带上“里/下/在”等口语词；
    这里按“先原样、再去尾词、再去分隔符”的顺序尝试，命中存在的路径为止。
    不存在的路径不会误伤真实存在的“目录”同名文件夹（先尝试原样）。
    """
    raws: list[str] = []

    def add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in raws:
            raws.append(v)

    raw = (base or "").strip().strip("“”\"'` 　")
    add(raw)
    add(_clean_dir_input(raw))
    for v in list(raws):
        m = re.match(r"^(.*?)(?:目录|文件夹|路径|里|下)$", v)
        if m:
            add(m.group(1))
    for v in list(raws):
        add(v.rstrip("/\\"))

    seen: list[Path] = []
    for v in raws:
        p = Path(v).expanduser()
        if p in seen:
            continue
        seen.append(p)
        if p.is_file() or p.is_dir():
            return [p]
    return []


def _file_suffix(name: str) -> str:
    """文件名后缀（含 .tar.gz）；非安装格式返回空串。"""
    low = name.lower()
    for s in (".tar.gz", ".deb", ".whl", ".tgz", ".gz"):
        if low.endswith(s):
            return s
    return ""


def _pkg_name_from_stem(name: str, suffix: str) -> str:
    """从安装文件名解析“包身份”（小写、去版本段）。

    .deb 文件名约定 `<包名>_<版本>_<架构>.deb`，包名不含下划线，取下划线
    前段；wheel 文件名 `<发行版>-<版本>…`，发行版内连字符规范为下划线，
    取第一段并按 pip 习惯把 `_` 归一到 `-`；npm tarball `<包名>-<版本>.tgz`
    （包名可含连字符），剥掉以数字开头的尾部版本段。
    """
    stem = name[: -len(suffix)] if suffix and name.endswith(suffix) else name
    if suffix == ".deb":
        return stem.partition("_")[0].lower()
    if suffix == ".whl":
        return stem.split("-", 1)[0].replace("_", "-").lower()
    m = re.search(r"^(.*)-(\d(?:[.+-][0-9A-Za-z.+-]*)?)$", stem)
    return (m.group(1) if m else stem).lower()


def _file_pkg_identity(file: Path) -> str:
    """判定安装文件归属的“包名”（小写）。

    .deb 以 dpkg-deb 控制信息为准（权威）；读不到（非有效 .deb 或没有
    dpkg-deb）退到文件名约定解析。whl/tgz 按文件名 `名-版本…` 解析。
    """
    name = file.name.lower()
    suffix = _file_suffix(name)
    if suffix == ".deb":
        pkg = _deb_package_name(file)
        if pkg:
            return pkg.lower()
    return _pkg_name_from_stem(name, suffix)


def _fuzzy_local_files(target: Path) -> tuple[list[Path], list[Path]]:
    """在 target 所在目录找“同包版本化安装文件”（同目录，只读）。

    包身份优先：.deb 以 dpkg-deb 元数据为准（读不到时按
    `<包名>_<版本>_…` 文件名解析），whl/tgz 按 `<名>-<版本>…` 文件名解析。
    只把与 target 同包名的文件当可安装候选
    （hello.deb → hello_1.0.deb、hello_2.10-5bb2_riscv64.deb）；
    helloworld_*.deb、libhello_*.deb 等“名称相近但不同包”只进 near
    列表供提示，绝不返回为候选。
    返回 (同包候选, 相近但不同包文件)。
    """
    parent = target.parent
    if not parent.is_dir():
        return [], []
    w = target.name.lower()
    suffix = _file_suffix(w)
    if suffix not in (".deb", ".whl", ".tgz", ".gz", ".tar.gz"):
        return [], []
    stem = w[: -len(suffix)] if suffix else w
    wanted = _pkg_name_from_stem(w, suffix)
    same_suffix = sorted(
        (
            q
            for q in parent.iterdir()
            if q.is_file()
            and q.name.lower() != w
            and _file_suffix(q.name.lower()) == suffix
        ),
        key=lambda q: q.name.lower(),
    )
    same_pkg: list[Path] = []
    near: list[Path] = []
    for q in same_suffix:
        if _file_pkg_identity(q) == wanted:
            same_pkg.append(q)
            continue
        low = q.name.lower()
        if low.startswith(stem) or stem in low:
            near.append(q)
    return same_pkg, near


def _resolve_local_install(path: str, dir: str = "") -> tuple[Path | None, str]:
    """把安装对象解析成本地文件。

    path 支持完整路径 / `~` / 相对文件名（相对进程当前目录）。相对文件名
    在当前目录找不到且 dir 已给时用 dir 拼接；两者都没有则返回
    提示信息（不做本地多轮回填挂起，后续由用户/云端会话正常处理）。
    精确文件不存在时做目录内“同包”匹配（hello.deb → hello_1.0.deb，
    以 dpkg-deb 元数据或文件名约定判定包身份；helloworld_*.deb 等名称
    相近但不同包不算命中）：唯一同包直接安装并回报实际文件名；多个版本
    则列出并请用户指定完整文件名；无同包时提示未找到，顺带列出目录内
    相近文件供参考，绝不自动安装不同包。
    返回 (file, err_msg)，成功时 file 非 None。
    """
    raw = (path or "").strip()
    if not raw:
        return None, "缺少安装文件路径（支持完整路径或当前目录下的文件名）"
    f = Path(raw).expanduser()
    if not f.is_absolute():
        base = (dir or "").strip()
        if base:
            base_paths = _dir_base_candidates(base)
            if base_paths and base_paths[0].is_file():
                f = base_paths[0]
            elif base_paths:
                f = base_paths[0] / f
            else:
                return None, f"目录不存在：{base}；请提供安装文件（.deb/.whl/.tgz）所在目录"
        else:
            f = Path.cwd() / f
    if f.is_file():
        return f, ""
    # 目录内近似匹配：只认“同包”版本化文件；唯一同包即用、多版本请用户
    # 选；无同包时列“名称相近但不同包”文件供参考，绝不自动装不同包。
    same_pkg, near = _fuzzy_local_files(f)
    if len(same_pkg) == 1:
        return same_pkg[0], ""
    if len(same_pkg) > 1:
        suffix = _file_suffix(f.name.lower())
        pkg = _pkg_name_from_stem(f.name.lower(), suffix)
        names = "、".join(q.name for q in same_pkg)
        return (
            None,
            f"目录 {f.parent} 中存在 {pkg} 的多个安装包版本：{names}。"
            "请确认后重新指定要安装的完整文件名",
        )
    near_names = "、".join(q.name for q in near[:8])
    near_tail = " 等" if len(near) > 8 else ""
    if Path(raw).expanduser().is_absolute():
        msg = f"文件不存在：{path}"
    elif (dir or "").strip():
        where = _clean_dir_input(dir) or dir.strip()
        msg = f"在 {where} 中没有找到 {raw}；请确认文件所在位置（或直接给完整路径）后重试"
    else:
        msg = f"当前目录 {Path.cwd()} 中没有找到 {raw}；请告知文件所在目录后重试"
    if near_names:
        msg = (
            f"{msg.rstrip('。')}。目录中有相近的安装文件：{near_names}{near_tail}；"
            "如需安装请用完整文件名重新指定（不会自动安装名称相近的其他软件包）"
        )
    return None, msg


def _as_bool(value: bool | str | int | None) -> bool:
    """bool 参数宽容解析：True/1/yes/on 为真，其余为假。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _deb_package_name(file: Path) -> str:
    """读取 .deb 控制信息里的真实包名（dpkg-deb -f，只读，不需 sudo）。"""
    try:
        p = run_command(["dpkg-deb", "-f", str(file), "Package"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _deb_depends(file: Path) -> list[list[str]]:
    """解析 .deb 的 Depends 依赖声明：按逗号分组，组内为 '|' 可选依赖。

    版本括号（如 `libc6 (>= 2.34)`）会被剥掉，只保留包名。无 Depends 或
    读取失败返回 []（失败由调用方结合安装结果兜底，不据此误报）。
    """
    try:
        p = run_command(["dpkg-deb", "-f", str(file), "Depends"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if p.returncode != 0 or not p.stdout.strip():
        return []
    raw = p.stdout.replace("\n ", " ").strip()
    groups: list[list[str]] = []
    for part in raw.split(","):
        alts = [
            alt.split("(", 1)[0].strip()
            for alt in part.split("|")
            if alt.split("(", 1)[0].strip()
        ]
        if alts:
            groups.append(alts)
    return groups


def _dep_installed(name: str) -> bool:
    """依赖是否已装：dpkg-query 状态含 ii 即满足（容忍多架构多行输出）。"""
    try:
        p = run_command(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", name], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if p.returncode != 0:
        return False
    return any(line.strip() == "ii" for line in p.stdout.splitlines())


def _deb_unsatisfied_deps(file: Path) -> list[str]:
    """纯 dpkg 模式前的依赖预检：列出 Depends 中“组内无一已装”的依赖。

    只查本机 dpkg 状态（不依赖软件源）。组内 `|` 任一已装即视为满足；
    dpkg 不会自动获取依赖，缺了就拒绝先装，避免留下半配置状态。
    """
    unsatisfied: list[str] = []
    for alts in _deb_depends(file):
        if not any(_dep_installed(a) for a in alts):
            unsatisfied.append(" | ".join(alts))
    return unsatisfied


def _apt_simulate_install(file: Path) -> tuple[bool, list[str], str]:
    """apt-get -s 预演安装本地 .deb：能否解析全部依赖、将新装哪些包。

    预演不写系统、不需 sudo；apt 会按版本约束/虚拟包/Provides 精确判断，
    比逐条 apt-cache 可靠。返回 (ok, 将新装包名, 失败原因)。
    """
    try:
        p = run_command(["apt-get", "-s", "--no-remove", "install", "--", str(file)], timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, [], "apt-get 不可用或预演超时"
    if p.returncode != 0:
        return False, [], p.stderr or p.stdout
    # “The following NEW packages will be installed: …”段落；apt 输出可能按
    # 系统语言本地化，解析不到只影响提示文案，不影响安装正确性。
    # 包列表行以空白开头，读到“0 upgraded…”等摘要行自然停止；
    # 输出按系统语言本地化时解析不到只影响提示文案。
    m = re.search(
        r"^The following NEW packages will be installed:[^\n]*\n"
        r"((?:[ \t]+[^\n]*\n?)*)",
        p.stdout,
        re.M,
    )
    new_pkgs: list[str] = []
    if m:
        new_pkgs = [
            t for t in re.split(r"\s+", m.group(1).strip()) if t and not t.startswith("(")
        ]
    return True, new_pkgs, ""


def _deb_architecture_error(file: Path) -> str:
    try:
        package = run_command(["dpkg-deb", "-f", str(file), "Architecture"], timeout=30)
        native = run_command(["dpkg", "--print-architecture"], timeout=15)
        if package.returncode or native.returncode:
            return "无法读取安装包或本机架构，未执行安装"
        package_arch = package.stdout.strip()
        native_arch = native.stdout.strip()
        if not package_arch or not native_arch:
            return "安装包或本机架构信息为空，未执行安装"
        if package_arch in {native_arch, "all"}:
            return ""
        foreign = run_command(["dpkg", "--print-foreign-architectures"], timeout=15)
        if foreign.returncode:
            return "无法检查已启用的外来架构，未执行安装"
        if package_arch in foreign.stdout.split():
            return ""
        return f"安装包架构 {package_arch} 与本机 {native_arch} 不匹配，且未启用该外来架构；未执行安装"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "架构检查工具不可用或超时，未执行安装"


def _deb_version_preflight(file: Path, pkg: str, identity: dict | None = None, allow_downgrade: bool = False) -> tuple[ExecResult | None, str]:
    try:
        metadata = run_command(["dpkg-deb", "-f", str(file), "Version", "Architecture"], timeout=30)
        fields = dict(line.split(": ", 1) for line in metadata.stdout.splitlines() if ": " in line)
        version = fields.get("Version", "").strip()
        architecture = fields.get("Architecture", "").strip()
        if metadata.returncode or not version or not architecture:
            return ExecResult(False, "无法读取安装包版本或架构，未执行安装"), ""
        target = pkg if architecture == "all" else f"{pkg}:{architecture}"
        if identity is not None:
            identity.update(package=target, version=version)
        state = package_state.query_package_state(target)
        if state.status in {"unknown", "incomplete"}:
            return ExecResult(False, f"{target} 状态未知或未配置完成，未执行安装；请先检查软件包状态", state.detail), ""
        if state.status != "installed":
            return None, ""
        for relation in ("eq", "lt", "gt"):
            compared = run_command(["dpkg", "--compare-versions", version, relation, state.version], timeout=15)
            if compared.returncode not in (0, 1):
                return ExecResult(False, "软件包版本比较失败，未执行安装"), ""
            if compared.returncode == 0:
                if relation == "eq":
                    return ExecResult(True, f"{target} 已安装相同版本 {state.version}，无需重复安装"), ""
                if relation == "lt":
                    if allow_downgrade:
                        return None, f"，版本降级：{state.version} → {version}"
                    confirmed = _downgrade_dialog(target, state.version, version)
                    if confirmed is True:
                        return None, f"，版本降级：{state.version} → {version}"
                    if confirmed is False:
                        return ExecResult(False, "已取消降级"), ""
                    return (
                        ExecResult(
                            False,
                            _downgrade_prompt(target, state.version, version),
                            need_param="allow_downgrade",
                            confirm=True,
                        ),
                        "",
                    )
                if state.held:
                    return ExecResult(False, f"{target} 已锁定版本 {state.version}（hold），未执行升级到 {version}"), ""
                return None, f"，版本升级：{state.version} → {version}"
        return ExecResult(False, "无法确定软件包版本关系，未执行安装"), ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ExecResult(False, "版本检查工具不可用或超时，未执行安装"), ""


def _install_deb(file: Path, deps: bool, allow_downgrade: bool = False) -> ExecResult:
    """安装本地 .deb：deps=True 走 apt（仓库依赖一并解决）；否则纯 dpkg。

    安装前先做只读依赖检测，不弹授权框空跑：
      - apt 模式用 `apt-get -s` 预演，依赖（含版本/虚拟包）解析不了就直接拒绝；
      - dpkg 模式逐组查 Depends 是否已装，缺依赖直接拒绝并提示改用 apt。
    两种方式装后都回读 dpkg 状态验证。
    """
    pkg = _deb_package_name(file)
    if not pkg:
        return ExecResult(
            False,
            f"无法解析软件包信息：{file.name}（不是有效的 .deb，或缺少 dpkg-deb）",
        )
    architecture_error = _deb_architecture_error(file)
    if architecture_error:
        return ExecResult(False, architecture_error)
    identity = {}
    version_result, version_note = _deb_version_preflight(file, pkg, identity, allow_downgrade)
    if version_result is not None:
        return version_result
    if not identity.get("package") or not identity.get("version"):
        return ExecResult(False, "安装目标版本信息缺失，未执行安装")
    preflight, repair = _dpkg_repair_plan()
    if preflight is not None:
        return preflight
    if deps:
        sim_ok, new_deps, sim_detail = _apt_simulate_install(file)
        if not sim_ok:
            return _installation_failure(ExecResult(
                False,
                f"{file.name} 的 apt 预演失败，未执行安装；请查看详情后重试",
                sim_detail,
                detail_format="text",
            ), preflight=True)
        install_progress.report(
            0,
            f"正在安装 {file.name}；大型应用下载和配置可能需要较长时间，"
            "安装过程中请勿中断或关闭应用",
            phase="preparing",
        )
        apt_command = ["apt-get", "install", "-y", "--no-remove"]
        if allow_downgrade:
            apt_command.append("--allow-downgrades")
        apt_command += ["--", str(file)]
        if repair:
            r = _run_sudo(
                ["/bin/sh", "-c", _shell_sequence([repair, apt_command]), "sh"],
                timeout=_APT_INSTALL_TIMEOUT,
                on_output=install_progress.observe_output,
            )
        else:
            r = _run_sudo(
                apt_command,
                timeout=_APT_INSTALL_TIMEOUT,
                on_output=install_progress.observe_output,
            )
        if not r.ok:
            return _installation_failure(r)
        if repair:
            repaired = _dpkg_repair_audit()
            if repaired is not None:
                return repaired
        verified = verify_package(identity["package"], identity["version"])
        if not verified["ok"]:
            return ExecResult(False, "安装命令已结束，但目标版本未通过核验：" + verified["message"], r.detail, detail_format=r.detail_format)
        ver = verified.get("local_state", {}).get("version") or identity["version"]
        tail = f"（版本 {ver}）" if ver else ""
        if new_deps:
            shown = "、".join(new_deps[:8])
            note = f"，依赖已一并安装：{shown}{' 等' if len(new_deps) > 8 else ''}"
        else:
            note = "，依赖均已满足"
        install_progress.report(100, f"{file.name} 安装完成", phase="done")
        return ExecResult(
            True, f"{file.name} 安装完成：{pkg}{tail}{note}{version_note}", r.detail, detail_format=r.detail_format
        )
    unsatisfied = _deb_unsatisfied_deps(file)
    declared = bool(_deb_depends(file))
    if unsatisfied:
        return ExecResult(
            False,
            f"{file.name} 缺少未安装的依赖（deps=false 只装本体）："
            f"{'、'.join(unsatisfied)}。请先安装这些依赖，"
            "或改用自动处理依赖的方式（deps=true）重试",
        )
    install_progress.report(
        0,
        f"正在安装 {file.name}；安装过程中请勿中断或关闭应用",
        phase="preparing",
    )
    dpkg_command = ["dpkg", "-i", str(file)]
    if repair:
        r = _run_sudo(
            ["/bin/sh", "-c", _shell_sequence([repair, dpkg_command]), "sh"],
            timeout=_APT_INSTALL_TIMEOUT,
            on_output=install_progress.observe_output,
        )
    else:
        r = _run_sudo(
            dpkg_command,
            timeout=_APT_INSTALL_TIMEOUT,
            on_output=install_progress.observe_output,
        )
    if not r.ok:
        classified = _installation_failure(r)
        if classified is not r:
            return classified
        low = (r.detail or "").lower()
        if "depend" in low or "依赖" in (r.detail or ""):
            return ExecResult(
                False,
                f"{r.message}。缺少依赖：如需自动安装依赖请用 apt 方式（deps=true）",
                r.detail,
                detail_format=r.detail_format,
            )
        return r
    if repair:
        repaired = _dpkg_repair_audit()
        if repaired is not None:
            return repaired
    verified = verify_package(identity["package"], identity["version"])
    if not verified["ok"]:
        return ExecResult(False, "安装命令已结束，但目标版本未通过核验：" + verified["message"], r.detail, detail_format=r.detail_format)
    ver = verified.get("local_state", {}).get("version") or identity["version"]
    tail = f"（版本 {ver}）" if ver else ""
    dep_note = "，依赖均已满足" if declared else "，无额外依赖"
    install_progress.report(100, f"{file.name} 安装完成", phase="done")
    return ExecResult(
        True, f"{file.name} 安装完成：{pkg}{tail}{dep_note}{version_note}", r.detail, detail_format=r.detail_format
    )


def install_local(path: str = "", target: str = "", dir: str = "", deps: bool = True, allow_downgrade: bool = False, **_) -> ExecResult:
    """本地安装：.deb 默认走 apt（自动处理依赖），deps=false 走 dpkg；
    .whl→pip、.tgz/.tar.gz→npm。路径支持完整/~/当前目录相对名，当前目录
    找不到时返回明确提示（不挂起本地多轮回填）。"""
    f, err = _resolve_local_install(path, dir)
    if f is None:
        return ExecResult(False, err)
    name = f.name.lower()
    allow = _as_bool(allow_downgrade)
    if name.endswith(".deb"):
        return _install_deb(f, _as_bool(deps), allow)
    if name.endswith(".whl"):
        return pip_install(pkg=str(f), target=target, allow_downgrade=allow)
    if name.endswith(".tgz") or name.endswith(".tar.gz"):
        return npm_install(pkg=str(f), target=target, allow_downgrade=allow)
    return ExecResult(
        False, f"不支持的安装格式: {f.suffix or '(无扩展名)'}（支持 .deb/.whl/.tgz/.tar.gz）"
    )


# ── 依赖计划执行（dep_plan → 用户确认 → dep_apply → dep_verify）─────
# 依据 docs/design/dependency_management.md：只装计划内、用户勾选的条目，
# 逐项安装 + 逐项回代码层复验；代码在确认后被改动则拒绝执行。

_DEP_ACTIONABLE = ("apt", "pip")


def _parse_item_selection(items: str | int | None, plan: dict) -> tuple[list[dict] | None, str]:
    """解析用户勾选的计划编号；空表示全部可自动安装项。"""
    entries = list(plan.get("items") or [])
    actionable = {item["index"]: item for item in entries if item.get("channel") in _DEP_ACTIONABLE}
    raw = str(items or "").strip()
    if not raw:
        selected = [actionable[index] for index in sorted(actionable)]
        if not selected:
            return None, "计划里没有可自动安装的项（都已满足或需要人工处理）"
        return selected, ""
    picks: list[int] = []
    for token in re.split(r"[,\s，、]+", raw):
        if not token:
            continue
        if not token.isdigit():
            return None, f"无法识别的编号：{token}（请用「1,3」这样的编号）"
        picks.append(int(token))
    if not picks:
        return None, "没有选择任何条目"
    unknown = [index for index in picks if index not in {item["index"] for item in entries}]
    if unknown:
        return None, f"计划里没有编号 {'、'.join(str(index) for index in unknown)}"
    skipped = [index for index in picks if index not in actionable]
    selected = [actionable[index] for index in sorted(set(picks)) if index in actionable]
    if skipped:
        return selected, (
            "编号 " + "、".join(str(index) for index in skipped)
            + " 不在可自动安装范围内（已满足或需人工处理），本次不会执行"
        )
    return selected, ""


def _pip_install_base(item: dict, root: Path) -> list[str] | None:
    """pip 安装命令前缀：项目 venv 优先，否则用户共享 venv。"""
    if item.get("target_env") == "project":
        python = root / ".venv" / "bin" / "python"
        if python.is_file():
            return [str(python), "-m", "pip"]
    return _user_pip()


def _pending_build_tools(item: dict) -> list[str]:
    """sdist 项执行前仍需补的构建工具（只补计划里展示过、且现在仍缺的 apt 包）。

    计划生成到用户确认之间可能已经装过工具，所以这里重新探测一次；
    反过来，计划里没展示过的包一律不装，避免「确认的」和「装的」不是一回事。
    """
    from bibit_intent.core.dependency_plan import build_tool_status

    declared = list((item.get("build_tools") or {}).get("install") or [])
    if not declared:
        return []
    still_missing = set(build_tool_status().get("install") or [])
    return [package for package in declared if package in still_missing]


def _install_pip_item(
    item: dict, root: Path, *, position: int, total: int, label: str,
) -> tuple[ExecResult, list[str]]:
    """pip 项：需要源码构建时先补齐构建工具，再安装。返回 (结果, 本次补的工具)。"""
    from bibit_intent.core.pip_index import pip_index_args
    from bibit_intent.core.progress import report_progress

    tools = _pending_build_tools(item)
    if tools:
        report_progress(position, total, f"{label} 需要源码构建，先补齐构建工具：{' '.join(tools)}")
        fix = _run_sudo(
            ["apt-get", "install", "-y", "--no-remove", "--", *tools], timeout=900
        )
        if not fix.ok:
            return ExecResult(
                False,
                f"补齐构建工具失败（{' '.join(tools)}）：{fix.message}",
                detail=fix.detail,
                escalate=getattr(fix, "escalate", ""),
                detail_format=fix.detail_format,
            ), []
        remaining = _pending_build_tools(item)
        if remaining:
            return ExecResult(
                False,
                "构建工具已装但仍不可用：" + "、".join(remaining),
                detail="补装后复验未通过，不尝试源码构建",
            ), tools
    base = _pip_install_base(item, root)
    if base is None:
        return ExecResult(False, "无法准备 Python 安装环境（共享 venv 创建失败）"), tools
    return _run(
        base + ["install", *pip_index_args(), item.get("spec") or item["name"]],
        timeout=900,
    ), tools


def _dep_item_rollback(item: dict, python: str, build_tools: list[str]) -> str:
    """该项的回滚命令：先卸本次为构建补的工具，再卸本项自己。"""
    from bibit_intent.core.dependency_plan import rollback_command

    parts = []
    if build_tools:
        parts.append("sudo apt-get remove -- " + " ".join(build_tools))
    main = rollback_command(item, python)
    if main:
        parts.append(main)
    return "；".join(parts)


def _verify_dep_item(item: dict, root: Path, python: str) -> tuple[bool, str]:
    """回代码层复验单项依赖：复用扫描模块的判定逻辑。"""
    from bibit_intent.core.dependency_scan import (
        Requirement, check_requirements, requirements_from_dicts,
    )

    requirement: Requirement | None = None
    parsed = requirements_from_dicts([item])
    if parsed:
        requirement = parsed[0]
    if requirement is None:
        return False, "无法复验（依赖信息不完整）"
    status = check_requirements([requirement], root=root, python=python).get(requirement.key(), {})
    detail = status.get("detail") or status.get("check") or ""
    return bool(status.get("satisfied")), detail


def dep_apply(plan_id: str = "", items: str = "", **_) -> ExecResult:
    """按依赖计划安装：只装计划内条目，逐项验证，失败即停。"""
    from bibit_intent.core.dependency_audit import record_apply
    from bibit_intent.core.dependency_plan import (
        get_plan,
        plan_is_current,
        record_apply_outcome,
    )
    from bibit_intent.core.progress import report_progress

    plan, err = get_plan(plan_id)
    if plan is None:
        return ExecResult(False, err)
    fresh, why = plan_is_current(plan)
    if not fresh:
        return ExecResult(False, why)
    selected, note = _parse_item_selection(items, plan)
    if selected is None:
        return ExecResult(False, note)
    if not selected:
        return ExecResult(False, note or "没有选择可自动安装的条目")

    root = Path(plan["project_root"])
    # 用计划解析出的目标解释器复验，保证「装到哪个环境就在哪个环境验证」
    python = plan.get("verify_python") or plan.get("python") or ""
    results: list[dict] = []
    total_items = len(selected)
    # 逐项上报进度：MCP 侧经 progress_bridge 转成 notifications/progress，
    # 未通过 MCP 调用（直接调 handler）时是 no-op。
    report_progress(0, total_items, f"准备安装 {total_items} 项依赖")
    for position, item in enumerate(selected):
        label = f"[{item['kind']}] {item['name']}"
        build_tools: list[str] = []
        report_progress(position, total_items, f"正在处理 {label}")
        if item["channel"] == "apt":
            r = _run_sudo(
                ["apt-get", "install", "-y", "--no-remove", "--", item["package"]],
                timeout=900,
            )
        elif item["channel"] == "pip":
            r, build_tools = _install_pip_item(
                item, root, position=position, total=total_items, label=label
            )
        else:
            continue
        if not r.ok:
            results.append({
                "index": item["index"], "label": label, "ok": False,
                "message": r.message, "detail": r.detail, "verified": False,
                "build_tools": build_tools,
            })
            # 第一项就失败：还没有任何变更，原样透传（保留 escalate/no_gui_askpass 语义）
            if position == 0:
                record_apply_outcome(results, overall=False)
                return r
            break
        verified, evidence = _verify_dep_item(item, root, python)
        results.append({
            "index": item["index"], "label": label, "ok": True,
            "message": r.message, "detail": r.detail, "verified": verified,
            "evidence": evidence,
            "build_tools": build_tools,
            "rollback": _dep_item_rollback(item, python, build_tools),
        })
        report_progress(
            position + 1,
            total_items,
            f"{label} 已安装并验证" if verified else f"{label} 已安装，验证未通过",
        )

    ok_count = len([item for item in results if item["ok"] and item["verified"]])
    lines = [
        f"依赖计划 {plan['plan_id']}：已执行 {len(results)} 项，"
        f"安装并验证通过 {ok_count} 项。"
    ]
    for item in results:
        mark = "完成" if item["ok"] and item["verified"] else ("未验证" if item["ok"] else "失败")
        line = f"  - [{mark}] {item['label']}"
        if item.get("evidence"):
            line += f"（验证：{item['evidence']}）"
        if item.get("build_tools"):
            line += f"（一并补齐构建工具：{' '.join(item['build_tools'])}）"
        if not item["ok"]:
            line += f"：{item['message']}"
        lines.append(line)
    manual = [
        item for item in (plan.get("items") or [])
        if item.get("channel") not in _DEP_ACTIONABLE and item.get("status") != "satisfied"
    ]
    if manual:
        lines.append("未自动处理（需人工）：" + "、".join(
            f"{item['index']} {item['name']}" for item in manual[:8]
        ))
    rollbacks = [
        f"  - {item['label']}：{item['rollback']}"
        for item in results
        if item.get("ok") and item.get("rollback")
    ]
    if rollbacks:
        lines.append("回滚（如需卸载本次安装的包，请人工确认后执行）：")
        lines.extend(rollbacks)
    if note:
        lines.append(note)
    lines.append("可回复「验证依赖 " + plan["plan_id"] + "」复核安装结果。")
    detail = json.dumps(
        {"plan_id": plan["plan_id"], "results": results, "note": note},
        ensure_ascii=False,
    )
    overall = bool(results) and all(item["ok"] and item["verified"] for item in results)
    record_apply(plan=plan, results=results, note=note, overall=overall)
    record_apply_outcome(results, overall=overall)
    return ExecResult(overall, "\n".join(lines), detail)


def _apt_package_base(name: str) -> str:
    """去掉 APT 仿真输出里的架构后缀，只比较包名。"""
    return name.partition(":")[0]


def _upgradable_packages() -> list[str] | None:
    """只读仿真普通 ``apt-get upgrade``，返回会被升级的包名；失败返回 None。

    这里不用 sudo，也不需要真正升级；固定 ``LC_ALL=C`` 是为了稳定解析 ``Inst``
    行，避免中文 locale 下输出「正在升级」等本地化文本导致解析失败。
    """
    try:
        p = run_command(
            ["apt-get", "-s", "--no-remove", "upgrade"],
            timeout=180,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    names: list[str] = []
    for line in p.stdout.splitlines():
        if not line.startswith("Inst "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        names.append(_apt_package_base(parts[1]))
    return sorted(set(names))


def _held_self_packages() -> set[str]:
    """读取当前被 apt-mark hold 的自研组件包（基础包名集合）。

    上次自升级失败后 ``bibit-agent-upgrade.sh`` 会把相关包重新 hold；hold 的包
    不会出现在 ``apt-get -s upgrade`` 输出里，但脱离式脚本会先 unhold 再升级，
    因此确认「服务会重启」的预检必须把 hold 中的自研包一并纳入。
    """
    try:
        p = run_command(["apt-mark", "showhold"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if p.returncode != 0:
        return set()
    return {_apt_package_base(name) for name in p.stdout.split()}

def _schedule_agent_upgrade(packages: list[str]) -> ExecResult:
    """把 Agent 自身栈的升级交给 Runtime cgroup 之外的 systemd transient unit。

    unit 运行在系统管理器中，不是 ``bianbu-agent-runtime@.service`` 的子进程；
    即使它停止 Runtime，也不会杀掉自己或正在运行的 apt/dpkg。当前 HTTP 会话
    会在 Runtime 重启时断开，因此这里只负责安排并立即返回。
    """
    targets = sorted(set(packages))
    if not Path(_SELF_UPDATE_SCRIPT).exists():
        return ExecResult(
            False,
            "未找到 Agent 组件独立升级脚本；本次系统更新未自动升级 Agent 组件，"
            "请稍后单独处理",
        )
    unit = f"bibit-agent-upgrade-{secrets.token_hex(4)}.service"
    r = _run_sudo(
        [
            "systemd-run",
            "--system",
            "--no-block",
            "--collect",
            f"--unit={unit}",
            _SELF_UPDATE_SCRIPT,
            "--uid", str(os.getuid()),
            *targets,
        ],
        timeout=60,
    )
    if not r.ok:
        return ExecResult(
            False,
            "系统软件包已更新，但安排 Agent 组件后台升级失败：" + r.message,
            r.detail,
            detail_format=r.detail_format,
        )
    return ExecResult(
        True,
        "已安排 Agent 组件后台升级："
        + "、".join(targets)
        + "；升级期间 Runtime 会短暂断开，完成后自动恢复。日志："
        + _SELF_UPDATE_LOG,
        r.detail,
        detail_format=r.detail_format,
    )


def _read_system_update_state() -> tuple[str, dict]:
    """读取系统更新状态文件与详情 JSON；缺失/异常时返回空状态。"""
    try:
        state = _SYSTEM_UPDATE_STATE.read_text(encoding="utf-8").strip()
    except OSError:
        state = ""
    detail: dict = {}
    try:
        value = json.loads(_SYSTEM_UPDATE_DETAIL.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            detail = value
    except (OSError, ValueError):
        pass
    return state, detail


def _system_update_unit_state() -> str:
    """只读查询 bibit-system-update.service 是否仍在运行。"""
    try:
        p = run_command(
            ["systemctl", "is-active", "bibit-system-update.service"],
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    return p.stdout.strip() or "unknown"


def _publish_system_update_progress(state: str) -> None:
    """把系统更新阶段同时投递给 MCP 进度通知与 bibit_task_status。"""
    label = _SYSTEM_UPDATE_LABELS.get(state, f"系统更新：{state}")
    percent = _SYSTEM_UPDATE_PERCENT.get(state)
    report_progress(percent, None, label)
    install_progress.report(percent, label, phase=state)


def _wait_system_update(timeout_seconds: int = 1800) -> tuple[str, dict]:
    """等待脱离式系统更新到达终态，并在阶段变化时发布进度。"""
    deadline = time.monotonic() + timeout_seconds
    last_state = ""
    missing_since: float | None = None
    while True:
        raise_if_cancelled()
        state, detail = _read_system_update_state()
        if state != last_state:
            _publish_system_update_progress(state)
            last_state = state
        if state in _SYSTEM_UPDATE_TERMINAL:
            return state, detail
        # 进入自研组件阶段后，Runtime 即将 stop 并连带 SIGTERM 当前 MCP 会话；
        # 这里立即返回，让「服务将重启」的结果先送达前端，而不是继续轮询直到
        # 被杀（前端看到 EOFError）。
        if state == "agent_upgrading":
            return state, detail

        if not state:
            if missing_since is None:
                missing_since = time.monotonic()
            unit_state = _system_update_unit_state()
            if unit_state in {"inactive", "failed"} and time.monotonic() - missing_since > 5:
                return "missing", {"error": "系统更新任务未生成状态文件且服务已退出"}
        else:
            missing_since = None

        if time.monotonic() >= deadline:
            return "timeout", {"error": "系统更新超过 30 分钟仍未结束"}
        time.sleep(1)


def _system_update_result(state: str, detail: dict) -> ExecResult:
    """把系统更新终态转换为 handler 返回结果。"""
    if state == "agent_upgrading":
        return ExecResult(
            True,
            "Bibit 组件升级已开始，相关服务将重启并短暂断开；完成后会自动弹框显示结果，"
            "也可回复「系统更新状态」查询。",
        )
    if state == "succeeded":
        return ExecResult(True, _SYSTEM_UPDATE_LABELS[state])
    if state == "succeeded_with_skips":
        return ExecResult(True, _SYSTEM_UPDATE_LABELS[state])
    if state == "cancelled":
        return ExecResult(True, _SYSTEM_UPDATE_LABELS[state])
    if state == "failed":
        return ExecResult(False, _SYSTEM_UPDATE_LABELS[state], str(detail.get("error") or ""))
    if state == "missing":
        return ExecResult(False, "系统更新任务未能启动或已退出", str(detail.get("error") or ""))
    return ExecResult(False, f"系统更新异常结束：{state}", str(detail.get("error") or ""))


def pkg_update(confirm: bool = False, **_):
    """Authorize one host-side job; APT must survive Runtime package restarts."""
    script = "/opt/bibit-intent/deploy/bibit-system-update.sh"
    if not Path(script).is_file():
        return ExecResult(False, "缺少系统更新脚本，请重新安装 bibit-intent")
    busy = _apt_dpkg_in_flight()
    if busy is not None:
        return ExecResult(
            True,
            f"{busy}。为避免与正在进行的安装/卸载冲突，本次未启动系统更新；请等待其结束后再重试。",
        )
    if not confirm:
        agent_targets = sorted((set(_upgradable_packages() or []) | _held_self_packages()) & _SELF_UPDATE_PACKAGES)
        if agent_targets:
            return ExecResult(
                False,
                "本次更新包含 Bibit 自身组件（"
                + "、".join(agent_targets)
                + "）。升级期间相关服务会重启并短暂断开，完成后会自动弹框显示结果。是否继续？",
                need_param="confirm",
                confirm=True,
            )
    session = _session_askpass_env() or {}
    desktop = {key: session[key] for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY") if session.get(key)}
    # 上一次运行会残留 succeeded/failed/cancelled 终态；不清掉的话
    # _wait_system_update 一上来就命中终态直接返回，不会跟踪本次任务的进度。
    # 这里与 systemd-run 共用一次 sudo 授权，同步清掉状态文件后再拉起脱离式
    # 任务，避免出现第二个密码框。
    state_file = shlex.quote(str(_SYSTEM_UPDATE_STATE))
    detail_file = shlex.quote(str(_SYSTEM_UPDATE_DETAIL))
    launch = (
        f"rm -f {state_file} {detail_file} && "
        f"systemd-run --system --no-block --collect "
        f"--on-active=10s --timer-property=RemainAfterElapse=no "
        f"--unit=bibit-system-update.service /bin/sh {shlex.quote(script)} "
        f"--uid {os.getuid()} --session {shlex.quote(json.dumps(desktop))}"
    )
    # The only timeout here used to cover the GUI password dialog as well as
    # systemd-run.  A user may legitimately need more than one minute to
    # respond; cancellation remains available through the execution context.
    result = _run_sudo(["/bin/sh", "-c", launch], timeout=None)
    if not result.ok:
        return result
    return ExecResult(
        True,
        "系统更新任务已提交，将在后台启动；这不表示升级已完成。"
        "升级 Bibit 组件期间 Runtime 可能短暂断开，请勿重复发起更新。"
        "完成后会弹框显示结果，也可在重新连接后查询「系统更新状态」。",
    )


def pkg_clean(**_):
    """清理所有软件缓存：apt-get clean"""
    return _run_sudo(["apt-get", "clean"], timeout=120)


def pkg_autoremove(**_):
    """删除系统不再使用的孤立软件：apt-get autoremove -y"""
    return _run_sudo(["apt-get", "autoremove", "-y"], timeout=600)


def pkg_restore_selections(file: str = "", **_) -> ExecResult:
    """从备份文件恢复包列表：sudo dpkg --set-selections < file"""
    if not file:
        return ExecResult(False, "缺少备份文件路径")
    try:
        data = Path(file).read_text(encoding="utf-8")
    except OSError as e:
        return ExecResult(False, f"读取备份文件失败: {e}")
    # dpkg --set-selections 写 /var/lib/dpkg/status，普通用户无权限，必须 sudo。
    r = _run_sudo(["dpkg", "--set-selections"], timeout=120, input=data)
    if not r.ok:
        return r
    return ExecResult(
        True,
        "包列表已恢复；执行 apt-get dselect-upgrade -y（或重启后自动生效）完成安装",
        r.detail,
        detail_format=r.detail_format,
    )


# ── 硬盘管理 ─────────────────────────────────────────────────────────

def disk_mount(dev: str = "", path: str = "", **_) -> ExecResult:
    """挂载分区：mount dev path"""
    if not dev or not path:
        return ExecResult(False, "缺少设备或挂载路径")
    return _run(["mount", dev, path], timeout=60)


def disk_umount(target: str = "", **_) -> ExecResult:
    """卸载分区：umount target（设备或目录）"""
    if not target:
        return ExecResult(False, "缺少设备或目录")
    return _run(["umount", target], timeout=60)


def disk_check(dev: str = "/dev/sda1", **_) -> ExecResult:
    """硬盘检查：fsck dev（危险，勿检查已挂载分区）"""
    return _run(["fsck", "-y", dev], timeout=600)


def disk_format(dev: str = "", **_) -> ExecResult:
    """格式化分区：mkfs.ext4 dev（非常危险）"""
    if not dev:
        return ExecResult(False, "缺少设备")
    return _run(["mkfs.ext4", "-F", dev], timeout=600)


# ── 进程管理 ─────────────────────────────────────────────────────────

def proc_kill(pid: int = 0, **_) -> ExecResult:
    """中止进程：kill pid"""
    if not pid:
        return ExecResult(False, "缺少进程号")
    return _run(["kill", str(pid)], timeout=30)


def proc_zombie(**_):
    """清除僵尸进程：ps -eal | awk '$2==Z' | xargs kill -9"""
    r = _run(["ps", "-eal"], timeout=15)
    if not r.ok:
        return r
    pids = []
    for line in r.detail.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] == "Z":
            pids.append(parts[3])
    if not pids:
        return ExecResult(True, "没有僵尸进程", "")
    return _run(["kill", "-9"] + pids, timeout=30)


# ── 服务管理 ─────────────────────────────────────────────────────────

def service_restart(name: str = "", **_) -> ExecResult:
    """重启服务：systemctl restart name"""
    if not name:
        return ExecResult(False, "缺少服务名")
    return _run(["systemctl", "restart", name], timeout=60)


def service_stop(name: str = "", **_) -> ExecResult:
    """停止服务：systemctl stop name"""
    if not name:
        return ExecResult(False, "缺少服务名")
    return _run(["systemctl", "stop", name], timeout=60)


def service_start(name: str = "", **_) -> ExecResult:
    """启动服务：systemctl start name"""
    if not name:
        return ExecResult(False, "缺少服务名")
    return _run(["systemctl", "start", name], timeout=60)


# ── 用户管理 ─────────────────────────────────────────────────────────

def user_add(name: str = "", **_) -> ExecResult:
    """增加用户：useradd -m -s /bin/bash name（wiki 的 adduser 非交互版）"""
    if not name:
        return ExecResult(False, "缺少用户名")
    return _run(["useradd", "-m", "-s", "/bin/bash", name], timeout=30)


def user_del(name: str = "", **_) -> ExecResult:
    """删除用户：userdel -r name（wiki 的 deluser 非交互版）"""
    if not name:
        return ExecResult(False, "缺少用户名")
    return _run(["userdel", "-r", name], timeout=30)


def user_lock(name: str = "", **_) -> ExecResult:
    """锁定用户：usermod -L name"""
    if not name:
        return ExecResult(False, "缺少用户名")
    return _run(["usermod", "-L", name], timeout=30)


def user_unlock(name: str = "", **_) -> ExecResult:
    """解锁用户：usermod -U name"""
    if not name:
        return ExecResult(False, "缺少用户名")
    return _run(["usermod", "-U", name], timeout=30)


# ── iptables 管理 ────────────────────────────────────────────────────

def iptables_block(ip: str = "", **_) -> ExecResult:
    """阻止一个 IP：iptables -A INPUT -s ip -j DROP"""
    if not ip:
        return ExecResult(False, "缺少 IP 地址")
    return _run_sudo(["/usr/sbin/iptables", "-A", "INPUT", "-s", ip, "-j", "DROP"], timeout=30, force_reauth=True)


def net_enable_nat(**_):
    """立即让网络支持 NAT：ip_forward=1 + iptables MASQUERADE"""
    r = _run(["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=15)
    if not r.ok:
        return r
    return _run(["iptables", "-t", "nat", "-I", "POSTROUTING", "-j", "MASQUERADE"],
                timeout=30)


# ── 文件编码转换 ─────────────────────────────────────────────────────

def text_convert(path: str = "", **_) -> ExecResult:
    """转换文件名 GBK→UTF8：convmv -r -f cp936 -t utf8 --notest --nosmart path"""
    if not path:
        return ExecResult(False, "缺少路径")
    return _run(
        ["convmv", "-r", "-f", "cp936", "-t", "utf8", "--notest", "--nosmart", path],
        timeout=600,
    )


# ── 分发表 ───────────────────────────────────────────────────────────
HANDLERS = {
    "pkg_install": pkg_install,
    "pkg_remove": pkg_remove,
    "pip_install": pip_install,
    "pip_uninstall": pip_uninstall,
    "npm_install": npm_install,
    "npm_uninstall": npm_uninstall,
    "install_local": install_local,
    "dep_apply": dep_apply,
    "pkg_update": pkg_update,
    "pkg_clean": pkg_clean,
    "pkg_autoremove": pkg_autoremove,
    "pkg_restore_selections": pkg_restore_selections,
    "disk_mount": disk_mount,
    "disk_umount": disk_umount,
    "disk_check": disk_check,
    "disk_format": disk_format,
    "proc_kill": proc_kill,
    "proc_zombie": proc_zombie,
    "service_restart": service_restart,
    "service_stop": service_stop,
    "service_start": service_start,
    "user_add": user_add,
    "user_del": user_del,
    "user_lock": user_lock,
    "user_unlock": user_unlock,
    "iptables_block": iptables_block,
    "net_enable_nat": net_enable_nat,
    "text_convert": text_convert,
}
