from bibit_intent.mcp_server.server import _build_executor, _tool_annotations
from bibit_intent.execution.system_tools import ExecResult


def test_mcp_install_reaches_existing_authorization_handler():
    registry, executor = _build_executor()
    calls = []
    def install(**params):
        calls.append(params)
        return ExecResult(False, "系统授权被取消")

    executor._handlers["pkg_install"] = install
    assert registry.get("pkg_install").system.get("confirm")
    result = executor.execute("pkg_install", {"pkg": "bash"})
    assert result.ok is False
    assert result.message == "系统授权被取消"
    assert calls == [{"pkg": "bash"}]


def test_mcp_format_confirmation_is_enforced_by_the_runtime_approval_gate():
    """MCP 侧不再二次拦截 confirm 工具，审批由 Runtime 的注解驱动（440d1c7）。

    MCP 没有把「已批准」回传的通道，本地再拒一次就是一道永远无法满足的闸门：
    appcli_run、关机、休眠会一律返回「已取消」。边界因此移到注解上——confirm
    工具必须带 destructiveHint，注解丢了 Runtime 就不会弹审批，危险工具等于
    变成免审批。
    """
    registry, executor = _build_executor()
    calls = []
    executor._handlers["disk_format"] = lambda **params: (
        calls.append(params), ExecResult(True, "已执行")
    )[1]

    tool = registry.get("disk_format")
    assert tool.system.get("confirm")
    annotations = _tool_annotations(tool)
    destructive = getattr(
        annotations, "destructive_hint", getattr(annotations, "destructiveHint", None)
    )
    assert destructive is True, "confirm 工具丢了 destructive 注解，Runtime 不会弹审批"

    result = executor.execute("disk_format", {"dev": "/dev/mock-only"})

    assert result.ok is True
    assert calls == [{"dev": "/dev/mock-only"}]
