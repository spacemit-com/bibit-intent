"""H01：appcli 注册表的加载、校验与 argv 渲染。

对应 docs/design/cli_anything_plan.md 的 H01 验收标准：
字段校验、argv 只吃登记过的键、非法动作/未知 appcli/未登记参数明确拒绝、
资源缺失或 YAML 损坏时报错不崩。
"""
from __future__ import annotations

import pytest
import yaml

from bibit_intent.core.appcli_registry import (
    CHANNELS,
    RISCV64_STATES,
    VERIFY_RULES,
    AppCliRegistry,
    AppCliRegistryError,
)


def _write(tmp_path, document) -> str:
    path = tmp_path / "appcli_registry.yaml"
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return str(path)


def _minimal(**overrides):
    appcli = {
        "name": "demo",
        "display": "Demo",
        "entry_point": "cli-anything-demo",
        "channel": "artifact-only",
        "riscv64": "verified",
        "actions": [
            {
                "name": "run",
                "argv": ["--mode", "{mode}"],
                "params": {"mode": {"type": "str", "default": "fast"}},
            }
        ],
    }
    appcli.update(overrides)
    return {"version": 1, "appclies": [appcli]}


# ── 打包资源本身必须自洽 ──────────────────────────────────────────────

def test_packaged_registry_loads_and_is_self_consistent():
    registry = AppCliRegistry.load()
    assert registry.names(), "打包注册表不能是空的"
    for spec in registry:
        assert spec.channel in CHANNELS
        assert spec.riscv64 in RISCV64_STATES
        for action in spec.actions:
            for artifact in action.artifacts:
                assert set(artifact.verify) <= set(VERIFY_RULES)
    # 白名单只放已通过 H16 设备验收的 appcli（今天只有 libreoffice）。
    assert {spec.name for spec in registry.verified()} == {"libreoffice"}


def test_packaged_registry_gates_a_non_whitelisted_appcli():
    registry = AppCliRegistry.load()
    for name in ("drawio",):  # riscv64 上 unsupported：设备没有 drawio 桌面后端
        with pytest.raises(AppCliRegistryError) as excinfo:
            registry.require_verified(name)
        assert excinfo.value.code == "appcli_unverified"


# ── argv 渲染 ────────────────────────────────────────────────────────

def test_build_command_uses_entry_point_and_defaults(tmp_path):
    spec = AppCliRegistry.load(_write(tmp_path, _minimal())).require("demo")
    assert spec.build_command("run", {}) == ["cli-anything-demo", "--mode", "fast"]
    assert spec.build_command("run", {"mode": "slow"}) == ["cli-anything-demo", "--mode", "slow"]


def test_undeclared_argument_is_rejected(tmp_path):
    registry = AppCliRegistry.load(_write(tmp_path, _minimal()))
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("demo").render_argv("run", {"mode": "fast", "extra": "x"})
    assert excinfo.value.code == "invalid_args"
    assert "extra" in str(excinfo.value)


def test_missing_required_argument_is_rejected(tmp_path):
    document = _minimal(
        actions=[{"name": "run", "argv": ["-o", "{output}"], "params": {"output": {"type": "str"}}}]
    )
    registry = AppCliRegistry.load(_write(tmp_path, document))
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("demo").build_command("run", {})
    assert excinfo.value.code == "invalid_args"


def test_enum_violation_is_rejected(tmp_path):
    document = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-f", "{fmt}"],
                "params": {"fmt": {"type": "str", "enum": ["png", "pdf"], "default": "png"}},
            }
        ]
    )
    registry = AppCliRegistry.load(_write(tmp_path, document))
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("demo").build_command("run", {"fmt": "bmp"})
    assert excinfo.value.code == "invalid_args"


@pytest.mark.parametrize("value", [None, "", "   ", "a\nb", "a\0b"])
def test_unsafe_values_are_rejected(tmp_path, value):
    registry = AppCliRegistry.load(_write(tmp_path, _minimal()))
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("demo").render_argv("run", {"mode": value})
    assert excinfo.value.code == "invalid_args"


