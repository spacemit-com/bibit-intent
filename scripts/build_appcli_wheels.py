#!/usr/bin/env python3
"""H15：内部 wheel 构建 / 校验流水线（设计见 docs/design/cli_anything_integration.md §7）。

上游 CLI-Anything 的官方装法是 `pip install git+…`，端侧不接受这种来源。本脚本把
「锁定表 → wheel → 技能」三件事放在**同一次运行**里做完，避免三者各自漂移：

1. **元数据**：按锁定表（`resources/appcli_wheels.yaml`）读上游 `registry.json`，断言
   `name`/`version`/`subdirectory`/`skill`/`entry_point` 与锁定表、本仓 `appcli_registry.yaml`
   三方一致；注册表里 `riscv64: verified` 的 appcli 必须在锁定表里有 wheel。
2. **wheel**：在每个包的 `subdirectory` 构建 wheel、算 sha256，与锁定表比对
   （`--update-lock` 回填；比对不上说明上游或工具链变了，必须显式确认），写出清单
   `appcli-wheels.json`（wheel 名 + 版本 + 校验和 + 来源提交）。
3. **技能**：调用 H12 的转换器，把 `riscv64: verified` 的 appcli 技能转成 Runtime 兼容
   格式（`--include-unverified` 仅供开发机自检）。

用法::

    # CI / 开发机校验：不联网、不构建（锁定表 schema + 元数据一致 + 技能同步 + 无 git+）
    python scripts/build_appcli_wheels.py --check --upstream /tmp/cli-anything-repo
    # 真构建：拉上游子目录构建 wheel 并把校验和写回锁定表
    python scripts/build_appcli_wheels.py --upstream /tmp/cli-anything-repo \\
        --out dist/appcli-wheels --update-lock

退出码：0 全部通过；1 校验发现问题；2 环境/输入不满足（缺锁定表、上游不是那个提交等）。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from bibit_intent.core.appcli_registry import AppCliRegistry, AppCliRegistryError  # noqa: E402
from bibit_intent.core.appcli_wheels import (  # noqa: E402
    AppCliWheel,
    AppCliWheelLock,
    AppCliWheelLockError,
    git_plus_specs,
    load_lock,
)

DEFAULT_OUT = REPO_ROOT / "dist" / "appcli-wheels"
MANIFEST_NAME = "appcli-wheels.json"


class PipelineError(RuntimeError):
    """输入不满足流水线的前置条件（不是「校验不通过」）。"""


@dataclass
class Report:
    """一次运行的结果：problem 为空即通过。"""

    problems: list[str]
    manifest: dict | None = None
    written: list[Path] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.written = list(self.written or [])


# ── 上游读取 ──────────────────────────────────────────────────────────

def head_commit(upstream: Path) -> str | None:
    """上游检出目录的当前提交；不是 git 仓库或没有 git 时返回 None。"""
    if shutil.which("git") is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip().lower() or None


def upstream_registry(upstream: Path, lock: AppCliWheelLock) -> dict[str, dict]:
    """上游 `registry.json` 的 `name -> 条目` 映射。"""
    path = upstream / lock.registry
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"读不到上游注册表 {path}：{exc}") from exc
    entries = raw.get("clis") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise PipelineError(f"{path} 里没有 clis 列表")
    return {str(item.get("name") or ""): item for item in entries if isinstance(item, dict)}


# ── 第 1 步：元数据 ──────────────────────────────────────────────────

def check_metadata(
    upstream: Path, lock: AppCliWheelLock, registry_path: Path | None, report: Report,
) -> None:
    registry = AppCliRegistry.load(registry_path)
    specs = {spec.name: spec for spec in registry.specs()}
    entries = upstream_registry(upstream, lock)

    for package in lock.packages:
        spec = specs.get(package.appcli)
        if spec is None:
            report.problems.append(f"锁定表里的 {package.appcli} 不在 appcli_registry.yaml 里")
            continue
        if spec.entry_point != package.entry_point:
            report.problems.append(
                f"{package.appcli}：入口命令不一致（注册表 {spec.entry_point} / "
                f"锁定表 {package.entry_point}）"
            )
        if package.name not in spec.requires.python:
            report.problems.append(
                f"{package.appcli}：注册表 requires.python 里没有 {package.name}，"
                "缺前置提示就不会指向内部源"
            )
        entry = entries.get(package.appcli)
        if entry is None:
            report.problems.append(f"{package.appcli}：上游 {lock.registry} 里没有这个 CLI")
            continue
        for field, expected in (
            ("version", package.version), ("entry_point", package.entry_point),
            ("skill_md", package.skill),
        ):
            if str(entry.get(field) or "") != expected:
                report.problems.append(
                    f"{package.appcli}：上游 {field}={entry.get(field)!r}，锁定表写的是 {expected!r}"
                )
        if not (upstream / package.subdirectory).is_dir():
            report.problems.append(f"{package.appcli}：上游没有子目录 {package.subdirectory}")

    for spec in registry.specs():
        if spec.verified and lock.for_appcli(spec.name) is None:
            report.problems.append(
                f"{spec.name} 已标 riscv64: verified，但锁定表里没有它的 wheel（内部源拿什么装？）"
            )

    # 注册表/锁定表都不许出现 git+ 来源：设备侧只允许内部源或本地 wheel。
    polluted = git_plus_specs([
        *(item for spec in registry.specs() for item in spec.requires.python),
        lock.repository,
    ])
    if polluted:
        report.problems.append(f"注册表里出现源码直取来源：{', '.join(polluted)}")


# ── 第 2 步：wheel ───────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pip_python() -> str:
    """找一个真的带 pip 的解释器。

    `sys.executable` 可能是 uv/venv 造出来的、**没有 pip** 的解释器（本仓的开发 venv 就是），
    这时直接构建会以「No module named pip」失败——那不是上游的问题，换一个解释器即可。
    """
    for candidate in dict.fromkeys([sys.executable, "python3", "/usr/bin/python3"]):
        if shutil.which(candidate) is None:
            continue
        try:
            probe = subprocess.run(
                [candidate, "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    raise PipelineError("找不到带 pip 的 Python 解释器（构建 appcli wheel 需要 pip）")


def build_wheel(upstream: Path, package: AppCliWheel, out: Path) -> Path:
    """在上游子目录里构建 wheel（纯 Python，产物与本机架构无关）。"""
    wheels_dir = out / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [pip_python(), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--disable-pip-version-check", "--no-input", "--wheel-dir", str(wheels_dir),
         str(upstream / package.subdirectory)],
        capture_output=True, text=True, timeout=600, check=False,
    )
    produced = sorted(wheels_dir.glob(f"{package.name.replace('-', '_')}-*.whl")) \
        or sorted(wheels_dir.glob(f"{package.name}-*.whl"))
    if completed.returncode != 0 or not produced:
        tail = " / ".join((completed.stderr or completed.stdout or "").strip().splitlines()[-3:])
        raise PipelineError(f"构建 {package.name} 失败：{tail}")
    return produced[-1]


def build_packages(
    upstream: Path, lock: AppCliWheelLock, out: Path, report: Report, *,
    update_lock: bool, builder=None,
) -> dict:
    manifest = {
        "format": 1,
        "upstream": {"repository": lock.repository, "commit": lock.commit},
        "index_url": lock.index_url,
        "packages": [],
    }
    build = builder if builder is not None else build_wheel
    for package in lock.packages:
        wheel = build(upstream, package, out)
        digest = sha256_file(wheel)
        if package.sha256 and package.sha256 != digest and not update_lock:
            report.problems.append(
                f"{package.name}：wheel 校验和不一致（锁定表 {package.sha256[:12]}…，"
                f"本次构建 {digest[:12]}…）；确认上游/工具链变化后跑 --update-lock"
            )
        manifest["packages"].append({
            "appcli": package.appcli, "name": package.name, "version": package.version,
            "wheel": wheel.name, "sha256": digest, "subdirectory": package.subdirectory,
            "entry_point": package.entry_point, "skill": package.skill,
        })
        report.written.append(wheel)
    manifest_path = out / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report.written.append(manifest_path)
    return manifest


def check_manifest(out: Path, lock: AppCliWheelLock, report: Report) -> None:
    """清单存在时，校验和必须与锁定表一致（只有已发布的包才有锁定校验和）。"""
    path = out / MANIFEST_NAME
    if not path.is_file():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.problems.append(f"{path} 不是合法 JSON：{exc}")
        return
    recorded = {
        str(item.get("name") or ""): str(item.get("sha256") or "")
        for item in manifest.get("packages") or []
        if isinstance(item, dict)
    }
    for package in lock.packages:
        if not package.sha256:
            continue
        observed = recorded.get(package.name)
        if observed is None:
            report.problems.append(f"清单里没有 {package.name}")
        elif observed != package.sha256:
            report.problems.append(
                f"{package.name}：清单校验和与锁定表不一致（{observed[:12]}… / "
                f"{package.sha256[:12]}…）"
            )


def update_lock_checksums(lock_path: Path, manifest: dict) -> None:
    """把本次构建的校验和写回锁定表的 sha256 字段（其余内容原样保留）。"""
    import yaml

    document = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    digests = {item["name"]: item["sha256"] for item in manifest.get("packages") or []}
    for item in document.get("packages") or []:
        digest = digests.get(str(item.get("name") or ""))
        if digest:
            item["sha256"] = digest
    lock_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


# ── 第 3 步：技能 ────────────────────────────────────────────────────

def load_converter():
    """加载 H12 的转换器（scripts/ 不是包，用路径加载；同一进程内调用便于 CI）。"""
    path = REPO_ROOT / "scripts" / "convert_appcli_skills.py"
    spec = importlib.util.spec_from_file_location("_bibit_convert_appcli_skills", path)
    module = importlib.util.module_from_spec(spec)
    # dataclass 处理注解时要能从 sys.modules 找到本模块（不注册会拿不到 __dict__）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sync_skills(
    upstream: Path, registry_path: Path, out: Path, report: Report, *,
    include_unverified: bool, check: bool, converter=None,
) -> None:
    module = converter if converter is not None else load_converter()
    try:
        skills, _skipped = module.convert_all(
            upstream, registry_path, include_unverified=include_unverified
        )
    except (module.ConversionError, AppCliRegistryError) as exc:
        report.problems.append(f"技能转换失败：{exc}")
        return
    skills_root = out / "skills"
    if check:
        report.problems.extend(module.check_skills(skills, skills_root))
    else:
        report.written.extend(module.write_skills(skills, skills_root))


# ── 主流程 ────────────────────────────────────────────────────────────

def run(
    *,
    upstream: Path,
    out: Path,
    lock_path: Path | None = None,
    registry_path: Path | None = None,
    check: bool = False,
    do_build: bool = True,
    update_lock: bool = False,
    include_unverified: bool = False,
    allow_commit_mismatch: bool = False,
    converter=None,
    builder=None,
) -> Report:
    report = Report(problems=[])
    try:
        lock = load_lock(lock_path)
    except AppCliWheelLockError as exc:
        raise PipelineError(str(exc)) from exc
    if not upstream.is_dir():
        raise PipelineError(f"上游目录不存在：{upstream}")

    seen = head_commit(upstream)
    if seen is not None and seen != lock.commit and not allow_commit_mismatch:
        raise PipelineError(
            f"上游检出在 {seen[:12]}…，锁定表要求 {lock.commit[:12]}…；"
            "换检出或确认后加 --allow-commit-mismatch"
        )

    resolved_registry = registry_path or REPO_ROOT / "src" / "bibit_intent" / "resources" / "appcli_registry.yaml"
    check_metadata(upstream, lock, resolved_registry, report)
    check_manifest(out, lock, report)

    manifest = None
    if do_build and not check:
        manifest = build_packages(
            upstream, lock, out, report, update_lock=update_lock, builder=builder
        )
        report.manifest = manifest
        if update_lock and lock_path is not None:
            update_lock_checksums(lock_path, manifest)

    sync_skills(
        upstream, resolved_registry, out, report,
        include_unverified=include_unverified, check=check, converter=converter,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建并校验 CLI-Anything appcli 内部 wheel")
    parser.add_argument("--upstream", required=True, type=Path, help="CLI-Anything 检出目录")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="产物目录（wheel + 清单 + 技能）")
    parser.add_argument("--lock", type=Path, default=None, help="锁定表路径（默认读包内资源）")
    parser.add_argument("--registry", type=Path, default=None, help="appcli 注册表路径")
    parser.add_argument("--check", action="store_true", help="只校验，不构建（CI 用）")
    parser.add_argument("--no-build", action="store_true", help="跳过 wheel 构建，只校验元数据与技能")
    parser.add_argument("--update-lock", action="store_true", help="把构建出的校验和写回锁定表")
    parser.add_argument(
        "--include-unverified", action="store_true",
        help="连未验收的 appcli 一起转技能（开发机自检，产物不进设备）",
    )
    parser.add_argument("--allow-commit-mismatch", action="store_true", help="上游检出与锁定提交不一致时继续")
    args = parser.parse_args(argv)

    try:
        report = run(
            upstream=args.upstream,
            out=args.out,
            lock_path=args.lock,
            registry_path=args.registry,
            check=args.check,
            do_build=not (args.check or args.no_build),
            update_lock=args.update_lock,
            include_unverified=args.include_unverified,
            allow_commit_mismatch=args.allow_commit_mismatch,
        )
    except PipelineError as exc:
        print(f"前置条件不满足：{exc}", file=sys.stderr)
        return 2

    for path in report.written:
        print(f"写入：{path}")
    for problem in report.problems:
        print(f"问题：{problem}", file=sys.stderr)
    print(f"appcli wheel 流水线：问题 {len(report.problems)} 个")
    return 1 if report.problems else 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
