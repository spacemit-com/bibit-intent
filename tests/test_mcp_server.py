from __future__ import annotations

import json
import asyncio
import inspect
import os
import pwd
import sys
import tempfile
from unittest.mock import patch

import pytest

from bibit_intent.mcp_server.server import (
    _build_executor,
    _mark_tool_failure,
    _tool_payload_failed,
)


@pytest.mark.asyncio
async def test_time_tool_preserves_confirmation(monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    from bibit_intent.execution.system_tools import ExecResult
    from bibit_intent.mcp_server import server

    registry, executor = _build_executor()
    confirmation = {"kind": "disable_ntp_and_set_time", "datetime": "15:30:00",
                    "language": "zh", "description": "Confirm NTP disable"}
    monkeypatch.setattr(executor, "execute", lambda *args, **kwargs: ExecResult(False, "Confirm", confirmation=confirmation))
    monkeypatch.setattr(server, "_build_executor", lambda: (registry, executor))
    monkeypatch.setattr(server, "_tools_for_groups", lambda *args: [registry.get("set_system_time")])
    mcp = server.create_server()
    result = json.loads(await mcp._tool_manager.get_tool("set_system_time").fn(datetime="15:30"))
    assert result["confirmation"] == confirmation
    assert result["ok"] is False
    assert "token" not in result["confirmation"]


def test_mcp_stdio_initialize_list_and_call():
    """Exercise the standard MCP handshake and a read-only tool end to end."""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bibit_intent.mcp_server"],
            # Keep the src-layout package ahead of any repository-root leftovers.
            env={**os.environ, "PYTHONPATH": str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src')},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info is not None
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert "sys_uptime" in names
                result = await session.call_tool("sys_uptime", {})
                assert result.isError is False
                assert result.content

    asyncio.run(run())


def test_natural_language_intent_is_not_mcp_claim():
    """Document that a matching local intent is not evidence of an MCP call."""
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.resources import resource_path
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.execution.system_tools import HANDLERS

    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, dict(HANDLERS))
    result = RuleEngine(registry).match("系统已经运行多久了？")
    assert result is not None
    assert result.tool_name == "sys_uptime"


def test_natural_language_mcp_phrase_avoids_local_rule():
    """A distinct phrasing should be left for the Runtime/cloud MCP path."""
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.resources import resource_path
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.execution.system_tools import HANDLERS

    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, dict(HANDLERS))
    result = RuleEngine(registry).match(
        "How long has this host remained continuously operational?"
    )
    assert result is None


def test_mcp_registry_exposes_tools_and_skills():
    registry, _ = _build_executor()
    names = {tool.name for tool in registry.all()}
    assert {"get_vol_status", "get_display_light", "set_volume", "launch_app", "wifi_scan", "proc_list",
            "list_apps"} <= names
    for tool in registry.all():
        schema = tool.mcp_schema()["function"]
        assert schema["name"] == tool.name
        assert schema["parameters"]["type"] == "object"


def test_mcp_adapter_execution_preserves_executor_validation():
    registry, executor = _build_executor()
    result = executor.execute("set_volume", {"level": 60})
    assert result.ok is False or result.message
    adjust = executor.execute("adjust_volume", {})
    assert adjust.need_param == "direction"
    missing = executor.execute("launch_app", {})
    assert missing.need_param == "app_name"


def test_audio_output_switch_normalizes_es8326_aliases():
    from bibit_intent.execution.system_tools import _canonical_output_device

    assert _canonical_output_device("snd-es8326") == "analog"
    assert _canonical_output_device("snd_es8326") == "analog"
    assert _canonical_output_device("snd-8326") == "analog"
    assert _canonical_output_device("es8326") == "analog"
    assert _canonical_output_device("speaker") == "speaker"