def test_numeric_and_bool_values_render_as_text(tmp_path):
    document = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-l", "{level}", "--flag", "{on}"],
                "params": {"level": {"type": "int", "default": 1}, "on": {"type": "bool", "default": True}},
            }
        ]
    )
    registry = AppCliRegistry.load(_write(tmp_path, document))
    assert registry.require("demo").build_command("run", {"level": 3, "on": False}) == [
        "cli-anything-demo", "-l", "3", "--flag", "false",
    ]


def test_unknown_appcli_and_action_are_rejected(tmp_path):
    registry = AppCliRegistry.load(_write(tmp_path, _minimal()))
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("nope")
    assert excinfo.value.code == "unknown_appcli"
    with pytest.raises(AppCliRegistryError) as excinfo:
        registry.require("demo").build_command("nope", {})
    assert excinfo.value.code == "unknown_action"


# ── 加载期校验 ───────────────────────────────────────────────────────

def test_missing_resource_reports_typed_error(tmp_path):
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(tmp_path / "absent.yaml")
    assert excinfo.value.code == "registry_unavailable"


def test_broken_yaml_reports_typed_error(tmp_path):
    path = _write(tmp_path, "version: 1\nappclies: [ {: \n")
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(path)
    assert excinfo.value.code == "registry_invalid"


def test_unsupported_version_is_rejected(tmp_path):
    document = _minimal()
    document["version"] = 2
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"channel": "cloud"}, "channel"),
        ({"riscv64": "maybe"}, "riscv64"),
        ({"entry_point": "/usr/bin/cli-anything-demo"}, "entry_point"),
        ({"entry_point": "cli anything"}, "entry_point"),
        ({"actions": []}, "动作"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": 3600}, "timeout_seconds"),
    ],
)
def test_invalid_appcli_fields_are_rejected(tmp_path, override, fragment):
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, _minimal(**override)))
    assert excinfo.value.code == "registry_invalid"
    assert fragment in str(excinfo.value)


def test_timeout_seconds_defaults_to_the_render_default(tmp_path):
    """默认超时是「这类动作通常要多久」的知识，放在注册表而不是散在代码里。"""
    from bibit_intent.core.appcli_registry import DEFAULT_APPCLI_TIMEOUT_SECONDS

    spec = AppCliRegistry.load(_write(tmp_path, _minimal())).require("demo")
    assert spec.timeout_seconds == DEFAULT_APPCLI_TIMEOUT_SECONDS == 300

    overridden = AppCliRegistry.load(
        _write(tmp_path, _minimal(timeout_seconds=600))
    ).require("demo")
    assert overridden.timeout_seconds == 600


def test_packaged_registry_declares_a_timeout_per_appcli():
    registry = AppCliRegistry.load()
    assert all(spec.timeout_seconds > 0 for spec in registry)


def test_unknown_field_is_rejected(tmp_path):
    """字段拼写错误必须在加载期暴露，而不是被静默忽略。"""
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, _minimal(chanel="host")))
    assert excinfo.value.code == "registry_invalid"


def test_argv_placeholder_must_be_declared(tmp_path):
    document = _minimal(
        actions=[{"name": "run", "argv": ["-o", "{output}"], "params": {}}]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"
    assert "output" in str(excinfo.value)


def test_required_param_must_appear_in_argv(tmp_path):
    document = _minimal(
        actions=[{"name": "run", "argv": ["--fixed"], "params": {"output": {"type": "str"}}}]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"


def test_duplicate_action_and_appcli_names_are_rejected(tmp_path):
    duplicated_action = _minimal(
        actions=[
            {"name": "run", "argv": ["--a"], "params": {}},
            {"name": "run", "argv": ["--b"], "params": {}},
        ]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, duplicated_action))
    assert excinfo.value.code == "registry_invalid"

    document = _minimal()
    document["appclies"].append(dict(document["appclies"][0]))
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"


def test_artifact_rule_must_be_known(tmp_path):
    document = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-o", "{output}"],
                "params": {"output": {"type": "str", "path": True}},
                "artifacts": [{"param": "output", "verify": ["magic_pdf"]}],
            }
        ]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"


