"""core/appcli_execution.py — 应用命令行执行策略：通道判定与工作区约束

执行应用前必须回答两个问题，且都由本模块给出单一答案：

1. **通道**：``channel: host`` 的应用需要宿主桌面/命名空间，在 Runtime worker
   的沙箱里执行只会产出半成品（X11 socket 不可达、应用随 scope 被杀），因此必须在
   执行前拒绝并交由上层转交 host（``escalate="needs_host_channel"``）。
   沙箱判定与 Runtime 侧 ``BIANBU_SANDBOX_ACTIVE`` 的语义保持一致。
2. **路径**：注册表里标了 ``path: true`` 的参数必须落在应用工作区内，
   越界（绝对路径在区外、``..`` 逃逸、软链指向区外）一律拒绝，执行时替换为绝对路径，
   这样应用不论以什么 cwd 启动都写到同一个可预期的位置。
"""
from __future__ import annotations

import os
from pathlib import Path

from bibit_intent.core.appcli_registry import DEFAULT_APPCLI_TIMEOUT_SECONDS

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "APPCLI_ROOT_ENV",
    "AppCliPathError",
    "channel_allowed",
    "appcli_root",
    "is_sandboxed",
    "resolve_in_workspace",
]

#: 工作区根目录覆盖；沿用 BIBIT_INTENT_* 环境变量约定。
APPCLI_ROOT_ENV = "BIBIT_INTENT_APPCLI_ROOT"

#: 渲染类动作的默认超时；``appcli_run`` 的 timeout 参数可覆盖。
#: 数值与注册表字段 ``timeout_seconds`` 的默认值共用同一个常量，避免两处漂移。
DEFAULT_TIMEOUT_SECONDS = DEFAULT_APPCLI_TIMEOUT_SECONDS


class AppCliPathError(ValueError):
    """路径参数越出应用工作区。"""


def appcli_root() -> Path:
    """应用工作区根目录（不存在也算得出路径，由执行前按需创建）。

    默认放在用户主目录下的 ``bibit-appcli``：产出物是给用户看的文件，
    不应埋在数据目录里；同时又不等于整个家目录，避免模板参数误写家目录里的配置。
    """
    override = os.environ.get(APPCLI_ROOT_ENV, "").strip()
    base = Path(override).expanduser() if override else Path.home() / "bibit-appcli"
    return base.resolve()


def is_sandboxed() -> bool:
    """当前进程是否处在 Runtime worker 的沙箱里。

    与 Runtime 侧一致：``BIANBU_SANDBOX_ACTIVE`` 取真值即为沙箱；另外把
    ``BIANBU_WORKER_EFFECTIVE_MODE=sandbox`` 也算作沙箱，防止只透传了一个变量时
    被误判成 host。
    """
    active = str(os.environ.get("BIANBU_SANDBOX_ACTIVE", "0")).strip().lower()
    if active in {"1", "true", "yes"}:
        return True
    mode = str(os.environ.get("BIANBU_WORKER_EFFECTIVE_MODE", "")).strip().lower()
    return mode == "sandbox"


def channel_allowed(channel: str, *, sandboxed: bool | None = None) -> bool:
    """该 channel 在当前环境是否可执行。``artifact-only`` 任何环境都允许。"""
    if channel != "host":
        return True
    return not (is_sandboxed() if sandboxed is None else sandboxed)


def resolve_in_workspace(value: str, *, root: Path | None = None) -> Path:
    """把路径参数解析成工作区内的绝对路径，越界抛 AppCliPathError。

    相对路径按工作区根解析（写到哪里可预期）；绝对路径必须已经在工作区内。
    """
    base = (root or appcli_root()).resolve()
    raw = str(value).strip()
    if not raw:
        raise AppCliPathError("路径参数为空")
    candidate = Path(raw).expanduser()
    resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if resolved != base and base not in resolved.parents:
        raise AppCliPathError(f"{raw} 不在应用工作区 {base} 内")
    return resolved
