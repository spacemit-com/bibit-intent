"""进度桥：把 worker 线程里的同步进度转成事件循环上的 MCP 通知。

MCP handler 经 ``asyncio.to_thread`` 在 worker 线程执行，而
``ctx.report_progress`` 是绑定事件循环的协程。线程里不能直接 await，
所以由本模块持有事件循环与一个队列：

- 线程侧 ``report_progress()`` 用 ``loop.call_soon_threadsafe`` 入队；
- 循环侧 pump 任务出队并 await 真正的上报函数。

绑定的是 ContextVar，而 ``asyncio.to_thread`` 会复制当前上下文，所以线程侧
读得到同一个 reporter。未绑定（直接调用 handler、或 SDK 不提供 Context 注入）
时 ``report_progress()`` 是 no-op，工具行为与接入进度前完全一致。

上报失败只记一次日志并停止后续上报：进度是观测量，不是执行的一部分，
不能因为它拖垮正在进行的安装。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

ProgressEmitter = Callable[[float, float | None, str | None], Awaitable[None]]

_STOP = object()
_CLOSE_TIMEOUT = 1.0


class _Reporter:
    """一次工具调用期间有效的进度上报器。"""

    __slots__ = ("_loop", "_queue", "_emitter", "_failed", "closed")

    def __init__(self, loop, emitter: ProgressEmitter) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._emitter = emitter
        self._failed = False
        self.closed = False

    def report(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """线程安全：可从 worker 线程调用；任何情况下都不抛异常。"""
        if self.closed or self._failed:
            return
        try:
            value = float(progress)
        except (TypeError, ValueError):
            return
        self._enqueue((value, total, message))

    def finish(self) -> None:
        """停止接收并排队结束哨兵。

        哨兵必须和进度项走同一条 ``call_soon_threadsafe`` 队列：直接
        ``put_nowait`` 会插到已调度但尚未执行的进度回调之前，把最后一条
        进度（例如「已完成」）丢掉。
        """
        self.closed = True
        self._enqueue(_STOP)

    def _enqueue(self, item) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            # 事件循环已关闭：队列不会再被消费，直接放入交给 drain 退出
            self._queue.put_nowait(item)

    async def drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            if self._failed:
                continue
            value, total, message = item
            try:
                await self._emitter(value, total, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 上报失败不影响执行
                self._failed = True
                logger.warning(
                    "MCP 进度上报失败，后续进度已忽略: %s: %s",
                    type(exc).__name__,
                    exc,
                )


_reporter: contextvars.ContextVar[_Reporter | None] = contextvars.ContextVar(
    "bibit_intent_progress_reporter", default=None
)


def report_progress(
    progress: float,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """handler 侧上报进度；没有绑定进度桥时静默忽略。"""
    reporter = _reporter.get()
    if reporter is not None:
        reporter.report(progress, total, message)


@asynccontextmanager
async def progress_bridge(emitter: ProgressEmitter | None) -> AsyncIterator[None]:
    """绑定进度桥，使 ``asyncio.to_thread`` 里的 handler 能上报进度。"""
    if emitter is None:
        yield
        return
    reporter = _Reporter(asyncio.get_running_loop(), emitter)
    token = _reporter.set(reporter)
    task = asyncio.create_task(reporter.drain())
    try:
        yield
    finally:
        _reporter.reset(token)
        reporter.finish()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CLOSE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


__all__ = ["ProgressEmitter", "progress_bridge", "report_progress"]
