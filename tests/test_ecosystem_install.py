"""阶段六回归：安装工具面按生态拆分（apt / pip / npm / 本地文件）。

覆盖：
  1. detect_ecosystem 对 pip/npm/apt 的识别边界（“安装 python3-pip”不误判）；
  2. decide_local_intent 生态路由（“装 python 库 requests”→ pip_install，
     且绝不再误入 pkg_install）；
  3. 新工具注册、mcp 分组（admin）与 install_family 自动描述提示；
  4. pip/npm/install_local handler 的参数校验、作用域与格式分发。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import HANDLERS as BUILTIN_HANDLERS
from bibit_intent.resources import resource_path
from bibit_intent.routing.rule_engine import RuleEngine
from bibit_intent.routing.intent import decide_local_intent


def _load_skill_handlers():
    spec = importlib.util.spec_from_file_location(
        "_test_ubuntu_admin_handlers",
        resource_path("skills") / "ubuntu_admin" / "handlers.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry():
    reg = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), reg, {})
    return reg


@pytest.fixture(scope="module")
def rule(registry):
    return RuleEngine(registry)


# ── detect_ecosystem ────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pip install requests", "pip"),
        ("pip 装 flask", "pip"),
        ("装 python 库 requests", "pip"),
        ("帮我装个库 numpy", "pip"),
        ("卸载 python 库 requests", "pip"),
        ("pip uninstall requests", "pip"),
        ("npm install axios", "npm"),
        ("npm 装 axios", "npm"),
        ("npm remove axios", "npm"),
        ("卸载 npm 包 axios", "npm"),
        ("装个依赖 lodash", "npm"),
        ("安装 nginx", None),
        ("安装 python3-pip", None),          # python 是包名一部分，不是生态提示词
        ("please install vim", None),
        ("帮我把声音调大", None),
        ("", None),
    ],
)
def test_detect_ecosystem(text, expected):
    from bibit_intent.routing.pkg_resolver import detect_ecosystem
    assert detect_ecosystem(text) == expected


# ── decide_local_intent 生态路由 ────────────────────────────────────

@pytest.mark.parametrize(
    ("text", "tool", "pkg"),
    [
        ("装 python 库 requests", "pip_install", "requests"),
        ("pip install requests", "pip_install", "requests"),
        ("pip 装 flask", "pip_install", "flask"),
        ("卸载 python 库 requests", "pip_uninstall", "requests"),
        ("npm install axios", "npm_install", "axios"),
        ("npm remove axios", "npm_uninstall", "axios"),
        ("帮我装个依赖 lodash", "npm_install", "lodash"),
    ],
)
def test_ecosystem_routes_to_pip_npm(rule, registry, text, tool, pkg):
    plan = decide_local_intent(text, rule, registry, embed=None)
    assert plan.action.value == "rule_call", f"{text!r} -> {plan.action.value}"
    assert plan.tool_name == tool, f"{text!r} -> {plan.tool_name}"
    assert plan.slots.get("pkg") == pkg


@pytest.mark.parametrize(
    "text",
    ["安装 python3-pip", "安装 nginx", "please install vim", "卸载软件 nginx"],
)
def test_apt_install_not_hijacked_by_ecosystem(rule, registry, text):
    plan = decide_local_intent(text, rule, registry, embed=None)
    assert plan.tool_name in ("pkg_install", "pkg_remove"), (
        f"{text!r} 应保持 apt 路径，实际 {plan.tool_name}"
    )


def test_install_library_never_goes_to_pkg_install(rule, registry):
    for text in ["装 python 库 requests", "npm install axios", "pip 装 flask"]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.tool_name != "pkg_install", f"{text!r} 误入 pkg_install"
        assert plan.tool_name in ("pip_install", "npm_install")


def test_ecosystem_without_package_name_escalates(rule, registry):
    plan = decide_local_intent("装个库", rule, registry, embed=None)
    assert plan.action.value == "escalate"
    assert plan.reason == "ecosystem_online"


# ── 本地文件安装（"安装test.deb"）前置路由 ──────────────────────────

@pytest.mark.parametrize(
    ("text", "path", "dir"),
    [
        ("安装test.deb", "test.deb", None),
        ("安装 test.deb", "test.deb", None),
        ("帮我安装 test.deb", "test.deb", None),
        ("安装 /tmp/app.deb", "/tmp/app.deb", None),
        ("安装 ./pkg.tgz", "./pkg.tgz", None),
        ("安装 node-v20.tar.gz", "node-v20.tar.gz", None),
        ("install app.deb", "app.deb", None),
        ("install local app.deb", "app.deb", None),
        ("please install ./app.deb", "./app.deb", None),
        ("安装本地包 test.deb", "test.deb", None),
        ("在~/workspace目录安装hello.deb", "hello.deb", "~/workspace"),
        ("在 ~/workspace 目录 安装 hello.deb", "hello.deb", "~/workspace"),
        ("在 ~/Downloads 里安装 app.deb", "app.deb", "~/Downloads"),
        ("在/tmp/目录安装hello.deb", "hello.deb", "/tmp/"),
        ("把hello.deb安装到~/workspace", "hello.deb", "~/workspace"),
        ("把 hello.deb 安装到 ~/workspace", "hello.deb", "~/workspace"),
        ("将 app.deb 安装到 /tmp", "app.deb", "/tmp"),
        ("安装 hello.deb 到 ~/workspace", "hello.deb", "~/workspace"),
    ],
)
def test_local_file_install_routes_to_install_local(rule, registry, text, path, dir):
    plan = decide_local_intent(text, rule, registry, embed=None)
    assert plan.action.value == "rule_call", f"{text!r} -> {plan.action.value}"
    assert plan.tool_name == "install_local", f"{text!r} -> {plan.tool_name}"
    assert plan.slots.get("path") == path, f"{text!r} -> {plan.slots}"
    assert plan.slots.get("dir") == dir, f"{text!r} -> {plan.slots}"


def test_apt_pkg_install_not_hijacked_by_local_file_rule(rule, registry):
    for text in ["安装 vim", "please install vim", "安装 python3-pip"]:
        plan = decide_local_intent(text, rule, registry, embed=None)
        assert plan.tool_name == "pkg_install", f"{text!r} -> {plan.tool_name}"


# ── 注册表与 MCP schema ─────────────────────────────────────────────

def test_install_tools_registered_with_admin_group(registry):
    for name in ("pip_install", "pip_uninstall", "npm_install", "npm_uninstall", "install_local"):
        tool = registry.get(name)
        assert tool is not None, name
        assert tool.mcp_group() == "admin", name
        assert tool.system.get("confirm") is (name not in {"pip_install", "npm_install"}), name
        assert tool.rule_patterns(), name          # 规则层可直判
        assert tool.local_signature() is None, name  # 不注入 1B 模型


def test_install_family_hint_in_mcp_description(registry):
    desc_apt = registry.get("pkg_install").mcp_schema()["function"]["description"]
    assert "apt/deb" in desc_apt and "pip_install" in desc_apt
    desc_pip = registry.get("pip_install").mcp_schema()["function"]["description"]
    assert "PyPI" in desc_pip and "pkg_install" in desc_pip
    desc_npm = registry.get("npm_install").mcp_schema()["function"]["description"]
    assert "npm registry" in desc_npm and "pip_install" in desc_npm


@pytest.mark.parametrize("name", ["pkg_install", "pip_install", "npm_install", "install_local", "dep_apply"])
def test_install_descriptions_do_not_authorize_download_only_requests(registry, name):
    description = registry.get(name).mcp_schema()["function"]["description"]
    assert "download-only requests do not authorize installation or repair" in description
    assert "even if the existing package is incomplete" in description


def test_download_wording_does_not_match_install_rule(registry):
    from bibit_intent.routing.rule_engine import RuleEngine

    match = RuleEngine(registry).match("帮我下载zenow")
    assert match is None or match.tool_name not in {"pkg_install", "install_local", "dep_apply"}


# ── handler 行为（参数校验 / 作用域 / 格式分发）──────────────────────

@pytest.fixture
def handlers(monkeypatch):
    module = _load_skill_handlers()
    monkeypatch.setattr(module, "_deb_architecture_error", lambda file: "")
    monkeypatch.setattr(module, "_deb_version_preflight", lambda file, pkg, identity, allow_downgrade=False: (identity.update(package=pkg, version="1.0"), ""))
    monkeypatch.setattr(module, "_pip_downgrade_preflight", lambda pkg, target="", allow_downgrade=False, python="": None)
    monkeypatch.setattr(module, "_npm_downgrade_preflight", lambda pkg, target="", global_install=False, allow_downgrade=False: None)
    monkeypatch.setattr(module, "verify_package", lambda *args: {"ok": True})
    return module


def test_pip_install_default_user_venv(handlers, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok", "installed")

    monkeypatch.setattr(handlers, "_run", fake_run)
    monkeypatch.setattr(handlers, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])

    r = handlers.pip_install(pkg="requests")
    assert r.ok is True
    assert calls == [["/venv/bin/python", "-m", "pip", "install", "-i", handlers._PIP_INDEX_URL, "requests"]]


def test_pip_install_target_requires_existing_dir(handlers, monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run", fake_run)

    missing = handlers.pip_install(pkg="requests", target=str(tmp_path / "nope"))
    assert missing.ok is False and "目标目录不存在" in missing.message

    r = handlers.pip_install(pkg="requests", target=str(tmp_path))
    assert r.ok is True
    assert calls == [
        [sys.executable, "-m", "pip", "install", "-i", handlers._PIP_INDEX_URL,
        "--target", str(tmp_path), "requests"]
    ]


def test_pip_invalid_package_rejected(handlers):
    assert handlers.pip_install(pkg="rm -rf /").ok is False
    assert handlers.pip_install(pkg="").ok is False


def test_package_name_rejects_path_traversal_but_keeps_scoped_npm(handlers):
    # 包名里的 "/" 会让 pip/npm 当作本地路径解析，必须拒绝（作用域包除外）
    assert handlers._safe_package("a/../../tmp/evil") == ""
    assert handlers._safe_package("../evil") == ""
    assert handlers._safe_package("pkg/sub") == ""
    assert handlers._safe_package("./pkg") == ""
    # 合法形式保留
    assert handlers._safe_package("requests[socks]==2.0") == "requests[socks]==2.0"
    assert handlers._safe_package("@babel/core") == "@babel/core"
    assert handlers._safe_package("@scope/pkg-name") == "@scope/pkg-name"


def test_npm_install_user_prefix_and_global(handlers, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(["sudo", "-A"] + cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run", fake_run)
    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)

    handlers.npm_install(pkg="axios")
    assert calls == [["npm", "install", "-g", "--prefix", str(Path.home() / ".local"), "axios"]]

    calls.clear()
    handlers.npm_install(pkg="axios", global_install=True)
    assert calls == [["sudo", "-A", "npm", "install", "-g", "axios"]]


def test_install_local_format_dispatch(handlers, monkeypatch, tmp_path):
    calls: list[tuple[str, list[str]]] = []

    def fake_run(cmd, timeout=60):
        calls.append(("run", cmd))
        return handlers.ExecResult(True, "ok")

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(("sudo", cmd))
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run", fake_run)
    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])

    deb = tmp_path / "app.deb"
    whl = tmp_path / "pkg.whl"
    tgz = tmp_path / "node.tgz"
    txt = tmp_path / "notes.txt"
    for f in (deb, whl, tgz, txt):
        f.write_text("x")

    # .deb 默认走 apt（自动解析依赖），deps=false 才用纯 dpkg
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(
        handlers, "_apt_simulate_install", lambda file: (True, ["libfoo"], "")
    )
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(handlers, "_installed_version", lambda pkg: "1.2.3")

    r = handlers.install_local(path=str(deb))
    assert r.ok is True and "依赖已一并安装：libfoo" in r.message
    assert calls == [("sudo", ["apt-get", "install", "-y", "--no-remove", "--", str(deb)])]

    calls.clear()
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    r = handlers.install_local(path=str(deb), deps=False)
    assert r.ok is True and "无额外依赖" in r.message
    assert calls == [("sudo", ["dpkg", "-i", str(deb)])]

    calls.clear()
    handlers.install_local(path=str(whl))
    assert calls == [
        ("run", ["/venv/bin/python", "-m", "pip", "install", "-i", handlers._PIP_INDEX_URL, str(whl)])
    ]

    calls.clear()
    handlers.install_local(path=str(tgz))
    assert calls == [
        ("run", ["npm", "install", "-g", "--prefix", str(Path.home() / ".local"), str(tgz)])
    ]

    r = handlers.install_local(path=str(txt))
    assert r.ok is False and "不支持的安装格式" in r.message
    assert handlers.install_local(path="/no/such/file.deb").ok is False


def test_install_local_relative_name_with_dir(handlers, monkeypatch, tmp_path):
    """相对文件名 + dir 目录 → 解析到 dir 下文件安装（.deb 相对名场景）。"""
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(handlers, "_installed_version", lambda pkg: "1.0")

    deb = tmp_path / "app.deb"
    deb.write_text("x")

    r = handlers.install_local(path="app.deb", dir=str(tmp_path), deps=False)
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(deb)]]


def test_install_local_missing_relative_returns_hint(handlers):
    """当前目录没有该文件 → 直接失败并提示目录（不做本地多轮回填）。"""
    r = handlers.install_local(path="zz_no_such_pkg.deb")
    assert r.ok is False
    assert "当前目录" in r.message and "没有找到" in r.message
    assert "zz_no_such_pkg.deb" in r.message and "目录" in r.message


def test_install_local_dir_fill_from_user_reply(handlers, monkeypatch, tmp_path):
    """用户补的目录带口语填充词（"在 ~/Downloads 目录"）也能解析。"""
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    r = handlers.install_local(
        path="app.deb",
        dir=f"在 {tmp_path} 目录",
        deps=False,
    )
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(deb)]]
    assert handlers._clean_dir_input(" 放到 ~/Downloads 文件夹 ") == "~/Downloads"


def test_install_local_bad_dir_or_missing_abs(handlers, tmp_path):
    bad = handlers.install_local(path="app.deb", dir=str(tmp_path / "nope"))
    assert bad.ok is False
    assert "目录不存在" in bad.message and "nope" in bad.message
    assert "请提供安装文件" in bad.message

    missing = handlers.install_local(path=str(tmp_path / "no.deb"))
    assert missing.ok is False
    assert "文件不存在" in missing.message and "no.deb" in missing.message


def test_install_local_dpkg_dependency_failure_hints_apt(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")

    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])

    def fake_sudo_fail(cmd, timeout=60, on_output=None):
        return handlers.ExecResult(
            False, "命令失败(码1)", "dpkg: dependency problems prevent configuration"
        )

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo_fail)
    r = handlers.install_local(path=str(deb), deps=False)
    assert r.ok is False and "依赖无法满足" in r.message
    assert "可能已有部分变更" in r.message


def test_install_local_fuzzy_single_version(handlers, monkeypatch, tmp_path):
    """hello.deb 不存在但目录里有 hello_1.0.0.deb → 自动装唯一近似文件。"""
    real = tmp_path / "hello_1.0.0.deb"
    real.write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "hello")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(handlers, "_installed_version", lambda pkg: "1.0.0")

    r = handlers.install_local(path="hello.deb", dir=str(tmp_path), deps=False)
    assert r.ok is True and "hello_1.0.0.deb" in r.message
    assert calls == [["dpkg", "-i", str(real)]]


def test_install_local_fuzzy_multiple_versions_asks(handlers, monkeypatch, tmp_path):
    """多个版本 → 列出全部候选并请用户重新指定完整文件名（不做多轮回填）。"""
    (tmp_path / "hello_1.0.deb").write_text("x")
    (tmp_path / "hello_2.0.deb").write_text("x")
    monkeypatch.setattr(
        handlers, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo"))
    )
    r = handlers.install_local(path="hello.deb", dir=str(tmp_path))
    assert r.ok is False
    assert "多个" in r.message
    assert "hello_1.0.deb" in r.message and "hello_2.0.deb" in r.message
    assert "完整文件名" in r.message


def test_install_local_fuzzy_none_keeps_not_found(handlers, tmp_path):
    r = handlers.install_local(path="hello.deb", dir=str(tmp_path))
    assert r.ok is False
    assert "没有找到" in r.message and "hello.deb" in r.message


def test_install_local_fuzzy_in_cwd(handlers, monkeypatch, tmp_path):
    import os

    (tmp_path / "hello_2.1.deb").write_text("x")
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "hello")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    r = handlers.install_local(path="hello.deb", deps=False)
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(tmp_path / "hello_2.1.deb")]]


def test_install_local_dir_tolerates_trailing_dir_word(handlers, monkeypatch, tmp_path):
    """模型把“…/workspace/目录”整段当目录名传入时，剥掉尾词后仍能命中。"""
    real = tmp_path / "hello_1.0.deb"
    real.write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "hello")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    r = handlers.install_local(
        path="hello.deb",
        dir=f"{tmp_path}/目录",
        deps=False,
    )
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(real)]]


def test_install_local_real_dir_named_目录_wins(handlers, monkeypatch, tmp_path):
    """真实存在名为“目录”的文件夹时按原样目录解析，不误剥。"""
    target = tmp_path / "目录"
    target.mkdir()
    want = target / "hello_1.0.deb"
    want.write_text("x")
    (tmp_path / "hello_2.0.deb").write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "hello")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    r = handlers.install_local(path="hello.deb", dir=str(target), deps=False)
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(want)]]


def test_install_local_fuzzy_middle_contains_different_pkg_refused(handlers, monkeypatch, tmp_path):
    """libhello_1.0.deb 是不同包 → 不自动装，提示未找到并列出相近文件。"""
    (tmp_path / "libhello_1.0.deb").write_text("x")

    def pkg_of(file):
        return file.name.lower().partition("_")[0]

    monkeypatch.setattr(handlers, "_deb_package_name", pkg_of)
    monkeypatch.setattr(
        handlers,
        "_run_sudo",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo")),
    )
    r = handlers.install_local(path="hello.deb", dir=str(tmp_path))
    assert r.ok is False
    assert "没有找到" in r.message and "hello.deb" in r.message
    assert "libhello_1.0.deb" in r.message


def test_install_local_fuzzy_prefix_lookalike_different_pkg_refused(handlers, monkeypatch, tmp_path):
    """目录只有 helloworld_0.1.deb → hello.deb 不得自动装错包，只给建议。"""
    (tmp_path / "helloworld_0.1.deb").write_text("x")

    def pkg_of(file):
        return file.name.lower().partition("_")[0]

    monkeypatch.setattr(handlers, "_deb_package_name", pkg_of)
    monkeypatch.setattr(
        handlers,
        "_run_sudo",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo")),
    )
    r = handlers.install_local(path="hello.deb", dir=str(tmp_path))
    assert r.ok is False
    assert "没有找到" in r.message and "hello.deb" in r.message
    assert "helloworld_0.1.deb" in r.message
    assert "相近" in r.message


def test_install_local_fuzzy_same_pkg_unique_among_lookalikes(handlers, monkeypatch, tmp_path):
    """hello_2.10…（同包）+ helloworld_0.1.deb（不同包）→ 只装同包文件。"""
    want = tmp_path / "hello_2.10-5bb2_riscv64.deb"
    want.write_text("x")
    (tmp_path / "helloworld_0.1.deb").write_text("x")

    def pkg_of(file):
        return file.name.lower().partition("_")[0]

    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", pkg_of)
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    r = handlers.install_local(path="hello.deb", dir=str(tmp_path), deps=False)
    assert r.ok is True
    assert "hello_2.10-5bb2_riscv64.deb" in r.message
    assert "helloworld" not in r.message
    assert calls == [["dpkg", "-i", str(want)]]


def test_install_local_fuzzy_same_pkg_unique_over_lookalike(handlers, monkeypatch, tmp_path):
    """同包文件唯一即装；libhello_x.deb 名称相近但不同包，不作候选。"""
    want = tmp_path / "hello_2.0.deb"
    want.write_text("x")
    (tmp_path / "libhello_x.deb").write_text("x")

    def pkg_of(file):
        return file.name.lower().partition("_")[0]

    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", pkg_of)
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    r = handlers.install_local(path="hello.deb", dir=str(tmp_path), deps=False)
    assert r.ok is True
    assert calls == [["dpkg", "-i", str(want)]]


def test_pkg_identity_parse_rules(handlers):
    """文件名 → 包身份解析：.deb 取 `_` 前段；whl 取发行版字段并归一下划线；
    npm tarball 剥掉以数字开头的尾部版本段。"""
    assert (
        handlers._pkg_name_from_stem("hello_2.10-5bb2_riscv64.deb", ".deb")
        == "hello"
    )
    assert handlers._pkg_name_from_stem("helloworld_0.1.deb", ".deb") == "helloworld"
    assert (
        handlers._pkg_name_from_stem("hello-1.0.0-py3-none-any.whl", ".whl")
        == "hello"
    )
    assert (
        handlers._pkg_name_from_stem("my_pkg-1.0-py3-none-any.whl", ".whl")
        == "my-pkg"
    )
    assert handlers._pkg_name_from_stem("express-4.18.2.tgz", ".tgz") == "express"
    assert handlers._pkg_name_from_stem("my-lib-1.2.3.tgz", ".tgz") == "my-lib"
    assert handlers._pkg_name_from_stem("node-v20.tar.gz", ".tar.gz") == "node-v20"


def test_install_local_fuzzy_whl_same_pkg_unique_installs(handlers, monkeypatch, tmp_path):
    """whl 同包唯一 → 解析后走 pip 安装。"""
    real = tmp_path / "hello-1.0.0-py3-none-any.whl"
    real.write_text("x")
    calls: list[list[str]] = []

    monkeypatch.setattr(handlers, "_user_pip", lambda: ["/venv/bin/python", "-m", "pip"])

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run", fake_run)
    r = handlers.install_local(path="hello.whl", dir=str(tmp_path))
    assert r.ok is True
    assert calls == [["/venv/bin/python", "-m", "pip", "install", "-i", handlers._PIP_INDEX_URL, str(real)]]


def test_install_local_fuzzy_whl_different_pkg_refused(handlers, monkeypatch, tmp_path):
    """whl 同理：只有 helloworld-1.0.0…whl → hello.whl 不自动装，只给建议。"""
    (tmp_path / "helloworld-1.0.0-py3-none-any.whl").write_text("x")
    monkeypatch.setattr(
        handlers,
        "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应执行")),
    )
    r = handlers.install_local(path="hello.whl", dir=str(tmp_path))
    assert r.ok is False
    assert "没有找到" in r.message and "hello.whl" in r.message
    assert "helloworld-1.0.0-py3-none-any.whl" in r.message


def test_install_local_deps_bool_coercion(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(handlers, "_deb_unsatisfied_deps", lambda file: [])
    monkeypatch.setattr(handlers, "_deb_depends", lambda file: [])
    monkeypatch.setattr(
        handlers, "_apt_simulate_install", lambda file: (True, [], "")
    )
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)

    assert handlers.install_local(path=str(deb), deps="false").ok is True
    assert calls == [["dpkg", "-i", str(deb)]]
    calls.clear()
    assert handlers.install_local(path=str(deb), deps="true").ok is True
    assert calls == [["apt-get", "install", "-y", "--no-remove", "--", str(deb)]]


# ── .deb 依赖检测（Depends 解析 / 已装 / 仓库可补 / 两边都没有）───────

class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_deb_depends_parse_groups(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    monkeypatch.setattr(
        handlers,
        "run_command",
        lambda *a, **k: _Proc(0, "libc6 (>= 2.34), foo | bar, baz:amd64 (>= 1.0)"),
    )
    assert handlers._deb_depends(deb) == [["libc6"], ["foo", "bar"], ["baz:amd64"]]


def test_deb_unsatisfied_deps_detection(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    monkeypatch.setattr(
        handlers, "_deb_depends", lambda file: [["libc6"], ["foo", "bar"], ["gone"]]
    )
    monkeypatch.setattr(handlers, "_dep_installed", lambda n: n == "libc6")
    assert handlers._deb_unsatisfied_deps(deb) == ["foo | bar", "gone"]


def test_apt_simulate_parse_new_packages(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    out = (
        "Reading package lists... Done\n"
        "Building dependency tree... Done\n"
        "The following NEW packages will be installed:\n"
        "  libfoo libbar\n"
        "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    monkeypatch.setattr(handlers, "run_command", lambda *a, **k: _Proc(0, out))
    ok, new_deps, detail = handlers._apt_simulate_install(deb)
    assert ok is True and new_deps == ["libfoo", "libbar"] and not detail


def test_apt_simulate_reports_failure_detail(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    monkeypatch.setattr(
        handlers, "run_command", lambda *a, **k: _Proc(100, "", "E: Unable to locate package x")
    )
    ok, new_deps, detail = handlers._apt_simulate_install(deb)
    assert ok is False and "Unable to locate" in detail


def test_install_deb_apt_refuses_when_simulate_fails(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(
        handlers, "_apt_simulate_install", lambda file: (False, [], "E: unmet deps")
    )
    monkeypatch.setattr(
        handlers, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo"))
    )
    r = handlers._install_deb(deb, deps=True)
    assert r.ok is False and "依赖无法满足" in r.message and "未执行安装" in r.message


def test_install_deb_dpkg_refuses_missing_dep(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(
        handlers, "_deb_unsatisfied_deps", lambda file: ["libfoo"]
    )
    monkeypatch.setattr(
        handlers, "_run_sudo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应 sudo"))
    )
    r = handlers._install_deb(deb, deps=False)
    assert r.ok is False and "libfoo" in r.message and "deps=true" in r.message


def test_install_deb_apt_success_with_dep_note(handlers, monkeypatch, tmp_path):
    deb = tmp_path / "app.deb"
    deb.write_text("x")
    calls: list[list[str]] = []

    def fake_sudo(cmd, timeout=60, on_output=None):
        calls.append(cmd)
        return handlers.ExecResult(True, "ok")

    monkeypatch.setattr(handlers, "_run_sudo", fake_sudo)
    monkeypatch.setattr(handlers, "_deb_package_name", lambda file: "testapp")
    monkeypatch.setattr(
        handlers, "_apt_simulate_install", lambda file: (True, ["libfoo", "libbar"], "")
    )
    monkeypatch.setattr(handlers, "_is_installed", lambda pkg: True)
    monkeypatch.setattr(handlers, "_installed_version", lambda pkg: "2.0")
    r = handlers._install_deb(deb, deps=True)
    assert r.ok is True
    assert "依赖已一并安装：libfoo、libbar" in r.message
    assert calls == [["apt-get", "install", "-y", "--no-remove", "--", str(deb)]]


# ── Executor 集成：确认门禁 ─────────────────────────────────────────

@pytest.mark.parametrize("tool_name,package", [("pip_install", "requests"), ("npm_install", "axios")])
def test_executor_install_does_not_require_confirmation(registry, tool_name, package):
    from bibit_intent.execution.system_tools import ExecResult

    calls = []

    def install(**params):
        calls.append(params)
        return ExecResult(True, "mock installed")

    executor = Executor(registry, handlers={tool_name: install}, confirm_callback=None)
    result = executor.execute(tool_name, {"pkg": package})
    assert result.ok is True
    assert len(calls) == 1
    assert calls[0]["pkg"] == package
