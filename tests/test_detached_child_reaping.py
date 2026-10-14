"""桌面 GUI 子进程兜底回收：launch_app/lock_screen/camera_open/exec 不得留 zombie。

bibit-intent 用 start_new_session 把 GUI 子进程挂到独立会话（避免服务 restart
连带杀掉用户窗口），代价是不能阻塞 wait()。若 spawn 后既不 wait 也不轮询，子进程
被外部结束（camera_close 的 pkill、用户解锁/关窗）后会一直以 <defunct> 挂在服务的
/proc/<pid>，实测「打开相机 → 关闭相机」必留一个 zombie snapshot。
"""
import os
import time

import pytest

from bibit_intent.execution import system_tools as st


def _proc_state(pid: int):
    """返回 /proc/<pid> 状态字符；进程句柄已消失（已回收）时返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().split(") ", 1)[1].split()[0]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return None


def _wait_gone(pid: int, timeout: float = 5.0) -> str | None:
    deadline = time.monotonic() + timeout
    state = _proc_state(pid)
    while state is not None and time.monotonic() < deadline:
        time.sleep(0.05)
        state = _proc_state(pid)
    return state


def test_spawn_detached_child_is_reaped_without_explicit_wait():
    """不调用 wait()/poll() 也必须回收——camera_open 正是这种 fire-and-forget。"""
    proc = st._spawn_detached(["/bin/sh", "-c", "exit 0"], None)

    residual = _wait_gone(proc.pid)

    assert residual is None, f"子进程退出后仍残留（state={residual}，僵尸未回收）"


def test_spawn_detached_does_not_leave_zombie_after_terminate():
    """外部结束子进程（等价 camera_close 的 pkill）后同样不得留僵尸。"""
    proc = st._spawn_detached(["/bin/sleep", "30"], None)
    assert proc.poll() is None, "长驻子进程不应被误等待阻塞"

    proc.terminate()
    residual = _wait_gone(proc.pid)

    assert residual is None, f"被结束的子进程仍残留（state={residual}）"


def test_spawn_detached_overwrites_are_all_reaped():
    """连续多次 spawn（重复打开应用/相机）不能累积僵尸。"""
    procs = [st._spawn_detached(["/bin/true"], None) for _ in range(5)]

    for proc in procs:
        assert _wait_gone(proc.pid) is None, f"pid {proc.pid} 退出后未回收"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
