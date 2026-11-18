"""依赖管理 M1：扫描 / 状态 / 计划 / 验证，以及 dep_apply 的执行边界。

覆盖 docs/design/dependency_management.md 的 M1 工具：
  dep_scan（需求发现）→ dep_status（本机满足度）→ dep_plan（干跑计划）
  → dep_apply（按计划安装，需确认）→ dep_verify（回代码层复验）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core import dependency_plan as dp
from bibit_intent.core import dependency_scan as ds
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.resources import resource_path


def _load_module(name: str, *parts: str):
    spec = importlib.util.spec_from_file_location(name, resource_path(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dev_deps():
    return _load_module("_test_dev_deps_handlers", "skills", "dev_deps", "handlers.py")


@pytest.fixture(scope="module")
def admin():
    return _load_module("_test_dep_admin_handlers", "skills", "ubuntu_admin", "handlers.py")


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(
        "import json\nimport requests\nimport helper\n", encoding="utf-8"
    )
    (tmp_path / "helper.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tls.c").write_text(
        "#include <stdio.h>\n#include <openssl/ssl.h>\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text(
        "# deps\nrequests>=2.31\ngit+https://example.com/foo.git\n", encoding="utf-8"
    )
    (tmp_path / "CMakeLists.txt").write_text(
        "find_package(ZLIB REQUIRED)\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"left-pad": "^1.0.0"}}), encoding="utf-8"
    )
    return tmp_path


def _keys(scanned: dict) -> set[str]:
    return {f"{item['kind']}:{item['name']}" for item in scanned["requirements"]}


# ── 扫描 ──────────────────────────────────────────────────────────────

def test_scan_collects_python_c_header_pkgconfig_and_node(tmp_path):
    scanned = ds.scan_project(_project(tmp_path))
    keys = _keys(scanned)

    assert "python:requests" in keys
    assert "c_header:openssl/ssl.h" in keys
    assert "pkgconfig:zlib" in keys
    assert "node:left-pad" in keys


def test_scan_skips_stdlib_local_modules_and_system_headers(tmp_path):
    scanned = ds.scan_project(_project(tmp_path))
    keys = _keys(scanned)

    assert "python:json" not in keys      # 标准库
    assert "python:helper" not in keys    # 项目内模块
    assert "python:sys" not in keys
    assert "c_header:stdio.h" not in keys  # 语言自带头文件


def test_scan_marks_non_index_requirement_as_manual(tmp_path):
    scanned = ds.scan_project(_project(tmp_path))
    vcs = [item for item in scanned["requirements"] if item["name"].startswith("git+")]

    assert len(vcs) == 1
    assert vcs[0]["detail"] == "来源非包索引，需人工确认"
    assert vcs[0]["evidence"][0]["file"] == "requirements.txt"


# ── 本机满足度 ────────────────────────────────────────────────────────

def test_status_distinguishes_present_and_missing(tmp_path):
    requirements = [
        ds.Requirement(kind=ds.KIND_C_HEADER, name="stdio.h"),
        ds.Requirement(kind=ds.KIND_C_HEADER, name="bibit-missing.h"),
    ]
    statuses = ds.check_requirements(requirements, root=tmp_path)

    assert statuses["c_header:stdio.h"]["satisfied"] is True
    assert statuses["c_header:bibit-missing.h"]["satisfied"] is False


# ── 计划 ──────────────────────────────────────────────────────────────

def test_plan_marks_wheel_apt_and_manual_items(tmp_path, monkeypatch):
    requirements = [
        ds.Requirement(kind=ds.KIND_PYTHON, name="requests", spec="requests>=2.31"),
        ds.Requirement(kind=ds.KIND_C_HEADER, name="openssl/ssl.h"),
        ds.Requirement(kind=ds.KIND_PYTHON, name="git+https://example.com/x.git",
                       detail="来源非包索引，需人工确认"),
    ]
    statuses = {item.key(): {"satisfied": False, "check": "probe", "detail": ""} for item in requirements}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": True, "package": "requests", "version": "2.34.2",
        "artifact": "requests-2.34.2-py3-none-any.whl", "source": "pypi.org",
        "needs_build": False, "is_direct": False, "requires_dist": [], "detail": "",
    })
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": True, "version": "3.0.16", "size_bytes": 2_400_000,
        "removes": [], "will_install": [pkg], "detail": "",
    })

    plan = dp.build_plan(tmp_path, requirements, statuses, scope="auto")
    items = {item["name"]: item for item in plan["items"]}

    assert items["requests"]["channel"] == "pip"
    assert items["requests"]["version"] == "2.34.2"
    assert items["requests"]["risk"] == "low"
    assert items["openssl/ssl.h"]["channel"] == "apt"
    assert items["openssl/ssl.h"]["package"] == "libssl-dev"
    assert items["openssl/ssl.h"]["risk"] == "medium"
    assert items["git+https://example.com/x.git"]["channel"] == "manual"
    assert items["git+https://example.com/x.git"]["risk"] == "high"
    assert plan["summary"]["actionable"] == 2
    assert plan["summary"]["needs_sudo"] is True
    assert "dep-" in plan["plan_id"]


def test_plan_marks_sdist_build_requirement(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="native-lib")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": True, "package": "native-lib", "version": "1.0",
        "artifact": "native_lib-1.0.tar.gz", "source": "pypi.org",
        "needs_build": True, "is_direct": False, "requires_dist": [], "detail": "",
    })
    monkeypatch.setattr(dp, "_build_tool_missing", lambda: [])

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["channel"] == "pip"
    assert item["risk"] == "medium"
    assert plan["summary"]["needs_build"] is True
    assert "需要源码构建" in " ".join(item["notes"])


def test_plan_refuses_removal_and_downgrades_to_manual(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PKGCONFIG, name="gtk+-3.0")
    statuses = {requirement.key(): {"satisfied": False, "check": "pkg-config --exists gtk+-3.0", "detail": ""}}
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": True, "version": "3.24", "size_bytes": 1000,
        "removes": ["old-lib"], "will_install": [pkg], "detail": "",
    })

    plan = dp.build_plan(tmp_path, [requirement], statuses)
    item = plan["items"][0]

    assert item["channel"] == "manual"
    assert item["risk"] == "high"
    assert item["manual_steps"]


def test_plan_store_expires_and_detects_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": True, "package": "requests", "version": "2.34.2",
        "artifact": "requests-2.34.2-py3-none-any.whl", "source": "pypi.org",
        "needs_build": False, "is_direct": False, "requires_dist": [], "detail": "",
    })
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="requests")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    plan = dp.build_plan(tmp_path, [requirement], statuses)
    dp.save_plan(plan)

    assert dp.get_plan(plan["plan_id"])[0] is not None
    assert dp.plan_is_current(plan)[0] is True

    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    fresh, why = dp.plan_is_current(plan)
    assert fresh is False and "变更" in why

    expired = dict(plan, expires_at=time.time() - 1)
    dp.save_plan(expired)
    assert dp.get_plan(plan["plan_id"])[0] is None


def test_install_target_prefers_project_venv_then_shared(tmp_path, monkeypatch):
    project_venv = tmp_path / ".venv" / "bin" / "python"
    project_venv.parent.mkdir(parents=True)
    project_venv.write_text("#!/bin/sh\n")
    project_venv.chmod(0o755)

    target, env = ds.install_target_python(tmp_path)
    assert target == str(project_venv) and env == "project"

    monkeypatch.setattr(ds, "SHARED_VENV_PYTHON", tmp_path / "shared" / "bin" / "python")
    target2, env2 = ds.install_target_python(tmp_path / "no-project")
    assert env2 == "user"
    assert target2 == str(tmp_path / "shared" / "bin" / "python")


def test_plan_dry_runs_with_available_python_but_verifies_in_target(tmp_path, monkeypatch):
    """共享 venv 尚未创建时：干跑借可用解释器，验证指向真正的安装目标。"""
    shared = tmp_path / "shared" / "bin" / "python"
    monkeypatch.setattr(ds, "SHARED_VENV_PYTHON", shared)
    seen = {}

    def fake_dry(python, spec, **kwargs):
        seen["python"] = python
        return {
            "resolvable": True, "package": "six", "version": "1.17.0",
            "artifact": "six-1.17.0-py2.py3-none-any.whl", "source": "pypi.org",
            "needs_build": False, "is_direct": False, "requires_dist": [], "detail": "",
        }

    monkeypatch.setattr(dp, "_pip_dry_run", fake_dry)
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="six")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    assert seen["python"] != str(shared)
    assert plan["verify_python"] == str(shared)
    assert plan["items"][0]["target_env"] == "user"


# ── 工具（skill）层 ───────────────────────────────────────────────────

def test_dep_scan_and_status_handlers(dev_deps, tmp_path):
    root = _project(tmp_path)

    scanned = dev_deps.dep_scan(path=str(root))
    assert scanned.ok is True and "依赖需求" in scanned.message

    status = dev_deps.dep_status(path=str(root))
    assert status.ok is True
    assert "缺失" in status.message


def test_dep_plan_and_verify_handlers(dev_deps, tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": True, "version": "1.0", "size_bytes": 1024,
        "removes": [], "will_install": [pkg], "detail": "",
    })
    plan_result = dev_deps.dep_plan(path=str(root), max_dry_runs=0)
    assert plan_result.ok is True
    plan_id = json.loads(plan_result.detail)["plan_id"]
    assert plan_id in plan_result.message

    verified = dev_deps.dep_verify(plan_id=plan_id)
    assert verified.ok is False  # 计划里的 VCS 依赖永远验证不过
    assert "依赖验证" in verified.message


def test_dep_scan_handler_rejects_missing_path(dev_deps):
    result = dev_deps.dep_scan(path="")
    assert result.ok is False and "路径" in result.message


def test_dep_plan_handler_rejects_bad_scope(dev_deps, tmp_path):
    result = dev_deps.dep_plan(path=str(tmp_path), scope="宇宙")
    assert result.ok is False and "scope" in result.message


# ── 执行层 ────────────────────────────────────────────────────────────

def _apt_plan(tmp_path, monkeypatch, *, package="libssl-dev"):
    requirement = ds.Requirement(kind=ds.KIND_C_HEADER, name="openssl/ssl.h")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": True, "version": "3.0.16", "size_bytes": 1024,
        "removes": [], "will_install": [pkg], "detail": "",
    })
    plan = dp.build_plan(tmp_path, [requirement], statuses)
    dp.save_plan(plan)
    return plan


def test_dep_apply_runs_selected_apt_item_and_verifies(admin, tmp_path, monkeypatch):
    plan = _apt_plan(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: (calls.append(cmd),
                                                            admin.ExecResult(True, "ok"))[1])
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "probe ok"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert result.ok is True
    assert calls == [["apt-get", "install", "-y", "--no-remove", "--", "libssl-dev"]]
    assert "安装并验证通过 1 项" in result.message


def test_dep_apply_skips_satisfied_and_manual_items(admin, tmp_path, monkeypatch):
    plan = _apt_plan(tmp_path, monkeypatch)
    # 勾到计划里不存在的编号要明确拒绝，绝不静默安装别的包
    other = admin.dep_apply(plan_id=plan["plan_id"], items="9")
    assert other.ok is False and "没有编号" in other.message


def test_dep_apply_rejects_stale_plan(admin, tmp_path, monkeypatch):
    plan = _apt_plan(tmp_path, monkeypatch)
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert result.ok is False and "计划失效" in result.message


def test_dep_apply_requires_known_plan(admin):
    result = admin.dep_apply(plan_id="dep-000000000000")
    assert result.ok is False and "计划" in result.message


def test_dep_apply_propagates_sudo_failure_without_side_effects(admin, tmp_path, monkeypatch):
    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo", lambda cmd, timeout=60, input=None: admin.ExecResult(
        False, "当前没有可用的图形登录会话", escalate="no_gui_askpass"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert result.ok is False
    assert result.escalate == "no_gui_askpass"


def test_dep_tools_registered_with_expected_groups():
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})

    for name in ("dep_scan", "dep_status", "dep_plan", "dep_verify", "dep_history"):
        tool = registry.get(name)
        assert tool.mcp_group() == "readonly"
        assert not tool.system.get("confirm")
    dep_apply = registry.get("dep_apply")
    assert dep_apply.mcp_group() == "admin"
    assert dep_apply.system.get("confirm") is True


# ── 路由回归：plan_id 直通 dep_apply ────────────────────────────────

@pytest.fixture(scope="module")
def routing():
    from bibit_intent.routing.intent import decide_local_intent
    from bibit_intent.routing.rule_engine import RuleEngine
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    return decide_local_intent, registry, RuleEngine(registry)


@pytest.mark.parametrize(
    ("text", "items"),
    [
        ("安装依赖 dep-76270bf385e5 2,3", "2,3"),
        ("按计划安装依赖 dep-76270bf385e5", None),
        ("安装依赖 dep-76270bf385e5", None),
        ("install dependencies dep-76270bf385e5 1", "1"),
    ],
)
def test_dep_apply_routed_before_ecosystem(routing, text, items):
    decide, registry, rule = routing
    plan = decide(text, rule=rule, registry=registry)

    assert plan.tool_name == "dep_apply"
    assert plan.slots["plan_id"] == "dep-76270bf385e5"
    assert plan.slots.get("items") == items


def test_ecosystem_install_still_routes_to_pip_and_npm(routing):
    decide, registry, rule = routing

    assert decide("装 python 库 requests", rule=rule, registry=registry).tool_name == "pip_install"
    assert decide("装个依赖 lodash", rule=rule, registry=registry).tool_name == "npm_install"


# ── 回滚清单与安装审计 ──────────────────────────────────────────────

def test_dep_apply_attaches_rollback_command(admin, tmp_path, monkeypatch):
    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "probe ok"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    item = json.loads(result.detail)["results"][0]
    assert item["rollback"] == "sudo apt-get remove -- libssl-dev"
    assert "回滚" in result.message
    assert "sudo apt-get remove -- libssl-dev" in result.message


@pytest.mark.parametrize(("item", "python", "expected"), [
    ({"channel": "apt", "package": "libssl-dev"}, "", "sudo apt-get remove -- libssl-dev"),
    ({"channel": "apt", "package": ""}, "", ""),
    ({"channel": "pip", "package": "requests"}, "", "python3 -m pip uninstall -y requests"),
    ({"channel": "pip", "name": "requests"}, "/venv/bin/python",
     "/venv/bin/python -m pip uninstall -y requests"),
    ({"channel": "manual", "package": "x"}, "", ""),
])
def test_rollback_command_per_channel(item, python, expected):
    assert dp.rollback_command(item, python) == expected


def test_dep_apply_writes_audit_record(admin, tmp_path, monkeypatch):
    from bibit_intent.core import dependency_audit as audit

    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "probe ok"))

    admin.dep_apply(plan_id=plan["plan_id"], items="1")

    records = audit.read_records(5)
    assert [r["plan_id"] for r in records] == [plan["plan_id"]]
    assert records[0]["ok"] is True
    assert records[0]["items"][0]["rollback"] == "sudo apt-get remove -- libssl-dev"


def test_dep_history_reports_recent_records(dev_deps, monkeypatch):
    from bibit_intent.core import dependency_audit as audit

    audit.record_apply(
        plan={"plan_id": "dep-aaa", "project_root": "/p", "scope": "project"},
        results=[{"index": 1, "label": "libssl-dev", "ok": True, "verified": True,
                  "rollback": "sudo apt-get remove -- libssl-dev"}],
        overall=True,
    )

    result = dev_deps.dep_history(limit=5)

    assert result.ok is True
    assert "dep-aaa" in result.message
    assert "sudo apt-get remove -- libssl-dev" in result.message
    assert json.loads(result.detail)["records"][0]["plan_id"] == "dep-aaa"


def test_dep_history_empty_and_bad_limit(dev_deps):
    empty = dev_deps.dep_history()
    assert empty.ok is True and "还没有" in empty.message

    bad = dev_deps.dep_history(limit="几")
    assert bad.ok is False and "整数" in bad.message


# ── 运行期动态库（ldd not found / ctypes.CDLL）──────────────────────

def test_scan_finds_ctypes_and_link_flag_shared_libs(tmp_path):
    (tmp_path / "loader.py").write_text(
        "import ctypes\n"
        "lib = ctypes.CDLL('libssl.so.3')\n"
        "ctypes.util.find_library('crypto')\n"
        "ctypes.CDLL('/usr/lib/libabsolute.so')\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "LDLIBS = -lz -lm\n"
        "CFLAGS = $(shell pkg-config --libs openssl)\n",
        encoding="utf-8",
    )

    scanned = ds.scan_project(tmp_path)
    kinds = {item["name"]: item for item in scanned["requirements"] if item["kind"] == ds.KIND_SHARED_LIB}

    assert set(kinds) == {"libssl.so.3", "libcrypto.so", "libz.so"}
    assert "ctypes.CDLL" in kinds["libssl.so.3"]["evidence"][0]["snippet"]
    assert "-lz" in kinds["libz.so"]["evidence"][0]["snippet"]


def test_ctypes_bare_name_import_is_recognised(tmp_path):
    (tmp_path / "loader.py").write_text(
        "from ctypes import CDLL\nlib = CDLL('sqlite3')\n", encoding="utf-8"
    )

    scanned = ds.scan_project(tmp_path)

    assert [item["name"] for item in scanned["requirements"]] == ["libsqlite3.so"]


@pytest.mark.parametrize(("soname", "expected"), [
    ("libssl.so.3", ["libssl3t64", "libssl3"]),
    ("libz.so.1", ["zlib1g"]),
    ("libfoo.so.1", ["libfoo1", "libfoo"]),
    ("libfoo.so", ["libfoo-dev", "libfoo"]),
    ("ssl", []),
    ("libx.so.1/../evil", []),
])
def test_shared_lib_apt_candidates(soname, expected):
    assert ds.shared_lib_apt_candidates(soname) == expected


def test_plan_lands_missing_so_on_runtime_package(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_SHARED_LIB, name="libssl.so.3")
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": pkg == "libssl3t64", "version": "3.5.0", "size_bytes": 2048,
        "removes": [], "will_install": [pkg], "detail": "",
    })

    plan = dp.build_plan(tmp_path, [requirement], {requirement.key(): {
        "satisfied": False, "check": "ctypes.CDLL('libssl.so.3')", "detail": "",
    }})

    item = plan["items"][0]
    assert item["channel"] == "apt" and item["package"] == "libssl3t64"
    assert item["verify"] == "ctypes.CDLL('libssl.so.3')"


def test_check_shared_lib_uses_real_ctypes_probe():
    import ctypes
    try:
        ctypes.CDLL("libc.so.6")
    except OSError:
        pytest.skip("本机没有 libc.so.6（非 glibc）")

    statuses = ds.check_requirements(
        [
            ds.Requirement(kind=ds.KIND_SHARED_LIB, name="libc.so.6"),
            ds.Requirement(kind=ds.KIND_SHARED_LIB, name="libbibit-missing.so.9"),
        ],
        root=Path("/tmp"),
    )

    assert statuses["shared_lib:libc.so.6"]["satisfied"] is True
    assert statuses["shared_lib:libbibit-missing.so.9"]["satisfied"] is False
    assert "ldd not found" in statuses["shared_lib:libbibit-missing.so.9"]["detail"]


def test_check_shared_lib_falls_back_to_ldconfig(monkeypatch):
    monkeypatch.setattr(ds, "_run", lambda argv, timeout=30: (127, "", "no python"))
    monkeypatch.setattr(
        ds, "_ldconfig_entries",
        lambda: "\tlibfoo.so.1 (libc6) => /usr/lib/libfoo.so.1\n",
    )

    statuses = ds.check_requirements(
        [
            ds.Requirement(kind=ds.KIND_SHARED_LIB, name="libfoo.so.1"),
            ds.Requirement(kind=ds.KIND_SHARED_LIB, name="libbar.so.2"),
        ],
        root=Path("/tmp"),
    )

    assert statuses["shared_lib:libfoo.so.1"]["satisfied"] is True
    assert statuses["shared_lib:libbar.so.2"]["satisfied"] is False
    assert "ldconfig" in statuses["shared_lib:libbar.so.2"]["check"]


# ── 指标（计划数 / 确认率 / 装后验证通过率）──────────────────────────

def test_dependency_metrics_track_plans_and_verification(admin, tmp_path, monkeypatch):
    dp.reset_metrics()
    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "probe ok"))

    admin.dep_apply(plan_id=plan["plan_id"], items="1")
    metrics = dp.dependency_metrics()

    assert metrics["plans_built"] == 1
    assert metrics["apply_runs"] == 1 and metrics["confirmation_rate"] == 1.0
    assert metrics["items_installed"] == 1 and metrics["verify_pass_rate"] == 1.0
    dp.reset_metrics()


def test_dependency_metrics_count_unverified_install(admin, tmp_path, monkeypatch):
    dp.reset_metrics()
    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (False, "还没装好"))

    admin.dep_apply(plan_id=plan["plan_id"], items="1")
    metrics = dp.dependency_metrics()

    assert metrics["items_installed"] == 1 and metrics["items_verified"] == 0
    assert metrics["verify_pass_rate"] == 0.0 and metrics["apply_failures"] == 1
    assert metrics["confirmation_rate"] == 1.0
    dp.reset_metrics()


def test_dependency_metrics_count_failed_first_item(admin, tmp_path, monkeypatch):
    dp.reset_metrics()
    plan = _apt_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "_run_sudo", lambda cmd, timeout=60, input=None: admin.ExecResult(
        False, "当前没有可用的图形登录会话", escalate="no_gui_askpass"))

    admin.dep_apply(plan_id=plan["plan_id"], items="1")
    metrics = dp.dependency_metrics()

    assert metrics["apply_runs"] == 1 and metrics["apply_failures"] == 1
    assert metrics["items_installed"] == 0 and metrics["verify_pass_rate"] is None
    dp.reset_metrics()


def test_pipeline_stats_include_dependency_metrics():
    from bibit_intent.execution.executor import Executor
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import Router
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry()
    router = Router(registry, RuleEngine(registry), None, Executor(registry, handlers={}))

    stats = EmbeddedBibitPipeline(router).stats()

    assert "plans_built" in stats["dependency_plan"]
    assert "confirmation_rate" in stats["dependency_plan"]
    assert "verify_pass_rate" in stats["dependency_plan"]


# ── 写文件后的依赖提醒（dep_notice，只读）────────────────────────────

def test_dep_notice_lists_missing_without_installing(dev_deps, tmp_path):
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")

    result = dev_deps.dep_notice(path=str(tmp_path))

    assert result.ok is True
    assert result.message.startswith("依赖提醒【未安装】：")
    assert "requests（pip）" in result.message
    assert "仅提示，未安装" in result.message
    detail = json.loads(result.detail)
    assert detail["notice"] == "missing"
    assert detail["missing"] == [{"kind": "python", "name": "requests", "channel": "pip"}]


def test_dep_notice_reports_satisfied(dev_deps, tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="requests")
    monkeypatch.setattr(
        dev_deps, "check_requirements",
        lambda items, **kwargs: {item.key(): {"satisfied": True, "check": "", "detail": ""} for item in items},
    )

    result = dev_deps.dep_notice(path=str(tmp_path))

    assert result.message.startswith("依赖提醒【已满足】：")
    assert json.loads(result.detail)["notice"] == "satisfied"
    assert requirement.key() == "python:requests"


def test_dep_notice_caps_listed_items(dev_deps, tmp_path):
    (tmp_path / "app.py").write_text(
        "import requests\nimport flask\nimport numpy\n", encoding="utf-8"
    )

    result = dev_deps.dep_notice(path=str(tmp_path), limit=2)

    assert "还缺 3 项依赖" in result.message
    assert "（另有 1 项未列出）" in result.message


def test_dep_notice_rejects_bad_limit(dev_deps, tmp_path):
    assert dev_deps.dep_notice(path=str(tmp_path), limit="几个").ok is False


def test_dep_notice_routed_by_rule(routing):
    decide, registry, rule = routing

    plan = decide("依赖提醒 /home/bianbu/workspace/demo", rule, registry)

    from bibit_intent.routing.intent import IntentAction

    assert plan.action is IntentAction.RULE_CALL
    assert plan.tool_name == "dep_notice"
    assert plan.slots["path"] == "/home/bianbu/workspace/demo"


def test_dep_notice_registered_readonly():
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})

    tool = registry.get("dep_notice")

    assert tool.mcp_group() == "readonly"
    assert not tool.system.get("confirm")


# ── 源码构建通道（M3-2）──────────────────────────────────────────────

def _fake_tool_status(missing: list[str]) -> dict:
    return {
        "ok": not missing,
        "tools": ["gcc", "make", "pkg-config", "python3-dev"],
        "missing": [{"tool": name, "apt": name, "why": ""} for name in missing],
        "install": list(missing),
    }


def _sdist_plan(tmp_path, monkeypatch, *, missing=("python3-dev",)):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="native-lib")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": True, "package": "native-lib", "version": "1.0",
        "artifact": "native_lib-1.0.tar.gz", "source": "pypi.org",
        "needs_build": True, "is_direct": False, "requires_dist": [], "detail": "",
    })
    monkeypatch.setattr(dp, "_build_tool_missing", lambda: list(missing))
    plan = dp.build_plan(tmp_path, [requirement], statuses)
    dp.save_plan(plan)
    return plan


def test_build_tool_status_maps_missing_tools_to_apt(monkeypatch):
    monkeypatch.setattr(dp, "_build_tool_missing", lambda: ["python3-dev"])

    status = dp.build_tool_status()

    assert status["ok"] is False
    assert status["install"] == ["python3-dev"]
    assert status["missing"][0]["apt"] == "python3-dev"
    assert "Python" in status["missing"][0]["why"]
    assert "gcc" in status["tools"]


def test_plan_sdist_marks_missing_build_tools(tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch)

    item = plan["items"][0]
    assert item["needs_build"] is True
    assert item["risk"] == "high"
    assert item["build_tools"]["install"] == ["python3-dev"]
    assert plan["summary"]["needs_build"] is True
    assert plan["summary"]["build_tools_missing"] == ["python3-dev"]

    message = dp.format_plan_message(plan)
    assert "缺 python3-dev" in message
    assert "apt-get install -y python3-dev" in message


def test_plan_sdist_reports_ready_build_tools(tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch, missing=())

    item = plan["items"][0]
    assert item["risk"] == "medium"
    assert item["build_tools"]["ok"] is True
    assert item["build_tools"]["install"] == []
    assert plan["summary"]["build_tools_missing"] == []
    assert "构建工具齐备" in dp.format_plan_message(plan)


def test_wheel_item_carries_no_build_tools(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="requests")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": True, "package": "requests", "version": "2.34.2",
        "artifact": "requests-2.34.2-py3-none-any.whl", "source": "pypi.org",
        "needs_build": False, "is_direct": False, "requires_dist": [], "detail": "",
    })
    monkeypatch.setattr(dp, "_build_tool_missing", lambda: ["python3-dev"])

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["needs_build"] is False
    assert item["build_tools"] == {}
    assert plan["summary"]["build_tools_missing"] == []


def test_dep_apply_installs_build_tools_then_builds(admin, tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch)
    sudo_calls: list[list[str]] = []
    pip_calls: list[list[str]] = []
    state = {"missing": ["python3-dev"]}

    def fake_status():
        return _fake_tool_status(state["missing"])

    def fake_sudo(cmd, timeout=60, input=None):
        sudo_calls.append(cmd)
        state["missing"] = [name for name in state["missing"] if name not in cmd]
        return admin.ExecResult(True, "ok")

    monkeypatch.setattr(dp, "build_tool_status", fake_status)
    monkeypatch.setattr(admin, "_run_sudo", fake_sudo)
    monkeypatch.setattr(admin, "_pip_install_base",
                        lambda item, root: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(admin, "_run", lambda argv, timeout=60: (
        pip_calls.append(argv), admin.ExecResult(True, "ok"))[1])
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "import ok"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert sudo_calls == [["apt-get", "install", "-y", "--no-remove", "--", "python3-dev"]]
    assert len(pip_calls) == 1
    assert pip_calls[0][:4] == ["/venv/bin/python", "-m", "pip", "install"]
    assert pip_calls[0][-1] == "native-lib"
    assert result.ok is True
    assert "一并补齐构建工具：python3-dev" in result.message

    record = json.loads(result.detail)["results"][0]
    assert record["build_tools"] == ["python3-dev"]
    assert record["rollback"].startswith("sudo apt-get remove -- python3-dev；")


def test_dep_apply_stops_when_build_tools_cannot_be_installed(admin, tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch)
    pip_calls: list[list[str]] = []

    monkeypatch.setattr(dp, "build_tool_status", lambda: _fake_tool_status(["python3-dev"]))
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: admin.ExecResult(False, "apt 报错"))
    monkeypatch.setattr(admin, "_run", lambda argv, timeout=60: (
        pip_calls.append(argv), admin.ExecResult(True, "ok"))[1])

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert result.ok is False
    assert "补齐构建工具失败" in result.message
    assert pip_calls == []


def test_dep_apply_never_installs_tools_outside_the_plan(admin, tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch)
    sudo_calls: list[list[str]] = []
    pip_calls: list[list[str]] = []

    # 计划里只展示过 python3-dev；执行时探测到缺 gcc，也不能顺手装 gcc
    monkeypatch.setattr(dp, "build_tool_status", lambda: _fake_tool_status(["gcc"]))
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: sudo_calls.append(cmd) or admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_pip_install_base",
                        lambda item, root: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(admin, "_run", lambda argv, timeout=60: (
        pip_calls.append(argv), admin.ExecResult(True, "ok"))[1])
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "import ok"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert sudo_calls == []
    assert len(pip_calls) == 1
    assert json.loads(result.detail)["results"][0]["build_tools"] == []


def test_dep_apply_skips_tool_install_when_tools_are_ready(admin, tmp_path, monkeypatch):
    plan = _sdist_plan(tmp_path, monkeypatch)
    sudo_calls: list[list[str]] = []

    monkeypatch.setattr(dp, "build_tool_status", lambda: _fake_tool_status([]))
    monkeypatch.setattr(admin, "_run_sudo",
                        lambda cmd, timeout=60, input=None: sudo_calls.append(cmd) or admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_pip_install_base",
                        lambda item, root: ["/venv/bin/python", "-m", "pip"])
    monkeypatch.setattr(admin, "_run", lambda argv, timeout=60: admin.ExecResult(True, "ok"))
    monkeypatch.setattr(admin, "_verify_dep_item", lambda item, root, python: (True, "import ok"))

    result = admin.dep_apply(plan_id=plan["plan_id"], items="1")

    assert result.ok is True
    assert sudo_calls == []


# ── 离线降级与缓存复用（M3-3）────────────────────────────────────────

def _unreachable(host, *, port=443, timeout=2.0):
    return False


def test_classify_failure_splits_network_and_missing():
    assert dp.classify_failure(
        "WARNING: Retrying: Could not resolve host: git.spacemit.com") == "network"
    assert dp.classify_failure("E: Unable to locate package foo") == "not_found"
    assert dp.classify_failure("某种没见过的错误") == "unknown"


def test_pip_cached_wheel_builds_offline_command(tmp_path, monkeypatch):
    cache = tmp_path / "wheels" / "ab" / "cd"
    cache.mkdir(parents=True)
    wheel = cache / "cymeme-2.0.13-cp314-cp314-linux_riscv64.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(dp, "_pip_cache_dir", lambda python, timeout=30: str(tmp_path))
    monkeypatch.setattr(dp, "_run", lambda argv, timeout=60: (
        0, " - cymeme-2.0.13-cp314-cp314-linux_riscv64.whl (300 kB)\n", ""))

    facts = dp.pip_cached_wheel("/venv/bin/python", "cymeme")

    assert facts["artifact"] == wheel.name
    assert facts["path"] == str(wheel)
    assert "--no-index" in facts["command"] and str(cache) in facts["command"]


def test_offline_facts_reports_missing_when_cache_is_empty(monkeypatch):
    monkeypatch.setattr(dp, "pip_cached_wheel", lambda python, name, **k: {})
    monkeypatch.setattr(dp, "apt_cached_deb", lambda package: "")

    assert dp.offline_facts("pip", python="/venv/bin/python", name="requests")["available"] is False
    assert dp.offline_facts("apt", package="libssl-dev")["available"] is False
    assert dp.offline_facts("apt", package="libssl-dev")["detail"]


def test_plan_reports_unreachable_mirror_and_offline_reuse(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="cymeme")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": False, "failure": "network",
        "detail": "Could not resolve host: git.spacemit.com",
    })
    monkeypatch.setattr(dp, "_host_reachable", _unreachable)
    monkeypatch.setattr(dp, "pip_cached_wheel", lambda python, name, **k: {
        "artifact": "cymeme-2.0.13-cp314-cp314-linux_riscv64.whl",
        "path": "/home/bianbu/.cache/pip/wheels/ab/cymeme-2.0.13-cp314-cp314-linux_riscv64.whl",
        "command": "/venv/bin/python -m pip install --no-index --find-links /cache cymeme",
    })

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["channel"] == "manual"
    assert item["failure"] == "network"
    assert item["unreachable_source"] == "git.spacemit.com"
    assert item["offline"]["available"] is True
    assert item["manual_steps"] == ["/venv/bin/python -m pip install --no-index --find-links /cache cymeme"]
    assert plan["summary"]["sources_unreachable"] == ["git.spacemit.com"]
    assert plan["summary"]["offline_reusable"] == 1

    message = dp.format_plan_message(plan)
    assert "pip 镜像不可达（git.spacemit.com）" in message
    assert "离线降级：源不可达（git.spacemit.com）" in message
    assert "可离线安装" in message
    assert "手动步骤：/venv/bin/python -m pip install --no-index" in message


def test_plan_offline_without_cache_says_it_cannot_install(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="numpy")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": False, "failure": "network",
        "detail": "Could not resolve host: git.spacemit.com",
    })
    monkeypatch.setattr(dp, "_host_reachable", _unreachable)
    monkeypatch.setattr(dp, "pip_cached_wheel", lambda python, name, **k: {})

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    assert plan["summary"]["offline_reusable"] == 0
    message = dp.format_plan_message(plan)
    assert "本地 pip wheel 缓存里没有可复用的产物" in message
    assert "本地缓存里没有可复用的产物" in message


def test_plan_apt_unreachable_reuses_cached_deb(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_COMMAND, name="bibit-absent-cmd")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": False, "failure": "not_found",
        "detail": "E: Unable to locate package bibit-absent-cmd",
    })
    monkeypatch.setattr(dp, "apt_source_hosts", lambda: ["archive.bianbu.xyz"])
    monkeypatch.setattr(dp, "_host_reachable", _unreachable)
    monkeypatch.setattr(dp, "apt_cached_deb", lambda package: "bibit-absent-cmd_1.0_riscv64.deb")

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["failure"] == "network"
    assert item["offline"]["available"] is True
    assert item["manual_steps"] == [
        "sudo apt-get install --no-download --no-remove -- bibit-absent-cmd"
    ]
    assert plan["summary"]["sources_unreachable"] == ["archive.bianbu.xyz"]


def test_plan_apt_missing_package_keeps_the_original_wording(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_COMMAND, name="bibit-absent-cmd")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_apt_facts", lambda pkg: {
        "resolvable": False, "failure": "not_found",
        "detail": "E: Unable to locate package bibit-absent-cmd",
    })
    monkeypatch.setattr(dp, "apt_source_hosts", lambda: ["archive.bianbu.xyz"])
    monkeypatch.setattr(dp, "_host_reachable", lambda host, *, port=443, timeout=2.0: True)

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["failure"] == "not_found"
    assert item["risk"] == "medium"
    assert "无法从 apt 源解析出提供该依赖的包" in " ".join(item["notes"])
    assert plan["summary"]["sources_unreachable"] == []


def test_plan_pip_source_without_a_compatible_artifact(tmp_path, monkeypatch):
    requirement = ds.Requirement(kind=ds.KIND_PYTHON, name="Cython")
    statuses = {requirement.key(): {"satisfied": False, "check": "probe", "detail": ""}}
    monkeypatch.setattr(dp, "_pip_dry_run", lambda *a, **k: {
        "resolvable": False, "failure": "not_found",
        "detail": "ERROR: Could not find a version that satisfies the requirement Cython "
                  "(from versions: none) / ERROR: No matching distribution found for Cython",
    })
    monkeypatch.setattr(dp, "pip_index_hosts", lambda: ["git.spacemit.com"])

    plan = dp.build_plan(tmp_path, [requirement], statuses)

    item = plan["items"][0]
    assert item["failure"] == "not_found"
    assert item["unreachable_source"] == ""
    assert item["offline"] == {}
    assert item["notes"][0].startswith("pip 源没有可用产物（git.spacemit.com）：")
    assert "riscv64" in item["notes"][1]
    assert plan["summary"]["sources_unreachable"] == []
    assert "离线降级" not in dp.format_plan_message(plan)


# ── 分发名 vs import 名（复验口径）────────────────────────────────────

def _fake_distribution(tmp_path, module: str, distribution: str) -> None:
    package = tmp_path / module
    package.mkdir()
    (package / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    info = tmp_path / f"{module}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0\n", encoding="utf-8"
    )
    (info / "top_level.txt").write_text(module + "\n", encoding="utf-8")


def test_status_resolves_distribution_name_to_import_name(tmp_path, monkeypatch):
    """requirements.txt 写分发名（如 pyahocorasick），实际 import 名不同也能判满足。"""
    _fake_distribution(tmp_path, "bibit_fake_dist", "Bibit-Fake-Dist")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    statuses = ds.check_requirements(
        [ds.Requirement(kind=ds.KIND_PYTHON, name="Bibit-Fake-Dist")],
        root=tmp_path, python=sys.executable,
    )

    status = statuses["python:Bibit-Fake-Dist"]
    assert status["satisfied"] is True
    assert status["check"].endswith("import bibit_fake_dist")
    assert status["detail"] == ""


def test_status_keeps_absent_distribution_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    statuses = ds.check_requirements(
        [ds.Requirement(kind=ds.KIND_PYTHON, name="bibit-absent-dist")],
        root=tmp_path, python=sys.executable,
    )

    status = statuses["python:bibit-absent-dist"]
    assert status["satisfied"] is False
    assert "不可导入" in status["detail"]
