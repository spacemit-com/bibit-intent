"""H15：appcli wheel 锁定表与内部源口径。

对应 docs/design/cli_anything_integration.md §7 与计划表 H15：上游 appcli 的官方装法是
`pip install git+github`，端侧一律不直装。锁定表把「上游提交 + 包元数据 + 内部源」固定下来，
本文件固定三件事：锁定表 schema 严格、依赖计划里不出现 git+、缺前置提示指向内部源。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest
import yaml

from bibit_intent.core.appcli_prereq import AppCliReadiness
from bibit_intent.core.appcli_registry import AppCliRegistry
from bibit_intent.core.appcli_wheels import (
    AppCliWheelLockError,
    git_plus_specs,
    load_lock,
    parse_lock,
    source_note,
)
from bibit_intent.resources import resource_path

_ORIGINAL_LOAD = AppCliRegistry.load


def _document(**overrides):
    document = {
        "version": 1,
        "upstream": {
            "repository": "https://github.com/HKUDS/CLI-Anything",
            "commit": "810c18b0d1ab9b234bc996c9fd999318523a3ef0",
            "registry": "registry.json",
        },
        "internal": {
            "index_url": "https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple",
            "wheel_dir": "/opt/bibit-intent/wheels/appcli",
        },
        "packages": [
            {
                "appcli": "demo",
                "name": "cli-anything-demo",
                "version": "1.0.0",
                "subdirectory": "demo/agent-harness",
                "skill": "skills/cli-anything-demo/SKILL.md",
                "entry_point": "cli-anything-demo",
                "sha256": "",
            }
        ],
    }
    document.update(overrides)
    return document


# ── 锁定表 schema ────────────────────────────────────────────────────

def test_upstream_subdirectories_keep_the_upstream_directory_name():
    """`agent-harness` 是**上游**的目录名，不随本仓的命名改动。

    `appcli` 那次改名把锁定表和转换器里的 `agent-harness` 一起机械替换掉了，
    而上游（含锁定的 `810c18b`）只有 `agent-harness`：H15 的 `--check` 会报
    「上游没有子目录」，H12 的转换器则悄悄退到已发布的 `skills/` 副本而不是
    包内那份。这里把「不重写上游路径」钉住，`<app>/agent-harness` 是唯一合法形状。
    """
    lock = load_lock()

    assert lock.packages
    for package in lock.packages:
        assert package.subdirectory == f"{package.appcli}/agent-harness"


def test_no_rewritten_upstream_directory_name_anywhere_in_the_sources():
    """机械改名最容易漏掉的就是「不是我们的词汇」的名字，这里全树扫一遍。

    范围是代码（`src`/`scripts`/`tests`），跳过 `__pycache__`。刻意不扫：

    - `docs/`：文档要能直说「改名把 `agent-harness` 错写成…」这件事（见
      `cli_anything_skill_comparison.md` §9.1），不该被守卫误伤；
    - `debian/<pkg>/`：dpkg 构建产物，是 `src` 的旧副本，重新打包前必然不一致。

    关键词拼接是为了不让本测试自己命中。
    """
    root = Path(__file__).resolve().parents[1]
    needle = "agent-" + "appcli"

    offenders = [
        path.relative_to(root).as_posix()
        for folder in ("src", "scripts", "tests")
        for path in (root / folder).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and needle in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert offenders == []


def test_packaged_lock_is_valid_and_pins_the_upstream_commit():
    lock = load_lock()

    assert lock.commit == "810c18b0d1ab9b234bc996c9fd999318523a3ef0"
    assert {item.name for item in lock.packages} == {
        "cli-anything-libreoffice", "cli-anything-drawio",
    }
    assert lock.unpublished() == lock.packages, "未发布前 sha256 必须留空，别写假校验和"


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda doc: doc.update(version=2), "version"),
    (lambda doc: doc["upstream"].update(commit="810c18b"), "commit"),
    (lambda doc: doc["upstream"].update(repository="git://github.com/x/y"), "https"),
    (lambda doc: doc["packages"][0].update(subdirectory="/etc/passwd"), "相对路径"),
    (lambda doc: doc["packages"][0].update(skill="../outside/SKILL.md"), "相对路径"),
    (lambda doc: doc["packages"][0].update(sha256="deadbeef"), "sha256"),
    (lambda doc: doc.update(packages=[]), "packages"),
    (lambda doc: doc["packages"].append(dict(doc["packages"][0])), "重复"),
    (lambda doc: doc["internal"].update(wheel_dir="wheels"), "绝对路径"),
])
def test_invalid_locks_are_rejected(mutate, message):
    document = _document()
    mutate(document)

    with pytest.raises(AppCliWheelLockError) as excinfo:
        parse_lock(document)

    assert message in str(excinfo.value)


def test_git_plus_source_in_the_lock_itself_is_rejected():
    document = _document()
    document["internal"]["index_url"] = "git+https://github.com/HKUDS/CLI-Anything"

    with pytest.raises(AppCliWheelLockError) as excinfo:
        parse_lock(document)

    assert "git+" in str(excinfo.value)


def test_git_plus_specs_flags_source_installs_only():
    assert git_plus_specs([
        "cli-anything-libreoffice==1.0.1",
        "pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=libreoffice",
        "-i https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple",
    ]) == ["pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=libreoffice"]


# ── 来源口径 ─────────────────────────────────────────────────────────

def test_source_note_points_at_the_internal_mirror_not_upstream():
    note = source_note("cli-anything-libreoffice")

    assert "git.spacemit.com" in note
    assert "cli-anything-libreoffice==1.0.1" in note
    assert "/opt/bibit-intent/wheels/appcli" in note
    assert "git+" not in note


def test_source_note_is_empty_for_unmanaged_packages():
    assert source_note("requests") == ""
    assert source_note("") == ""


def test_published_package_note_drops_the_offline_hint():
    document = _document()
    document["packages"][0]["sha256"] = "a" * 64
    lock = parse_lock(document)

    note = source_note("cli-anything-demo", lock)

    assert "已发布" in note
    assert "离线" not in note


# ── 缺前置提示：内部源而不是 git+ ────────────────────────────────────

def _load_handlers():
    path = resource_path("skills") / "appcli" / "handlers.py"
    spec = importlib.util.spec_from_file_location("_test_appcli_wheels_handlers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_prereq_hint_shows_the_internal_source_for_a_locked_wheel(tmp_path, monkeypatch):
    handlers = _load_handlers()
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "appclies": [
                    {
                        "name": "libreoffice",
                        "display": "LibreOffice",
                        "entry_point": "cli-anything-libreoffice",
                        "channel": "host",
                        "riscv64": "verified",
                        "requires": {"apt": ["libreoffice-writer"],
                                     "python": ["cli-anything-libreoffice"]},
                        "actions": [{"name": "peek", "argv": ["peek"], "readonly": True}],
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        handlers.AppCliRegistry, "load",
        classmethod(lambda cls, _path=None: _ORIGINAL_LOAD(path)),
    )
    monkeypatch.setattr(
        handlers, "check_readiness",
        lambda spec: AppCliReadiness(
            entry_point_found=False,
            missing_apt=("libreoffice-writer",),
            missing_python=("cli-anything-libreoffice",),
        ),
    )

    result = handlers.appcli_run(appcli="libreoffice", action="peek")

    assert not result.ok
    assert "内部源 git.spacemit.com" in result.message
    assert "cli-anything-libreoffice==1.0.1" in result.message
    assert "git+" not in result.message
    # 其余口径不变：仍然是「不安装、先出计划」。
    assert "生成依赖计划 <工程目录>" in result.message