def test_file_search_executes_via_filesearch_service(tmp_path, monkeypatch):
    """file_search 在 MCP 侧应转发到 bibit-filesearch 服务，而非"无执行实现"。"""
    from unittest.mock import patch

    from bibit_intent import file_results
    monkeypatch.setattr(file_results, "load_settings", lambda port=None: {
        "index_dirs": [str(tmp_path)], "exclude_patterns": [],
        "indexable_types": ["md"], "max_depth": 3,
    })
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("真实正文")
    registry, executor = _build_executor()
    assert registry.get("file_search").system.get("type") == "filesearch"
    with patch("httpx.post") as fake_post:
        fake_post.return_value.raise_for_status.return_value = None
        fake_post.return_value.json.return_value = {
            "code": 0,
            "data": {"results": [
                {"fileName": "a.md", "filePath": str(tmp_path / "a.md"), "confidence": 1.0},
                {"fileName": "b.md", "filePath": str(tmp_path / "b.md"), "confidence": 0.5},
            ]},
        }
        result = executor.execute("file_search", {"query": "GTK"})
        assert result.ok is True
        assert "a.md（相对匹配度 100%）" in result.message
        assert "按相关度排序，第 1 位最相关" in result.message
        assert "\n\n第 1 位：a.md" in result.message
        assert "\n\n第 2 位：b.md" in result.message
        assert "\n\n可点击文件名" in result.message
        assert "b.md（相对匹配度 50%）" in result.message
    # 空 query → 缺参澄清（executor 校验层拦截），而非"无执行实现"
    empty = executor.execute("file_search", {})
    assert empty.need_param == "query"


def test_mcp_unknown_tool_is_safe():
    _, executor = _build_executor()
    result = executor.execute("does_not_exist", {})
    assert result.ok is False
    assert "未知工具" in result.message


def test_camera_open_and_close_have_handler():
    """camera_open / camera_close / take_photo 应已注册执行 handler，不再是“无执行实现”。"""
    from unittest.mock import patch

    from bibit_intent.execution.system_tools import ExecResult

    registry, executor = _build_executor()
    assert registry.get("camera_open").system.get("type") == "shell"
    assert registry.get("camera_close").system.get("type") == "shell"
    assert registry.get("take_photo").system.get("type") == "shell"

    # take_photo：helper 报告「相机在跑、停在相机页」时直接拍照并透传路径
    fake = {"ok": True, "running": True, "can_take": True,
            "path": "/home/bianbu/Pictures/Camera/x.jpeg"}
    with patch("bibit_intent.execution.system_tools._find_desktop_session_env",
               return_value={"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000"}), \
         patch("bibit_intent.execution.system_tools._capture_call", return_value=fake) as fake_call:
        r = executor.execute("take_photo", {})
        assert r.ok is True
        assert "x.jpeg" in r.message
        # 首个动作必须是 status，且相机已在跑时不得再开一次
        assert fake_call.call_args_list[0][0][0] == "status"
        assert [c[0][0] for c in fake_call.call_args_list].count("take") >= 1

    # camera_close：pkill 命中 → ok；未命中 → 提示相机未运行
    with patch("bibit_intent.execution.system_tools._run",
               return_value=ExecResult(True, "ok", "")) as fake_run:
        r = executor.execute("camera_close", {})
        assert r.ok is True
        fake_run.assert_called_once_with(["pkill", "-x", "snapshot"], timeout=3)

    # camera_open：注入桌面会话环境后 Popen 启动 snapshot
    fake_env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000"}
    with patch("bibit_intent.execution.system_tools._find_desktop_session_env",
               return_value=fake_env), \
         patch("bibit_intent.execution.system_tools.subprocess.Popen") as fake_popen:
        r = executor.execute("camera_open", {})
        assert r.ok is True
        fake_popen.assert_called_once()
        # 环境必须带上桌面会话变量，保证 Wayland 客户端连得上
        _, kwargs = fake_popen.call_args
        assert kwargs["env"].get("WAYLAND_DISPLAY") == "wayland-1"
        assert kwargs["start_new_session"] is True


