"""全局测试夹具。

依赖审计默认写到用户数据目录；测试里必须改道，否则跑一次 pytest 就往真实的
``~/.local/share/bibit-intent/dependency-audit.jsonl`` 塞记录。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_dependency_audit(tmp_path_factory, monkeypatch):
    target = tmp_path_factory.mktemp("dependency-audit") / "audit.jsonl"
    monkeypatch.setenv("BIBIT_INTENT_DEPENDENCY_AUDIT", str(target))
    return target
