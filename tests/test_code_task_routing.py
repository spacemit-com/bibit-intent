"""代码类请求的前置门：端侧不做判定，一律升级云端交给 code_task。

只测正则的命中与不命中边界，不碰路由全链路（那需要 gateway/embedding）。
"""
from __future__ import annotations

import pytest

from bibit_intent.routing.intent import _CODE_TASK_RE


@pytest.mark.parametrize(
    "text",
    [
        "帮我写个脚本统计日志里的错误行数",
        "calc.py 里的 add 算错了帮我改一下",
        "调试一下这个报错",
        "这段代码帮我优化一下",
        "main.c 编译不过，报了个段错误",
        "fix the bug in parse_apt.c",
        "explain what this function does",
        "分析一下这个项目的依赖",
        "重构 todo.py",
        "跑通这个 Makefile",
    ],
)
def test_code_requests_are_routed_to_cloud(text: str) -> None:
    assert _CODE_TASK_RE.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "查看当前音量",
        "现在音量是多少",
        "打开浏览器",
        "帮我写这周的周报",
        "这周的项目进度怎么样",
        "把声音调大一点",
        "现在几点了",
        "连一下 wifi",
    ],
)
def test_unrelated_requests_are_not_captured(text: str) -> None:
    assert not _CODE_TASK_RE.search(text), text