def test_camera_home_resolution_follows_session_user():
    """拍照目录必须跟会话用户名走，不能写死 bianbu 或沿用服务自身 HOME。"""
    from bibit_intent.execution import camera_capture as cc
    from bibit_intent.execution import system_tools as st

    # 服务以 root 运行（HOME=/root），会话属主是 uid 1000 → 注入的 HOME 必须是
    # 会话属主的家目录，且不能覆盖会话自带的 HOME。
    session = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    with patch.dict(st.os.environ, {"HOME": "/root"}, clear=False):
        env = st._gui_child_env(session)
        assert env["HOME"] == pwd.getpwuid(1000).pw_dir
        assert env["XDG_CONFIG_HOME"] == os.path.join(pwd.getpwuid(1000).pw_dir, ".config")
    assert st._gui_child_env(dict(session, HOME="/home/someoneelse"))["HOME"] == "/home/someoneelse"

    # 相册目录取 user-dirs.dirs 的 XDG_PICTURES_DIR，而非写死 <home>/Pictures
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".config"))
        with open(os.path.join(tmp, ".config", "user-dirs.dirs"), "w", encoding="utf-8") as fh:
            fh.write('XDG_PICTURES_DIR="$HOME/我的图片"\n')
        with patch.dict(cc.os.environ, {"HOME": tmp}, clear=False):
            assert cc._pictures_dir() == os.path.join(tmp, "我的图片", "Camera")
        # 绝对路径写法同样支持
        with open(os.path.join(tmp, ".config", "user-dirs.dirs"), "w", encoding="utf-8") as fh:
            fh.write('XDG_PICTURES_DIR="/srv/photos"\n')
        with patch.dict(cc.os.environ, {"HOME": tmp}, clear=False):
            assert cc._pictures_dir() == os.path.join("/srv/photos", "Camera")
        # user-dirs.dirs 缺失 → 退回 <home>/Pictures（不能是 /home/bianbu）
        os.remove(os.path.join(tmp, ".config", "user-dirs.dirs"))
        with patch.dict(cc.os.environ, {"HOME": tmp}, clear=False):
            assert cc._pictures_dir() == os.path.join(tmp, "Pictures", "Camera")


