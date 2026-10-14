import importlib.util
import sys
import pytest

from bibit_intent.core.package_state import PackageState

from bibit_intent.resources import resource_path


def load_handlers():
    spec = importlib.util.spec_from_file_location(
        "pkg_info_summary_handlers", resource_path("skills/ubuntu_cmd/handlers.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state,expected", [
    (PackageState("installed", "4.0"), "本机状态：已安装 4.0"),
    (PackageState("installed", "4.0", True), "本机状态：已安装 4.0（hold）"),
    (PackageState("config_only"), "未安装，仅保留配置"),
    (PackageState("incomplete", "4.0"), "未配置完成或状态异常"),
    (PackageState("unknown"), "不能确定是否已安装"),
])
def test_repository_and_installed_versions_are_distinct(monkeypatch, state, expected):
    module = load_handlers()
    monkeypatch.setattr(module, "query_package_state", lambda pkg: state)
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: module.ExecResult(True, "ok", "Package: bash\nVersion: 5.0\nArchitecture: riscv64"))
    result = module.pkg_info("bash")
    assert "版本：5.0" in result.message
    assert expected in result.message


def test_metadata_summary_preserves_details_and_does_not_claim_installed(monkeypatch):
    module = load_handlers()
    detail = "Package: bash\nVersion: 5.3\nArchitecture: riscv64\nDescription: shell"
    calls = []

    def run(command, timeout):
        calls.append(command)
        return module.ExecResult(True, "ok", detail)

    monkeypatch.setattr(module, "_run", run)
    result = module.pkg_info("bash")
    assert "bash" in result.message and "5.3" in result.message
    assert "riscv64" in result.message
    assert "不代表已安装" in result.message
    assert result.detail == detail
    assert calls == [["apt-cache", "show", "bash"]]


def test_metadata_summary_handles_multiple_versions(monkeypatch):
    module = load_handlers()
    detail = "Package: bash\nVersion: 5.3\n\nPackage: bash\nVersion: 5.2"
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: module.ExecResult(True, "ok", detail))
    result = module.pkg_info("bash")
    assert "2 条" in result.message
    assert "首条" in result.message
    assert "最新" not in result.message


def test_empty_metadata_is_not_success(monkeypatch):
    module = load_handlers()
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: module.ExecResult(True, "ok", ""))
    assert module.pkg_info("bash").ok is False


def test_metadata_failure_is_preserved(monkeypatch):
    module = load_handlers()
    failure = module.ExecResult(False, "命令超时", "diagnostic")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: failure)
    assert module.pkg_info("bash") is failure
