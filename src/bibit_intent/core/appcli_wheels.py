"""CLI-Anything 应用 wheel 的锁定表（H15）。

上游 79 个 CLI 里有 73 个的官方安装方式是

    pip install git+https://github.com/HKUDS/CLI-Anything.git#subdirectory=<dir>

端侧不能接受这种来源：`git+` 是「没有校验和的源码拉取」，既不可审计也不可离线。所以
`resources/appcli_wheels.yaml` 固定三件事，本模块是它唯一的读取入口：

- **上游提交**：`upstream.repository` + 40 位 `commit`——升级必须改这个值并重跑 H16 验收；
- **元数据断言**：每个包的 `name`/`version`/`subdirectory`/`skill`/`entry_point`，与上游
  `registry.json` 不一致就构建失败（防止「wheel 换了、注册表没换」）；
- **内部源**：`internal.index_url`（SpacemiT PyPI 镜像，与 `core/pip_index.py` 同一口径）
  与 `internal.wheel_dir`（设备上放离线 wheel 的目录），依赖计划据此标注真实来源。

`sha256` 由构建流水线回填（`scripts/build_appcli_wheels.py --update-lock`），为空表示
「这份 wheel 还没在内部源上发布过」——设备侧据此知道自己该等发布，而不是去上游拉源码。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from bibit_intent.resources import resource_path

#: `sha256:` 缺省写法：允许留空（未发布），填了就必须是 64 位十六进制。
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
#: 源码直取的 pip 规格：`git+https://…`、`git+ssh://…`，以及 `#subdirectory=` 变体。
_GIT_PLUS_RE = re.compile(r"(?:^|[=\s\"'`(])git\+", re.IGNORECASE)


class AppCliWheelLockError(RuntimeError):
    """锁定表缺失、损坏或字段非法。"""


@dataclass(frozen=True)
class AppCliWheel:
    """一个应用对应的 wheel 及其内部源来源。"""

    appcli: str
    name: str
    version: str
    subdirectory: str
    skill: str
    entry_point: str
    sha256: str

    @property
    def published(self) -> bool:
        """是否已在内部源上发布（有校验和）。"""
        return bool(self.sha256)


@dataclass(frozen=True)
class AppCliWheelLock:
    """锁定表内容：上游提交 + 内部源 + 每个应用的 wheel。"""

    repository: str
    commit: str
    registry: str
    index_url: str
    wheel_dir: str
    packages: tuple[AppCliWheel, ...]

    def for_package(self, package: str) -> AppCliWheel | None:
        wanted = str(package or "").strip()
        return next((item for item in self.packages if item.name == wanted), None)

    def for_appcli(self, appcli: str) -> AppCliWheel | None:
        wanted = str(appcli or "").strip()
        return next((item for item in self.packages if item.appcli == wanted), None)

    def unpublished(self) -> tuple[AppCliWheel, ...]:
        return tuple(item for item in self.packages if not item.published)


def _require(document: object, key: str, where: str) -> object:
    if not isinstance(document, dict) or key not in document:
        raise AppCliWheelLockError(f"{where} 缺少字段 {key}")
    return document[key]


def _text(value: object, where: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AppCliWheelLockError(f"{where} 不能为空")
    return text


def _relative(value: object, where: str) -> str:
    """相对路径：不允许绝对路径与 `..`，它们能把构建指到仓库外。"""
    text = _text(value, where)
    parts = Path(text).parts
    if Path(text).is_absolute() or ".." in parts:
        raise AppCliWheelLockError(f"{where} 必须是仓库内的相对路径：{text!r}")
    return text


def _parse_package(raw: object, index: int) -> AppCliWheel:
    where = f"packages[{index}]"
    appcli = _text(_require(raw, "appcli", where), f"{where}.appcli")
    name = _text(_require(raw, "name", where), f"{where}.name")
    version = _text(_require(raw, "version", where), f"{where}.version")
    subdirectory = _relative(
        _require(raw, "subdirectory", where), f"{where}.subdirectory"
    )
    skill = _relative(_require(raw, "skill", where), f"{where}.skill")
    entry_point = _text(_require(raw, "entry_point", where), f"{where}.entry_point")
    sha256 = str((raw or {}).get("sha256") or "").strip().lower()
    if sha256 and not _SHA256_RE.match(sha256):
        raise AppCliWheelLockError(f"{where}.sha256 不是 sha256 十六进制：{sha256!r}")
    return AppCliWheel(
        appcli=appcli,
        name=name,
        version=version,
        subdirectory=subdirectory,
        skill=skill,
        entry_point=entry_point,
        sha256=sha256,
    )


def parse_lock(document: object, *, source: str = "appcli_wheels.yaml") -> AppCliWheelLock:
    """校验并归一化锁定表；任何非法字段都抛 AppCliWheelLockError（不猜、不跳过）。"""
    if not isinstance(document, dict):
        raise AppCliWheelLockError(f"{source} 不是 YAML 映射")
    if document.get("version") != 1:
        raise AppCliWheelLockError(f"{source} 的 version 必须是 1")

    upstream = _require(document, "upstream", source)
    repository = _text(
        _require(upstream, "repository", f"{source}:upstream"), f"{source}:upstream.repository"
    )
    if not repository.startswith("https://"):
        raise AppCliWheelLockError(f"{source}:upstream.repository 必须是 https 地址")
    commit = _text(_require(upstream, "commit", f"{source}:upstream"), f"{source}:upstream.commit").lower()
    if not _COMMIT_RE.match(commit):
        raise AppCliWheelLockError(f"{source}:upstream.commit 必须是 40 位提交号：{commit!r}")
    registry = _relative(
        _require(upstream, "registry", f"{source}:upstream"), f"{source}:upstream.registry"
    )

    internal = _require(document, "internal", source)
    index_url = _text(
        _require(internal, "index_url", f"{source}:internal"), f"{source}:internal.index_url"
    )
    wheel_dir = _text(
        _require(internal, "wheel_dir", f"{source}:internal"), f"{source}:internal.wheel_dir"
    )

    # 先查 git+ 再查协议：`git+https://…` 看着像 https，先报「来源不许源码直取」比报
    # 「不是 https 地址」更贴近真实缺陷（端侧只允许内部源或本地 wheel 文件）。
    polluted = git_plus_specs([repository, index_url, wheel_dir])
    if polluted:
        raise AppCliWheelLockError(f"{source} 不允许 git+ 来源：{', '.join(polluted)}")
    if not index_url.startswith("https://"):
        raise AppCliWheelLockError(f"{source}:internal.index_url 必须是 https 地址")
    if not wheel_dir.startswith("/"):
        raise AppCliWheelLockError(f"{source}:internal.wheel_dir 必须是绝对路径")

    raw_packages = _require(document, "packages", source)
    if not isinstance(raw_packages, list) or not raw_packages:
        raise AppCliWheelLockError(f"{source} 的 packages 必须是非空列表")
    packages = tuple(_parse_package(raw, index) for index, raw in enumerate(raw_packages))

    for seen, label in (
        ({item.name for item in packages}, "name"),
        ({item.appcli for item in packages}, "appcli"),
        ({item.subdirectory for item in packages}, "subdirectory"),
    ):
        if len(seen) != len(packages):
            raise AppCliWheelLockError(f"{source} 的 packages 有重复 {label}")

    return AppCliWheelLock(
        repository=repository,
        commit=commit,
        registry=registry,
        index_url=index_url,
        wheel_dir=wheel_dir,
        packages=packages,
    )


def load_lock(path: Path | str | None = None) -> AppCliWheelLock:
    """读锁定表；默认读包内的 `resources/appcli_wheels.yaml`。"""
    target = Path(path) if path is not None else resource_path("appcli_wheels.yaml")
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppCliWheelLockError(f"锁定表不可读：{target}（{exc}）") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AppCliWheelLockError(f"锁定表不是合法 YAML：{target}（{exc}）") from exc
    return parse_lock(document, source=str(target))


def git_plus_specs(specs: Iterable[str]) -> list[str]:
    """挑出「从源码直取」的规格（`git+…`）。

    依赖计划里出现这类来源即为缺陷：端侧只允许内部源或本地 wheel 文件。
    """
    return [str(spec) for spec in specs if _GIT_PLUS_RE.search(str(spec))]


def source_note(package: str, lock: AppCliWheelLock | None = None) -> str:
    """某个 wheel 包的来源说明；不是应用包就返回空串。

    给依赖计划与缺前置提示共用：用户看到的是「内部源（镜像）」或「设备上的离线 wheel
    目录」，而不是上游的 git+ 安装命令。
    """
    try:
        resolved = lock if lock is not None else load_lock()
    except AppCliWheelLockError:
        return ""
    item = resolved.for_package(package)
    if item is None:
        return ""
    host = resolved.index_url.split("//", 1)[-1].split("/", 1)[0]
    if item.published:
        return f"内部源 {host}（wheel {item.name}=={item.version}，已发布）"
    return (
        f"内部源 {host}（wheel {item.name}=={item.version} 尚未发布；"
        f"发布前可从设备目录 {resolved.wheel_dir} 离线安装）"
    )


__all__ = [
    "AppCliWheel",
    "AppCliWheelLock",
    "AppCliWheelLockError",
    "git_plus_specs",
    "load_lock",
    "parse_lock",
    "source_note",
]
