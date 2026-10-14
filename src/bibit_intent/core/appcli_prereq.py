"""core/appcli_prereq.py — 应用前置齐备度检查（只读）

`appcli_list`（H02）要告诉用户「缺什么」，`appcli_run`（H03）要据此拒绝执行、
H07 要据此给出依赖计划提示，因此判定逻辑独立成模块。

三条判据，缺一即 `needs_install`：

1. ``entry_point`` 能在 PATH 里找到——这才是真正决定应用能否被执行的判据；
2. ``requires.apt`` 里的软件包已安装（复用 core.package_state 的 dpkg 解析）；
3. ``requires.python`` 里的分发包已安装（按**分发包名**查元数据，不用模块名，
   因为 ``cli-anything-libreoffice`` 的导入名是 ``cli_anything.libreoffice``）。

只做只读查询，不安装任何东西，也不修改环境。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib import metadata

from bibit_intent.core.appcli_registry import AppCliSpec
from bibit_intent.core.package_state import query_package_state


@dataclass(frozen=True)
class AppCliReadiness:
    """一个应用的前置齐备度。"""

    entry_point_found: bool
    missing_apt: tuple[str, ...] = ()
    missing_python: tuple[str, ...] = ()
    state: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.entry_point_found
            and not self.missing_apt
            and not self.missing_python
        )

    @property
    def status(self) -> str:
        return "ready" if self.ready else "needs_install"

    def missing(self) -> tuple[str, ...]:
        """人类可读的缺失清单，顺序固定便于测试与展示。"""
        items: list[str] = []
        items.extend(f"apt:{name}" for name in self.missing_apt)
        items.extend(f"python:{name}" for name in self.missing_python)
        if not self.entry_point_found:
            items.append("入口命令未安装")
        return tuple(items)


def _distribution_missing(name: str, dist_version) -> bool:
    try:
        dist_version(name)
    except metadata.PackageNotFoundError:
        return True
    except (OSError, ValueError):
        # 元数据损坏时保守判为未满足，由依赖计划去核对，不当作已装。
        return True
    return False


def check_readiness(
    spec: AppCliSpec,
    *,
    which=shutil.which,
    package_state=query_package_state,
    dist_version=metadata.version,
) -> AppCliReadiness:
    """检查单个应用的前置。

    三个探针都可注入（``which`` / ``package_state`` / ``dist_version``），
    判定逻辑本身不依赖具体环境，便于测试与复用。
    """
    entry_point_found = which(spec.entry_point) is not None

    missing_apt = tuple(
        name for name in spec.requires.apt if package_state(name).status != "installed"
    )
    missing_python = tuple(
        name for name in spec.requires.python
        if _distribution_missing(name, dist_version)
    )
    return AppCliReadiness(
        entry_point_found=entry_point_found,
        missing_apt=missing_apt,
        missing_python=missing_python,
    )
