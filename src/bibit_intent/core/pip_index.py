"""pip 索引配置（端侧统一走 SpacemiT 镜像）。

镜像对未托管的包会 302 到 pypi.org（如纯 Python 的 requests），所以「从镜像装」
不等于「只从内网装」，依赖计划里要如实标注真实下载源。
"""
from __future__ import annotations

PIP_INDEX_URL = "https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple"


def pip_index_args() -> list[str]:
    return ["-i", PIP_INDEX_URL]
