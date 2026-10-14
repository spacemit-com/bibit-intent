"""H12：上游 CLI-Anything 技能 → Runtime 技能的构建期转换器。

对应 docs/design/cli_anything_plan.md 的 H12 与
docs/design/cli_anything_skill_comparison.md 的结论：上游 151 个 SKILL.md 里 109 个会被
Runtime 加载器硬拒（折叠块标量 + 未登记键），所以**不能直接拷贝**，必须构建期转换。

本文件固定转换器的四条硬要求：四键单行 frontmatter、删掉 Installation/REPL、
只同步 `riscv64: verified` 的 appcli、目录名与 frontmatter name 一致且不重名。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import convert_appcli_skills as conv  # noqa: E402

UPSTREAM_FIXTURE = (
    "---\n"
    "name: >-\n"
    "  cli-anything-demo\n"
    "description: >-\n"
    "  Command-line interface for Demo - Create, edit and export demo artifacts\n"
    "  from the command line, with project files and JSON output for agents.\n"
    "author: Someone\n"
    "category: office\n"
    "entry_point: cli-anything-demo\n"
    "requires:\n"
    "- demo-tool\n"
    "install: pip install git+https://github.com/HKUDS/CLI-Anything.git\n"
    "runAs: subagent\n"
    "---\n"
    "\n"
    "# cli-anything-demo\n"
    "\n"
    "A demo appcli.\n"
    "\n"
    "## Installation\n"
    "\n"
    "```bash\n"
    "pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=demo\n"
    "```\n"
    "\n"
    "## Usage\n"
    "\n"
    "```bash\n"
    "# Show help\n"
    "cli-anything-demo --help\n"
    "\n"
    "# Start interactive REPL mode\n"
    "cli-anything-demo\n"
    "```\n"
    "\n"
    "## REPL Mode\n"
    "\n"
    "Run without arguments to enter an interactive session.\n"
    "\n"
    "## Examples\n"
    "\n"
    "### Interactive REPL Session\n"
    "\n"
    "Start an interactive session.\n"
    "\n"
    "```bash\n"
    "cli-anything-demo\n"
    "```\n"
    "\n"
    "### Export\n"
    "\n"
    "```bash\n"
    "cli-anything-demo export render out.pdf\n"
    "```\n"
)


def _registry(tmp_path: Path, *entries) -> Path:
    path = tmp_path / "appcli_registry.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "appclies": list(entries)}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _appcli(name: str, riscv64: str = "verified", **overrides) -> dict:
    spec = {
        "name": name,
        "display": f"{name} display",
        "entry_point": f"cli-anything-{name}",
        "channel": "artifact-only",
        "riscv64": riscv64,
        "actions": [{"name": "peek", "argv": ["peek"], "readonly": True}],
    }
    spec.update(overrides)
    return spec


def _upstream(tmp_path: Path, name: str, text: str = UPSTREAM_FIXTURE) -> Path:
    root = tmp_path / "upstream"
    target = root / name / "agent-harness" / "cli_anything" / name / "skills" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return root


# ── frontmatter ──────────────────────────────────────────────────────

def test_block_scalars_become_a_single_line_description(tmp_path):
    root = _upstream(tmp_path, "demo")
    skill = conv.convert_skill(root, "demo")

    conv.validate(skill.text)
    header = skill.text.split("---")[1]
    assert "\n  " not in header, "存在续行说明块标量没压平"
    assert (
        "description: Command-line interface for Demo - Create, edit and export demo "
        "artifacts from the command line, with project files and JSON output for agents."
    ) in skill.text


def test_unregistered_keys_are_dropped_and_registry_keys_reported(tmp_path):
    root = _upstream(tmp_path, "demo")
    skill = conv.convert_skill(root, "demo")

    for dropped in ("author", "runAs", "install"):
        assert f"\n{dropped}:" not in skill.text
    assert set(skill.registry_hints) == {"category", "entry_point", "requires", "install"}
    assert skill.registry_hints["category"] == "office"


def test_long_description_is_cut_at_the_first_sentence():
    long_text = (
        "Command-line interface for Demo - first sentence here. Then a very long "
        + "enumeration of features " * 40
    )
    collapsed = conv.collapse_description(long_text, limit=80)
    assert collapsed == "Command-line interface for Demo - first sentence here."
    assert len(collapsed) <= 80


def test_very_long_single_sentence_is_truncated_without_trailing_dots():
    collapsed = conv.collapse_description("word " * 200, limit=40)
    assert len(collapsed) <= 41
    assert collapsed.endswith("…")
    assert not collapsed.rstrip("…").endswith(" ")


# ── 正文 ─────────────────────────────────────────────────────────────

def test_installation_and_repl_sections_are_removed(tmp_path):
    root = _upstream(tmp_path, "demo")
    body = conv.convert_skill(root, "demo").text

    assert "## Installation" not in body
    assert "## REPL Mode" not in body
    assert "### Interactive REPL Session" not in body
    assert "pip install" not in body
    assert "interactive session" not in body.lower()


def test_bare_entry_point_and_repl_comment_lines_are_dropped(tmp_path):
    root = _upstream(tmp_path, "demo")
    body = conv.convert_skill(root, "demo").text

    assert "# Start interactive REPL mode" not in body
    # 保留同一代码块里有意义的命令
    assert "cli-anything-demo --help" in body
    assert "cli-anything-demo export render out.pdf" in body
    # 裸入口命令 = 进 REPL 的那条，必须删掉
    assert "\ncli-anything-demo\n" not in body


def test_banner_forbids_direct_binary_and_repl(tmp_path):
    root = _upstream(tmp_path, "demo")
    body = conv.convert_skill(root, "demo").text

    lines = body.splitlines()
    title = next(index for index, line in enumerate(lines) if line.startswith("# "))
    banner_line = lines[title + 2]
    assert banner_line.startswith("> Every command in this Skill runs through the `appcli_run`")
    assert "appcli_list" in body
    assert "appcli_verify" in body


# ── 上游定位与挑选 ───────────────────────────────────────────────────

def test_in_package_skill_wins_over_published_copy(tmp_path):
    root = _upstream(tmp_path, "demo")
    published = root / "skills" / "cli-anything-demo" / "SKILL.md"
    published.parent.mkdir(parents=True)
    published.write_text(UPSTREAM_FIXTURE.replace("A demo appcli.", "PUBLISHED COPY."), "utf-8")

    skill = conv.convert_skill(root, "demo")
    assert "in-package" not in skill.text
    assert "cli_anything/demo/skills" in str(skill.source)


def test_published_copy_is_used_when_there_is_no_package_copy(tmp_path):
    root = tmp_path / "upstream"
    published = root / "skills" / "cli-anything-demo" / "SKILL.md"
    published.parent.mkdir(parents=True)
    published.write_text(UPSTREAM_FIXTURE, encoding="utf-8")

    skill = conv.convert_skill(root, "demo")
    assert skill.source == published


def test_missing_upstream_skill_is_an_error(tmp_path):
    registry = _registry(tmp_path, _appcli("demo"))
    with pytest.raises(conv.ConversionError) as excinfo:
        conv.convert_all(tmp_path / "upstream", registry)
    assert "没有找到 SKILL.md" in str(excinfo.value)


def test_only_verified_appclies_are_converted_by_default(tmp_path):
    root = _upstream(tmp_path, "demo")
    _upstream(tmp_path, "other")
    registry = _registry(tmp_path, _appcli("demo"), _appcli("other", riscv64="unverified"))

    skills, skipped = conv.convert_all(root, registry)
    assert [skill.name for skill in skills] == ["cli-anything-demo"]
    assert skipped == ["other（riscv64=unverified，未验收不落地）"]

    both, skipped_again = conv.convert_all(root, registry, include_unverified=True)
    assert sorted(skill.name for skill in both) == ["cli-anything-demo", "cli-anything-other"]
    assert skipped_again == []


def test_duplicate_skill_names_are_rejected(tmp_path, monkeypatch):
    root = _upstream(tmp_path, "demo")
    registry = _registry(tmp_path, _appcli("demo"))

    # 两个 appcli 都映射到同一个技能名（目录名与 name 都是 cli-anything-dupe）
    monkeypatch.setattr(
        conv, "convert_skill",
        lambda upstream, name, **kwargs: conv.ConvertedSkill(
            name="cli-anything-dupe",
            directory="cli-anything-dupe",
            text="---\nname: cli-anything-dupe\ndescription: x\n---\n\nbody\n",
            source=Path("/dev/null"),
        ),
    )
    with pytest.raises(conv.ConversionError) as excinfo:
        # 两个 appcli 映射到同一个技能名：Runtime 的 skills_list 会静默丢一个
        monkeypatch.setattr(
            conv.AppCliRegistry, "load",
            classmethod(lambda cls, path=None: _FakeRegistry(["a", "b"])),
        )
        conv.convert_all(root, registry)
    assert "技能名冲突" in str(excinfo.value)


class _FakeRegistry:
    def __init__(self, names):
        from bibit_intent.core.appcli_registry import AppCliSpec

        self._specs = {
            name: AppCliSpec(
                name=name, display=name, entry_point=f"cli-anything-{name}",
                channel="artifact-only", riscv64="verified",
                actions=[{"name": "peek", "argv": ["peek"], "readonly": True}],
            )
            for name in names
        }

    def specs(self):
        return tuple(self._specs.values())


# ── CLI ──────────────────────────────────────────────────────────────

def test_cli_writes_skills_and_report(tmp_path, capsys):
    root = _upstream(tmp_path, "demo")
    registry = _registry(tmp_path, _appcli("demo"))
    out = tmp_path / "skills"
    report = tmp_path / "report.json"

    code = conv.main([
        "--upstream", str(root), "--out", str(out),
        "--registry", str(registry), "--report", str(report),
    ])

    assert code == 0
    written = out / "cli-anything-demo" / "SKILL.md"
    assert written.is_file()
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["skills"][0]["name"] == "cli-anything-demo"
    assert payload["skills"][0]["registry_hints"]["category"] == "office"
    assert "写入：" in capsys.readouterr().out


def test_cli_check_mode_detects_missing_stale_and_extra(tmp_path, capsys):
    root = _upstream(tmp_path, "demo")
    registry = _registry(tmp_path, _appcli("demo"))
    out = tmp_path / "skills"
    argv = ["--upstream", str(root), "--out", str(out), "--registry", str(registry), "--check"]

    assert conv.main(argv) == 1  # 还没生成

    conv.main(["--upstream", str(root), "--out", str(out), "--registry", str(registry)])
    assert conv.main(argv) == 0

    (out / "cli-anything-demo" / "SKILL.md").write_text("tampered", encoding="utf-8")
    assert conv.main(argv) == 1
    assert "过期" in capsys.readouterr().err

    extra = out / "cli-anything-gone" / "SKILL.md"
    extra.parent.mkdir()
    extra.write_text("---\nname: x\n---\n", encoding="utf-8")
    conv.main(["--upstream", str(root), "--out", str(out), "--registry", str(registry)])
    assert conv.main(argv) == 1  # 上游已不在清单里 → 多余产物
    assert "多余" in capsys.readouterr().err


def test_check_ignores_skills_the_converter_does_not_own(tmp_path, capsys):
    """产物落在 Runtime 的 `resources/skills/`，旁边 20 多个手工技能不归转换器管。

    早期实现把所有不在同步清单里的目录都判成「多余」，于是 `--check` 在真实
    skills 根上永远是红的，等于没有 CI 校验。
    """
    root = _upstream(tmp_path, "demo")
    registry = _registry(tmp_path, _appcli("demo"))
    out = tmp_path / "skills"
    manual = out / "verification-before-completion" / "SKILL.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("---\nname: verification-before-completion\n---\n\n手工技能。\n")
    argv = ["--upstream", str(root), "--out", str(out), "--registry", str(registry)]

    assert conv.main([*argv, "--check"]) == 1  # 还没生成被测技能
    assert conv.main(argv) == 0

    assert conv.main([*argv, "--check"]) == 0
    assert "多余" not in capsys.readouterr().err


def test_cli_reports_conversion_failure_with_nonzero_exit(tmp_path, capsys):
    registry = _registry(tmp_path, _appcli("demo"))
    code = conv.main([
        "--upstream", str(tmp_path / "missing"),
        "--out", str(tmp_path / "out"), "--registry", str(registry),
    ])
    assert code == 2
    assert "转换失败" in capsys.readouterr().err


# ── 真实上游（有克隆就跑） ───────────────────────────────────────────

def _real_upstream_root() -> Path | None:
    """上游检出：`CLI_ANYTHING_UPSTREAM` 优先，其次历史默认路径。

    写死 `/tmp/cli-anything-repo` 时，克隆放在别处（例如 ~/workspace/CLI-Anything）
    的机器永远跑不到这条用例——它恰恰是唯一能验证「上游目录名对不对」的一条。
    """
    candidates = [
        os.environ.get("CLI_ANYTHING_UPSTREAM"),
        "/tmp/cli-anything-repo",
        str(Path.home() / "workspace/CLI-Anything"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "registry.json").is_file():
            return Path(candidate)
    return None


REAL_UPSTREAM = _real_upstream_root()


@pytest.mark.skipif(REAL_UPSTREAM is None, reason="没有上游克隆（设 CLI_ANYTHING_UPSTREAM）")
def test_real_upstream_registry_appclies_convert_cleanly(tmp_path):
    skills, _ = conv.convert_all(
        REAL_UPSTREAM, None, include_unverified=True
    )
    assert skills, "本仓注册表里的 appcli 至少应能找到一个上游技能"
    for skill in skills:
        conv.validate(skill.text)
        assert skill.text.startswith("---\nname: cli-anything-")
        assert "## Installation" not in skill.text


@pytest.mark.skipif(REAL_UPSTREAM is None, reason="没有上游克隆（设 CLI_ANYTHING_UPSTREAM）")
def test_real_upstream_locates_the_in_package_skill():
    """上游目录名写错时这里会退到已发布副本（两份不同），必须命中包内那份。"""
    registry = conv.AppCliRegistry.load(None)
    for spec in registry.specs():
        if not spec.verified:
            continue
        source = conv.find_upstream_skill(REAL_UPSTREAM, spec.name)
        assert "agent-harness" in source.as_posix(), source
