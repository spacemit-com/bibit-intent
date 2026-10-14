"""H15：wheel 构建 / 校验流水线。

对应 docs/design/cli_anything_integration.md §7：同一流水线里跑「元数据 + wheel + 技能」，
三者必须同版本。这里用假的转换器与假的构建器把流程本身固定下来（真构建见
`scripts/build_appcli_wheels.py --upstream <checkout> --out dist/appcli-wheels`），
另有一个用真转换器 + 真注册表的端到端校验用例。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_appcli_wheels.py"

COMMIT = "810c18b0d1ab9b234bc996c9fd999318523a3ef0"


def _load_pipeline():
    spec = importlib.util.spec_from_file_location("_test_build_appcli_wheels", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pipeline():
    return _load_pipeline()


def _lock_document(**overrides):
    document = {
        "version": 1,
        "upstream": {
            "repository": "https://github.com/HKUDS/CLI-Anything",
            "commit": COMMIT,
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


def _registry_document(*, verified: bool = True, python=("cli-anything-demo",)):
    return {
        "version": 1,
        "appclies": [
            {
                "name": "demo",
                "display": "Demo",
                "entry_point": "cli-anything-demo",
                "channel": "artifact-only",
                "riscv64": "verified" if verified else "unverified",
                "requires": {"python": list(python)},
                "actions": [{"name": "peek", "argv": ["peek"], "readonly": True}],
            }
        ],
    }


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    root = tmp_path / "upstream"
    (root / "demo" / "agent-harness").mkdir(parents=True)
    (root / "skills" / "cli-anything-demo").mkdir(parents=True)
    (root / "skills" / "cli-anything-demo" / "SKILL.md").write_text(
        "# demo\n", encoding="utf-8"
    )
    (root / "registry.json").write_text(
        json.dumps({
            "clis": [{
                "name": "demo",
                "version": "1.0.0",
                "entry_point": "cli-anything-demo",
                "skill_md": "skills/cli-anything-demo/SKILL.md",
            }]
        }),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    path = tmp_path / "appcli_wheels.yaml"
    path.write_text(yaml.safe_dump(_lock_document(), allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(yaml.safe_dump(_registry_document(), allow_unicode=True), encoding="utf-8")
    return path


class FakeConverter:
    """假转换器：只记录被怎么调用，顺便暴露检查结果。"""

    class ConversionError(RuntimeError):
        pass

    def __init__(self, problems=()):
        self.problems = list(problems)
        self.calls: list[tuple[str, bool]] = []

    def convert_all(self, upstream, registry_path, *, include_unverified):
        self.calls.append(("convert", include_unverified))
        return [types.SimpleNamespace(directory="cli-anything-demo", text="x")], []

    def check_skills(self, skills, out):
        self.calls.append(("check", out))
        return list(self.problems)

    def write_skills(self, skills, out):
        self.calls.append(("write", out))
        return [Path(out) / "cli-anything-demo" / "SKILL.md"]


def _fake_builder(content: bytes = b"wheel-bytes"):
    def build(upstream, package, out):
        wheels_dir = Path(out) / "wheels"
        wheels_dir.mkdir(parents=True, exist_ok=True)
        wheel = wheels_dir / f"{package.name.replace('-', '_')}-{package.version}-py3-none-any.whl"
        wheel.write_bytes(content)
        return wheel

    return build


# ── 校验模式 ─────────────────────────────────────────────────────────

def test_check_mode_reports_no_problems_and_does_not_build(
    pipeline, upstream, lock_path, registry_path, tmp_path,
):
    converter = FakeConverter()

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry_path, check=True, converter=converter,
    )

    assert report.problems == []
    assert report.written == []
    assert ("check", tmp_path / "out" / "skills") in converter.calls
    assert not (tmp_path / "out" / "appcli-wheels.json").exists()


def test_metadata_mismatch_with_the_registry_is_reported(
    pipeline, upstream, lock_path, tmp_path,
):
    registry = tmp_path / "appcli_registry.yaml"
    registry.write_text(
        yaml.safe_dump(_registry_document(python=()), allow_unicode=True), encoding="utf-8"
    )

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry, check=True, converter=FakeConverter(),
    )

    assert any("requires.python" in problem for problem in report.problems)


def test_verified_appcli_without_a_locked_wheel_is_reported(
    pipeline, upstream, lock_path, tmp_path,
):
    lock = tmp_path / "appcli_wheels.yaml"
    document = _lock_document()
    document["packages"][0]["appcli"] = "other"
    lock.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    registry = tmp_path / "appcli_registry.yaml"
    registry.write_text(
        yaml.safe_dump(_registry_document(), allow_unicode=True), encoding="utf-8"
    )

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock,
        registry_path=registry, check=True, converter=FakeConverter(),
    )

    assert any("锁定表里没有它的 wheel" in problem for problem in report.problems)


def test_upstream_metadata_drift_is_reported(pipeline, upstream, lock_path, registry_path, tmp_path):
    (upstream / "registry.json").write_text(
        json.dumps({"clis": [{"name": "demo", "version": "2.0.0",
                              "entry_point": "cli-anything-demo",
                              "skill_md": "skills/cli-anything-demo/SKILL.md"}]}),
        encoding="utf-8",
    )

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry_path, check=True, converter=FakeConverter(),
    )

    assert any("version" in problem and "2.0.0" in problem for problem in report.problems)


def test_missing_subdirectory_in_upstream_is_reported(
    pipeline, upstream, lock_path, registry_path, tmp_path,
):
    (upstream / "demo" / "agent-harness").rmdir()

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry_path, check=True, converter=FakeConverter(),
    )

    assert any("子目录" in problem for problem in report.problems)


def test_git_plus_requirement_in_the_registry_is_a_problem(
    pipeline, upstream, lock_path, tmp_path,
):
    registry = tmp_path / "appcli_registry.yaml"
    document = _registry_document()
    document["appclies"][0]["requires"]["python"] = [
        "git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=demo/agent-harness"
    ]
    registry.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry, check=True, converter=FakeConverter(),
    )

    assert any("源码直取" in problem for problem in report.problems)


def test_commit_mismatch_stops_the_run_unless_allowed(
    pipeline, upstream, lock_path, registry_path, tmp_path, monkeypatch,
):
    monkeypatch.setattr(pipeline, "head_commit", lambda _upstream: "b" * 40)

    with pytest.raises(pipeline.PipelineError) as excinfo:
        pipeline.run(upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
                     registry_path=registry_path, check=True, converter=FakeConverter())
    assert "锁定表要求" in str(excinfo.value)

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry_path, check=True, converter=FakeConverter(),
        allow_commit_mismatch=True,
    )
    assert report.problems == []


# ── 构建模式 ─────────────────────────────────────────────────────────

def test_build_writes_wheels_and_a_manifest_with_checksums(
    pipeline, upstream, lock_path, registry_path, tmp_path,
):
    out = tmp_path / "out"

    report = pipeline.run(
        upstream=upstream, out=out, lock_path=lock_path, registry_path=registry_path,
        converter=FakeConverter(), builder=_fake_builder(b"payload"),
    )

    assert report.problems == []
    manifest = json.loads((out / "appcli-wheels.json").read_text(encoding="utf-8"))
    package = manifest["packages"][0]
    assert package["name"] == "cli-anything-demo"
    assert package["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert package["wheel"].endswith("-py3-none-any.whl")
    assert manifest["upstream"]["commit"] == COMMIT
    assert manifest["index_url"].startswith("https://git.spacemit.com/")
    written = [path.name for path in report.written]
    assert "appcli-wheels.json" in written
    assert any(name.endswith("-py3-none-any.whl") for name in written)


def test_published_lock_with_a_different_checksum_is_a_problem(
    pipeline, upstream, tmp_path, registry_path,
):
    lock = tmp_path / "appcli_wheels.yaml"
    document = _lock_document()
    document["packages"][0]["sha256"] = "c" * 64
    lock.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock,
        registry_path=registry_path, converter=FakeConverter(), builder=_fake_builder(),
    )

    assert any("校验和不一致" in problem for problem in report.problems)


def test_update_lock_records_the_observed_checksum(
    pipeline, upstream, lock_path, registry_path, tmp_path,
):
    report = pipeline.run(
        upstream=upstream, out=tmp_path / "out", lock_path=lock_path,
        registry_path=registry_path, converter=FakeConverter(),
        builder=_fake_builder(b"payload"), update_lock=True,
    )

    digest = hashlib.sha256(b"payload").hexdigest()
    written = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    assert written["packages"][0]["sha256"] == digest
    assert report.problems == []


def test_manifest_checksum_is_re_checked_on_a_later_check_run(
    pipeline, upstream, lock_path, registry_path, tmp_path,
):
    out = tmp_path / "out"
    pipeline.run(
        upstream=upstream, out=out, lock_path=lock_path, registry_path=registry_path,
        converter=FakeConverter(), builder=_fake_builder(b"payload"),
    )
    # 发布后锁定了校验和，但内部源上的产物被换过 → 校验模式必须报出来。
    document = _lock_document()
    document["packages"][0]["sha256"] = "d" * 64
    lock_path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    report = pipeline.run(
        upstream=upstream, out=out, lock_path=lock_path, registry_path=registry_path,
        check=True, converter=FakeConverter(),
    )

    assert any("清单校验和与锁定表不一致" in problem for problem in report.problems)


# ── 与真转换器的接线 ─────────────────────────────────────────────────

UPSTREAM_SKILL = (
    "---\n"
    "name: >-\n"
    "  cli-anything-{appcli}\n"
    "description: >-\n"
    "  Command-line interface for {appcli} - create, edit and export "
    "{appcli} artifacts\n"
    "  from the command line, with project files and JSON output for agents.\n"
    "category: office\n"
    "entry_point: cli-anything-{appcli}\n"
    "install: pip install git+https://github.com/HKUDS/CLI-Anything.git\n"
    "---\n"
    "\n"
    "# cli-anything-{appcli}\n"
    "\n"
    "## Installation\n"
    "\n"
    "```bash\n"
    "pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory={appcli}\n"
    "```\n"
    "\n"
    "## Examples\n"
    "\n"
    "```bash\n"
    "cli-anything-{appcli} export render out.pdf\n"
    "```\n"
)


def _real_upstream(tmp_path):
    """按真实锁定表造一个上游检出：每包一个 SKILL.md（含上游的 git+ 安装段）。"""
    from bibit_intent.core.appcli_wheels import load_lock

    lock = load_lock()
    upstream = tmp_path / "upstream"
    entries = []
    for package in lock.packages:
        (upstream / package.subdirectory).mkdir(parents=True)
        skill = upstream / package.subdirectory / "cli_anything" / package.appcli / "skills"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            UPSTREAM_SKILL.format(appcli=package.appcli), encoding="utf-8"
        )
        entries.append({
            "name": package.appcli, "version": package.version,
            "entry_point": package.entry_point, "skill_md": package.skill,
        })
    (upstream / lock.registry).write_text(json.dumps({"clis": entries}), encoding="utf-8")
    return upstream


def test_real_converter_generates_a_skill_for_the_verified_appcli(pipeline, tmp_path):
    """真锁定表 + 真注册表 + 真转换器：只有 verified 的 appcli 生成技能，且能复检。"""
    from bibit_intent.resources import resource_path

    upstream = _real_upstream(tmp_path)
    out = tmp_path / "out"
    registry = resource_path("appcli_registry.yaml")

    report = pipeline.run(
        upstream=upstream, out=out, registry_path=registry,
        converter=None, builder=_fake_builder(),
    )
    assert report.problems == []

    skill = out / "skills" / "cli-anything-libreoffice" / "SKILL.md"
    assert skill.is_file(), "libreoffice 已 verified，必须生成 per-app 技能"
    assert not (out / "skills" / "cli-anything-drawio").exists(), "unsupported 的不落地"
    assert "git+" not in skill.read_text(encoding="utf-8")

    again = pipeline.run(
        upstream=upstream, out=out, registry_path=registry, check=True,
    )
    assert again.problems == []