def test_artifact_rule_must_name_a_declared_path_param(tmp_path):
    """产物规则必须说清校验哪个文件，且那个参数必须是路径参数。"""
    unknown_param = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-o", "{output}"],
                "params": {"output": {"type": "str", "path": True}},
                "artifacts": [{"param": "ghost", "verify": ["exists"]}],
            }
        ]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, unknown_param))
    assert excinfo.value.code == "registry_invalid"

    not_a_path = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-o", "{output}"],
                "params": {"output": {"type": "str"}},
                "artifacts": [{"param": "output", "verify": ["exists"]}],
            }
        ]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, not_a_path))
    assert excinfo.value.code == "registry_invalid"
    assert "path" in str(excinfo.value)


def test_artifact_rule_needs_at_least_one_rule(tmp_path):
    document = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["-o", "{output}"],
                "params": {"output": {"type": "str", "path": True}},
                "artifacts": [{"param": "output", "verify": []}],
            }
        ]
    )
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, document))
    assert excinfo.value.code == "registry_invalid"


# ── 只读与产物查询 ───────────────────────────────────────────────────

def test_readonly_actions_and_artifact_lookup(tmp_path):
    document = _minimal(
        actions=[
            {"name": "peek", "argv": ["peek"], "params": {}, "readonly": True},
            {
                "name": "render",
                "argv": ["-o", "{output}"],
                "params": {"output": {"type": "str", "path": True}},
                "artifacts": [{"param": "output", "verify": ["exists", "size>0"]}],
            },
        ]
    )
    spec = AppCliRegistry.load(_write(tmp_path, document)).require("demo")
    assert [action.name for action in spec.actions if action.readonly] == ["peek"]
    rules = spec.artifact_rules("render")
    assert [(item.param, item.verify) for item in rules] == [("output", ["exists", "size>0"])]
    # 只读动作不声明产物规则
    assert spec.artifact_rules("peek") == ()


def test_packaged_registry_artifact_params_are_declared_paths():
    """打包注册表里的每条产物规则都能解析出真实的路径参数。"""
    registry = AppCliRegistry.load()
    checked = 0
    for spec in registry:
        for action in spec.actions:
            for artifact in action.artifacts:
                assert action.params[artifact.param].path
                checked += 1
    assert checked, "打包注册表应至少声明一条产物规则"


def test_colon_values_survive_yaml_round_trip(tmp_path):
    """`16:9` 这类值不加引号会被 YAML 解析成整数，注册表必须能正确还原。"""
    document = _minimal(
        actions=[
            {
                "name": "run",
                "argv": ["--preset", "{preset}"],
                "params": {"preset": {"type": "str", "enum": ["16:9", "4:3"], "default": "16:9"}},
            }
        ]
    )
    spec = AppCliRegistry.load(_write(tmp_path, document)).require("demo")
    assert spec.build_command("run", {"preset": "4:3"}) == ["cli-anything-demo", "--preset", "4:3"]


# ── 执行环境变量（H16 设备发现）─────────────────────────────────────

def test_packaged_registry_pins_the_headless_vcl_plugin(tmp_path):
    """LibreOffice 在无 DISPLAY 的设备上会以 exit 134 起不来，注册表必须钉住 svp。"""
    spec = AppCliRegistry.load().require("libreoffice")

    assert spec.env == {"SAL_USE_VCLPLUGIN": "svp"}


@pytest.mark.parametrize("env", [
    {"": "x"},
    {"A=B": "x"},
    {"KEY": "line\nbreak"},
    {"KEY": 1},
])
def test_invalid_appcli_env_is_rejected(tmp_path, env):
    with pytest.raises(AppCliRegistryError) as excinfo:
        AppCliRegistry.load(_write(tmp_path, _minimal(env=env)))
    assert excinfo.value.code == "registry_invalid"


def test_absent_appcli_env_defaults_to_empty(tmp_path):
    spec = AppCliRegistry.load(_write(tmp_path, _minimal())).require("demo")

    assert spec.env == {}