def test_take_photo_prompts_for_camera_authorization():
    """首次拍授权框卡住时：不许直接报「没写出照片」，要先等用户点允许再补拍。"""
    from unittest.mock import patch

    from bibit_intent.execution import system_tools as st

    registry, executor = _build_executor()
    session = {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    awaiting = {"ok": True, "running": True, "can_take": True,
                "awaiting_authorization": True, "permission": "unset"}
    granted = {"ok": True, "running": True, "can_take": True,
               "awaiting_authorization": False, "permission": "yes"}
    taken = {"ok": True, "path": "/home/bianbu/Pictures/Camera/a.jpeg"}

    calls: list[tuple] = []

    def fake_call(action, env, timeout=25, args=None):
        calls.append((action, tuple(args or ())))
        if action == "status":
            # 第一次问：卡在授权；等过之后再问：已授权
            return granted if any(c[0] == "await-permission" for c in calls) else awaiting
        if action == "await-permission":
            return {"ok": True, "reason": "granted", "notified": True}
        if action == "take":
            return taken
        return {"ok": False, "reason": "unexpected"}

    with patch.object(st, "_find_desktop_session_env", return_value=session), \
         patch.object(st, "_capture_call", side_effect=fake_call):
        r = executor.execute("take_photo", {})

    assert r.ok is True and "a.jpeg" in r.message
    assert any(c[0] == "await-permission" for c in calls), calls
    # helper 必须以独立 argv 收到等待参数，不能塞成一整串
    await_args = next(c[1] for c in calls if c[0] == "await-permission")
    assert "--require-dialog" in await_args and "--notify" in await_args

    # 用户一直没点（超时）→ 给出「请点允许」的指引，且不得谎报已拍照
    def timeout_call(action, env, timeout=25, args=None):
        if action == "status":
            return awaiting
        if action == "await-permission":
            return {"ok": False, "reason": "permission_timeout"}
        raise AssertionError("超时后不应再按快门")

    with patch.object(st, "_find_desktop_session_env", return_value=session), \
         patch.object(st, "_capture_call", side_effect=timeout_call):
        r = executor.execute("take_photo", {})

    assert r.ok is False
    assert "允许" in r.message and "no photo" not in r.message.lower()

    # 用户点了拒绝 → 提示去隐私设置重新允许
    def denied_call(action, env, timeout=25, args=None):
        if action == "status":
            return awaiting
        return {"ok": False, "reason": "denied"}

    with patch.object(st, "_find_desktop_session_env", return_value=session), \
         patch.object(st, "_capture_call", side_effect=denied_call):
        r = executor.execute("take_photo", {})
    assert r.ok is False and "拒绝" in r.message

    # 拒绝会让授权框一起消失：helper 报 no_dialog 但 store 是 no，也要说清是被拒
    def denied_via_nodialog(action, env, timeout=25, args=None):
        if action == "status":
            return awaiting
        return {"ok": False, "reason": "no_dialog", "permission": "no"}

    with patch.object(st, "_find_desktop_session_env", return_value=session), \
         patch.object(st, "_capture_call", side_effect=denied_via_nodialog):
        r = executor.execute("take_photo", {})
    assert r.ok is False and "拒绝" in r.message


def test_mcp_group_derivation_and_filter():
    from bibit_intent.mcp_server import server as srv

    registry, _ = _build_executor()
    assert registry.get("get_vol_status").mcp_group() == "readonly"
    assert registry.get("get_display_light").mcp_group() == "readonly"
    assert registry.get("list_apps").mcp_group() == "readonly"
    assert registry.get("get_system_info").mcp_group() == "readonly"
    assert registry.get("file_search").mcp_group() == "readonly"
    assert registry.get("eth_status").mcp_group() == "readonly"
    assert registry.get("eth_ip").mcp_group() == "readonly"
    assert registry.get("window_list").mcp_group() == "readonly"
    assert registry.get("wifi_scan").mcp_group() == "readonly"
    assert registry.get("wifi_status").mcp_group() == "readonly"
    assert registry.get("wifi_ip").mcp_group() == "readonly"
    assert registry.get("reboot").mcp_group() == "admin"
    assert registry.get("launch_app").mcp_group() == "control"
    assert registry.get("set_volume").mcp_group() == "control"

    assert srv._allowed_groups() is None
    tools = srv._tools_for_groups(registry, None)
    assert "reboot" in {t.name for t in tools}
    readonly = srv._tools_for_groups(registry, {"readonly"})
    names = {t.name for t in readonly}
    assert "list_apps" in names and "get_system_info" in names
    assert "reboot" not in names and "launch_app" not in names


def test_mcp_group_partition_is_disjoint_and_complete():
    from bibit_intent.mcp_server import server as srv

    registry, _ = _build_executor()
    by_group: dict[str, set[str]] = {}
    for tool in registry.all():
        by_group.setdefault(tool.mcp_group(), set()).add(tool.name)

    assert set(by_group) == {"readonly", "control", "admin"}
    all_names = {tool.name for tool in registry.all()}
    assert set().union(*by_group.values()) == all_names
    for left in by_group:
        for right in by_group:
            if left < right:
                assert not by_group[left] & by_group[right]

    # 拆成三个 MCP 后，这三个集合就是各 MCP 的工具面边界。
    # readonly 不应包含任何 confirm 工具；control 不应包含 readonly/admin。
    for name in by_group["readonly"]:
        tool = registry.get(name)
        assert tool.system.get("confirm") is not True
        assert tool.mcp_async() is False
    for name in by_group["control"]:
        tool = registry.get(name)
        assert tool.system.get("confirm") is not True
        assert tool.mcp_async() is False
    assert {"reboot", "shutdown", "pkg_install", "pkg_remove", "user_add", "disk_format"} <= by_group["admin"]
    for name in by_group["admin"]:
        assert registry.get(name).mcp_group() == "admin"


def test_mcp_group_filter_matches_annotation_boundaries():
    from bibit_intent.mcp_server.server import _tool_annotations, _tools_for_groups

    registry, _ = _build_executor()
    for group in ("readonly", "control", "admin"):
        tools = _tools_for_groups(registry, {group})
        for tool in tools:
            annotations = _tool_annotations(tool)
            assert annotations is not None, f"{tool.name} 缺少 MCP ToolAnnotations"
            read_only = getattr(annotations, "read_only_hint", getattr(annotations, "readOnlyHint", None))
            destructive = getattr(annotations, "destructive_hint", getattr(annotations, "destructiveHint", None))
            if group == "readonly":
                assert read_only is True and destructive is False, tool.name
            elif group == "admin":
                assert read_only is False, tool.name
            else:
                assert read_only is False, tool.name


def test_mcp_group_env_filter(monkeypatch):
    from bibit_intent.mcp_server import server as srv

    registry, _ = _build_executor()
    monkeypatch.setenv("BIBIT_MCP_GROUPS", "readonly")
    names = {t.name for t in srv._tools_for_groups(registry, srv._allowed_groups())}
    assert "list_apps" in names and "reboot" not in names
    monkeypatch.delenv("BIBIT_MCP_GROUPS")
    assert srv._allowed_groups() is None


def test_launch_apps_schema_array_and_validation():
    registry, executor = _build_executor()
    schema = registry.get("launch_apps").mcp_schema()["function"]
    props = schema["parameters"]["properties"]
    assert props["app_names"]["type"] == "array"
    assert props["app_names"]["items"]["type"] == "string"
    assert "app_names" in schema["parameters"]["required"]
    assert executor.execute("launch_apps", {}).need_param == "app_names"
    bad = executor.execute("launch_apps", {"app_names": 123})
    assert bad.ok is False


def test_close_apps_schema_array_and_validation():
    registry, executor = _build_executor()
    schema = registry.get("close_apps").mcp_schema()["function"]
    props = schema["parameters"]["properties"]
    assert props["app_names"]["type"] == "array"
    assert props["app_names"]["items"]["type"] == "string"
    assert "app_names" in schema["parameters"]["required"]
    assert executor.execute("close_apps", {}).need_param == "app_names"


def test_mcp_schema_generalized_aliases_and_discovery():
    registry, _ = _build_executor()
    launch = registry.get("launch_app")
    desc = launch.mcp_schema()["function"]["description"]
    assert "Supported application aliases:" in desc
    assert "list_apps" in desc  # discovery 提示
    # close_app 与 launch_app 对称：英文描述 + 共用别名注入 + discovery 提示
    close = registry.get("close_app")
    cdesc = close.mcp_schema()["function"]["description"]
    assert "Close a running application" in cdesc
    assert "Supported application aliases:" in cdesc
    assert "list_apps" in cdesc
    assert close.mcp_group() == "control"
    # discovery 布尔标记（list_apps 自身）不应被当成工具名拼进描述
    apps_desc = registry.get("list_apps").mcp_schema()["function"]["description"]
    assert "True tool" not in apps_desc
    # 别名注入不再是 launch_app 特判：任何带 system.aliases 的工具都注入
    for tool in registry.all():
        if tool.system.get("aliases"):
            d = tool.mcp_schema()["function"]["description"]
            assert "Supported application aliases:" in d
    # discovery 工具的 schema 保持只读、非必填
    apps = registry.get("list_apps")
    assert apps.system.get("mcp", {}).get("group") == "readonly"


def test_list_apps_is_readonly_and_callable():
    registry, executor = _build_executor()
    tool = registry.get("list_apps")
    assert tool is not None
    schema = tool.mcp_schema()["function"]
    assert schema["name"] == "list_apps"
    # query 有默认值 → 不是必填；规则/端侧视图不暴露
    assert "query" not in schema["parameters"]["required"]
    assert tool.rule_patterns() == []
    assert tool.local_signature() is None
    result = executor.execute("list_apps", {})
    assert result.ok is True
    assert result.message


def test_mcp_parameter_specs_keep_types_and_defaults():
    from bibit_intent.mcp_server.server import _parameter_specs

    registry, _ = _build_executor()
    specs = {p.name: p for p in _parameter_specs(registry.get("list_apps"))}
    assert specs["query"].default == ""          # 有默认值 → 非必填
    assert specs["query"].annotation is str
    specs2 = {p.name: p for p in _parameter_specs(registry.get("launch_apps"))}
    assert specs2["app_names"].default is inspect.Parameter.empty  # 必填
    assert specs2["app_names"].annotation is list
    volume = {p.name: p for p in _parameter_specs(registry.get("set_volume"))}
    assert volume["level"].default is inspect.Parameter.empty  # 绝对音量必填
    assert volume["level"].annotation is int
    assert "direction" not in volume
    adjust = {p.name: p for p in _parameter_specs(registry.get("adjust_volume"))}
    assert adjust["direction"].default is inspect.Parameter.empty  # 相对方向必填
    assert adjust["direction"].annotation is str
    assert adjust["amount"].default == 10
    assert adjust["amount"].annotation is int


def test_executor_unwraps_arguments_envelope():
    from bibit_intent.execution.executor import Executor

    registry, _ = _build_executor()
    seen: dict[str, object] = {}

    def fake_launch(**kwargs):
        seen.update(kwargs)
        from bibit_intent.execution.system_tools import ExecResult
        return ExecResult(True, "ok")

    executor = Executor(
        registry,
        confirm_callback=lambda _: True,
        handlers={"launch_app": fake_launch},
    )
    r = executor.execute("launch_app", {"arguments": "终端"})
    assert r.ok is True
    assert seen.get("app_name") == "终端"
    # 对象信封也合并回真实参数
    executor.execute("launch_app", {"arguments": {"app_name": "浏览器"}})
    assert seen.get("app_name") == "浏览器"


def test_mcp_unwrap_envelope_before_validation():
    """FastMCP 校验前的信封解包（字符串→单必填参数；list 拆分）。"""
    from bibit_intent.mcp_server.server import _unwrap_envelope

    registry, _ = _build_executor()
    launch = registry.get("launch_app").params
    multi = registry.get("launch_apps").params
    volume = registry.get("set_volume").params

    # 单字符串信封 → 映射到唯一必填参数 app_name
    assert _unwrap_envelope(launch, {"arguments": "终端"}) == {"app_name": "终端"}
    # 对象信封 → 合并
    assert _unwrap_envelope(
        launch, {"arguments": {"app_name": "终端", "extra": 1}}
    ) == {"app_name": "终端", "extra": 1}
    # list 参数（launch_apps.app_names）→ 按分隔符拆列表
    assert _unwrap_envelope(multi, {"arguments": "终端, 文件管理器"}) == {
        "app_names": ["终端", "文件管理器"]
    }
    assert _unwrap_envelope(multi, {"arguments": "终端、vim；firefox"}) == {
        "app_names": ["终端", "vim", "firefox"]
    }
    assert _unwrap_envelope(multi, {"arguments": "终端和控制中心"}) == {
        "app_names": ["终端", "控制中心"]
    }
    # 批量关闭与批量启动共用同一套信封拆分（close_apps.app_names）
    close_multi = registry.get("close_apps").params
    assert _unwrap_envelope(close_multi, {"arguments": "终端和控制中心"}) == {
        "app_names": ["终端", "控制中心"]
    }
    # 无信封 / 工具真带 arguments 参数 → 原样返回
    assert _unwrap_envelope(volume, {"volume": 50}) == {"volume": 50}
    assert _unwrap_envelope({"arguments": {"type": "str"}}, {"arguments": "x"}) == {
        "arguments": "x"
    }


def test_mcp_e2e_envelope_call_not_validation_error():
    """端到端：{arguments: ...} 信封不应再被 FastMCP 以缺参拒绝。"""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bibit_intent.mcp_server"],
            env={**os.environ, "PYTHONPATH": str(
                __import__('pathlib').Path(__file__).resolve().parents[1] / 'src')},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("launch_app", {"arguments": "终端"})
                assert res.isError is False
                text = res.content[0].text if res.content else ""
                assert "validation error" not in text

    asyncio.run(run())


def test_executor_localizes_english_errors():
    _, executor = _build_executor()
    missing = executor.execute("launch_app", {"language": "en"})
    assert missing.need_param == "app_name"
    assert "Missing parameter" in missing.message
    unknown = executor.execute("does_not_exist", {"language": "en"})
    assert "Unknown tool" in unknown.message


class _TextBlock:
    """mcp 的 TextContent 最小替身，避免测试绑定具体 SDK 版本。"""

    def __init__(self, text: str) -> None:
        self.text = text


def test_tool_payload_failure_is_detected_from_the_envelope():
    ok = [_TextBlock(json.dumps({"ok": True, "message": "done"}))]
    failed = [_TextBlock(json.dumps({"ok": False, "message": "参数错误"}))]

    assert _tool_payload_failed(ok) is False
    assert _tool_payload_failed(failed) is True
    # 取不到结论的都按「没失败」处理：非 JSON、没有 ok、空内容
    assert _tool_payload_failed([_TextBlock("$ raw stdout")]) is False
    assert _tool_payload_failed([_TextBlock(json.dumps({"message": "x"}))]) is False
    assert _tool_payload_failed([]) is False
    assert _tool_payload_failed(None) is False


def test_tool_failure_marks_mcp_error_without_touching_the_payload():
    text = json.dumps({"ok": False, "message": "参数错误"}, ensure_ascii=False)
    marked = _mark_tool_failure([_TextBlock(text)])

    if marked is not None and hasattr(marked, "content"):
        assert marked.isError is True
        assert [block.text for block in marked.content] == [text]
    # 成功的调用不该被包一层
    ok = [_TextBlock(json.dumps({"ok": True}))]
    assert _mark_tool_failure(ok) is ok


def test_mcp_e2e_failed_tool_call_reports_is_error():
    """端到端：工具自己判失败时，MCP 结果必须是 isError（Runtime 才能区分）。"""
    pytest.importorskip("mcp.server.fastmcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "bibit_intent.mcp_server"],
            env={**os.environ, "PYTHONPATH": str(
                __import__('pathlib').Path(__file__).resolve().parents[1] / 'src')},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("appcli_verify", {
                    "appcli": "no-such-app", "action": "verify", "path": "/nonexistent/artifact",
                })
                assert res.isError is True
                payload = json.loads(res.content[0].text)
                assert payload["ok"] is False
                assert payload["message"]

    asyncio.run(run())
