import pytest

from bibit_intent import EmbeddedBibitPipeline, PipelineSettings
from bibit_intent.core.registry import ToolRegistry
from bibit_intent.execution.executor import Executor
from bibit_intent.execution.skill_loader import load_skills
from bibit_intent.execution.system_tools import ExecResult
from bibit_intent.resources import resource_path


def test_embedded_pipeline_preserves_install_handler_dispatch(tmp_path, monkeypatch):
    import bibit_intent.pipeline as pipeline_module

    executors = []
    calls = []

    def install(**params):
        calls.append(params)
        return ExecResult(True, "mock installed")

    def build_executor(*args, **kwargs):
        executor = Executor(*args, **kwargs)
        executor._handlers["pkg_install"] = install
        executors.append(executor)
        return executor

    monkeypatch.setattr(pipeline_module, "Executor", build_executor)
    monkeypatch.setattr(pipeline_module.EmbeddingRetriever, "start_background_build", lambda self: None)
    EmbeddedBibitPipeline.from_package(
        data_root=tmp_path, settings=PipelineSettings(local_timeout_seconds=1)
    )
    result = executors[0].execute("pkg_install", {"pkg": "bash"})
    assert result.ok is True
    assert calls == [{"pkg": "bash"}]


@pytest.mark.parametrize("package,expected", [("bash", True), ("other-package", False)])
def test_explicit_callback_only_accepts_reviewed_action(package, expected):
    registry = ToolRegistry(resource_path("tools.yaml"))
    load_skills(resource_path("skills"), registry, {})
    calls = []
    reviewed_action = registry.get("pkg_install").display + "：pkg=bash"

    def install(**params):
        calls.append(params)
        return ExecResult(True, "mock installed")

    executor = Executor(
        registry,
        confirm_callback=lambda action: action == reviewed_action,
        handlers={"pkg_install": install},
    )
    result = executor.execute("pkg_install", {"pkg": package})
    assert result.ok is expected
    assert calls == ([{"pkg": package}] if expected else [])
