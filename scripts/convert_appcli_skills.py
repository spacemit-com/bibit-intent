#!/usr/bin/env python3
"""把上游 CLI-Anything 的 SKILL.md 转换成 Runtime 能加载的技能（H12）。

上游技能是「某个 CLI 怎么用」的说明书，Runtime 技能是「这类任务怎么做」的过程性记忆，
两者格式**不兼容**（实测 151 个里 109 个会被 Runtime 加载器拒绝，而且是启动期硬报错）：

- 上游模板用 YAML 折叠块标量写 ``description: >-``，Runtime 的解析器是行式的，会把缩进
  续行当成 frontmatter 行 → ``unsupported SKILL.md frontmatter at line N``；
- 上游还写 ``author``/``runAs``/``command``/``requires``/``install`` 等键，Runtime 只认
  ``name``/``display_name``/``description``/``version`` 四个，多一个就报错；
- 正文的 ``## Installation`` 段教模型 ``pip install git+github``，与本仓「安装必须走
  dep_plan + 审批」直接冲突；``## REPL Mode`` 段教模型进交互式会话，而 ``appcli_run``
  明确禁止无子命令调用。

所以转换器做四件事，并且**只在构建期跑**（上游模板一改，手工维护的副本就会静默失效）：

1. 折叠块标量 → 单行 description（过长时截到第一句）；
2. 未登记键丢弃，有注册表语义的键（category/entry_point/requires/install）收集进报告，
   交给 H15 的流水线合并进 ``appcli_registry.yaml``；
3. 删掉 ``## Installation`` 与 ``## REPL Mode`` 段，并在正文顶部插入一句
   「一律经 appcli_run 执行」的约束；
4. 目录名与 frontmatter ``name`` 统一为 ``cli-anything-<appcli>`` 并查重——Runtime 的
   ``skills_list`` 读 frontmatter 名、``skill_view`` 读目录名，两者不一致会「列得出、打不开」。

只同步 ``riscv64: verified`` 的 appcli（未验收的技能不落地，否则模型会选到跑不通的能力）；
``--include-unverified`` 仅供开发机自检，产物不进设备。

用法::

    python scripts/convert_appcli_skills.py --upstream /tmp/cli-anything-repo --out /tmp/skills
    python scripts/convert_appcli_skills.py ... --check     # 只校验产物是否与源同步（CI 用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bibit_intent.core.appcli_registry import AppCliRegistry, AppCliRegistryError  # noqa: E402

#: Runtime 加载器接受的 frontmatter 键（bianbu_agent/skills/loader.py 的 _FRONTMATTER_FIELDS）。
FRONTMATTER_FIELDS = ("name", "display_name", "description", "version")

#: 上游有注册表语义的键：不进技能文件，进报告（由 H15 流水线并入 appcli_registry.yaml）。
REGISTRY_KEYS = ("category", "entry_point", "requires", "install", "command", "entrypoint")

#: 必须从正文删除的段标题（小写比较，前缀匹配）。
STRIPPED_SECTIONS = ("installation", "repl mode")

#: 标题里出现这些词也整段删除（上游把 REPL 会话散在 Examples 里当小节）。
STRIPPED_SECTION_WORDS = ("repl",)

#: 裸入口命令 = 无子命令调用 = 进 REPL（appcli_run 也拒绝这种调用）。
_BARE_ENTRY = re.compile(r"^cli-anything-[a-z0-9-]+$")

#: description 上限。Runtime 只用它做触发判断，不需要上游那种「全部能力枚举」。
MAX_DESCRIPTION_CHARS = 400

BANNER = (
    "> Every command in this Skill runs through the `appcli_run` tool, which takes a "
    "registered appcli and action with `key=value` arguments. Do not run the binary "
    "directly, do not pass free-form command lines, and do not enter an interactive REPL.\n"
    "> Before the first run, call `appcli_list` to see the appcli, its actions and their "
    "parameters; use `appcli_verify` to re-check a file that was produced earlier."
)

_HEADING = re.compile(r"^(?P<hashes>#{2,})\s+(?P<title>.+?)\s*$")
_SKILL_DIR = re.compile(r"^cli-anything-(?P<name>[a-z0-9][a-z0-9-]*)$")


class ConversionError(RuntimeError):
    """无法转换（缺上游文件、名字冲突、产物不合 Runtime 契约）。"""


@dataclass
class ConvertedSkill:
    name: str
    directory: str
    text: str
    source: Path
    registry_hints: dict = field(default_factory=dict)


# ── frontmatter ──────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """解析上游 frontmatter：支持折叠/字面块标量、映射与序列（Runtime 的解析器都不支持）。

    上游 frontmatter 是**真 YAML**（``description: >-`` 折叠标量、``requires:`` + ``- 依赖``
    序列），而 Runtime 的解析器是行式的，只认 ``key: value`` 单行。这里做得比 Runtime
    宽松，是为了把上游格式**读完再改写**，而不是在构建期直接报错。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ConversionError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ConversionError("SKILL.md 的 frontmatter 没有结束标记") from exc

    fields: dict[str, object] = {}
    current: str | None = None
    collecting = False
    pending: list[str] = []

    def flush() -> None:
        """把收集到的续行落成值：全是 ``- item`` 就是序列，否则按标量拼接。"""
        if current is None or not collecting:
            return
        items = [line.strip() for line in pending if line.strip()]
        if items and all(item.startswith("- ") for item in items):
            fields[current] = [item[2:].strip() for item in items]
        else:
            fields[current] = " ".join(items)

    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or line.lstrip().startswith("#"):
            continue
        if collecting and (line.startswith((" ", "\t")) or stripped.startswith("- ")):
            pending.append(stripped)
            continue
        flush()
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not key:
            raise ConversionError(f"frontmatter 行无法解析：{line!r}")
        current = key
        marker = value.strip()
        pending = []
        if marker in {">", ">-", ">+", "|", "|-", "|+"} or marker == "":
            # 块标量或映射/序列：值在后续（缩进的）行里
            fields[key] = "" if marker == "" else ""
            collecting = True
            continue
        fields[key] = marker.strip("\"'")
        collecting = False
    flush()
    body = "\n".join(lines[end + 1:]).strip()
    return fields, body


def collapse_description(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """压成单行；超过上限时截到第一句（触发判断只需要「这是什么」）。"""
    single = " ".join(text.split())
    if len(single) <= limit:
        return single
    head, separator, _ = single.partition(". ")
    if separator and len(head) + 1 <= limit:
        return head + "."
    return single[:limit].rstrip(". ") + "…"


def strip_sections(body: str) -> str:
    """删除 Installation / REPL Mode 段（连标题一起，直到下一个同级或更高级标题）。"""
    kept: list[str] = []
    skipping_level: int | None = None
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group("hashes"))
            title = heading.group("title").strip().lower()
            if skipping_level is not None and level <= skipping_level:
                skipping_level = None
            if skipping_level is None and (
                any(title.startswith(t) for t in STRIPPED_SECTIONS)
                or any(word in title for word in STRIPPED_SECTION_WORDS)
            ):
                skipping_level = level
            if skipping_level is not None:
                continue
        if skipping_level is not None:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def strip_repl_traces(body: str) -> str:
    """清掉散在 Usage/Examples 里的 REPL 残留。

    删掉 ``## REPL Mode`` 段还不够：上游在 Usage 里写着 ``# Start interactive REPL mode``
    加一条裸命令，在 Examples 里还有一整段 REPL 会话（标题不含 "REPL Mode" 但含 "REPL"）。
    留着这些会让模型以为可以进交互式会话，而 ``appcli_run`` 明确禁止无子命令调用。
    这里做两件事：整段删除标题含 repl 的小节（见 ``strip_sections``），以及删掉提到 repl
    的行与「裸入口命令」行——裸调用就是进 REPL 的那条命令。
    """
    kept: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            kept.append(line)
            continue
        if in_fence:
            if "repl" in stripped.lower() or _BARE_ENTRY.match(stripped):
                continue
            kept.append(line)
            continue
        if "repl" in stripped.lower():
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def insert_banner(body: str, banner: str = BANNER) -> str:
    """把「经 appcli_run 执行」的约束插到一级标题之后。"""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join([*lines[: index + 1], "", banner, *lines[index + 1:]]).strip()
    return banner + "\n\n" + body.strip()


def render(fields: dict[str, object], body: str) -> str:
    """生成 Runtime 加载器能吃的 SKILL.md（四键、单行、值不加引号）。"""
    ordered = [key for key in FRONTMATTER_FIELDS if str(fields.get(key, "")).strip()]
    extra = sorted(set(fields) - set(FRONTMATTER_FIELDS))
    if extra:
        raise ConversionError(f"仍有未登记键：{', '.join(extra)}")
    header = "\n".join(f"{key}: {str(fields[key]).strip()}" for key in ordered)
    return f"---\n{header}\n---\n\n{body.strip()}\n"


# ── 上游定位 ─────────────────────────────────────────────────────────

def find_upstream_skill(upstream: Path, appcli: str) -> Path:
    """找该 appcli 的 SKILL.md，**优先包内那份**（与 wheel 同版本）。"""
    in_package = sorted(upstream.glob(f"{appcli}/agent-harness/cli_anything/*/skills/SKILL.md"))
    if in_package:
        return in_package[0]
    published = upstream / "skills" / f"cli-anything-{appcli}" / "SKILL.md"
    if published.is_file():
        return published
    raise ConversionError(
        f"{appcli}：上游没有找到 SKILL.md（试过 {appcli}/agent-harness/... 与 "
        f"skills/cli-anything-{appcli}/SKILL.md）"
    )


# ── 转换与校验 ───────────────────────────────────────────────────────

def validate(text: str) -> None:
    """按 Runtime 加载器的规则自检（行式解析、四键、单行、name+description 必填）。

    为什么在这里重写一遍：bibit-intent 不依赖 bianbu-agent-runtime，构建期无法 import
    真实加载器；这份校验的作用是「构建期就报错」，而不是「相信它等价」——真正的等价性
    由 Runtime 侧 `tests/sdk/skills/test_skill_loader.py` 与设备侧加载把关。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ConversionError("产物的第一行必须是 ---")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ConversionError("产物的 frontmatter 没有结束标记") from exc
    metadata: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise ConversionError(f"产物的第 {number} 行是续行（块标量没有压平）：{line!r}")
        key, separator, value = line.partition(":")
        if not separator or key.strip() not in FRONTMATTER_FIELDS:
            raise ConversionError(f"产物有未登记的 frontmatter 行（第 {number} 行）：{line!r}")
        metadata[key.strip()] = value.strip().strip("'\"")
    for required in ("name", "description"):
        if not metadata.get(required, "").strip():
            raise ConversionError(f"产物缺少 {required}")
    if not "\n".join(lines[end + 1:]).strip():
        raise ConversionError("产物正文为空")


def convert_skill(upstream: Path, appcli: str, *, display: str = "") -> ConvertedSkill:
    source = find_upstream_skill(upstream, appcli)
    fields, body = parse_frontmatter(source.read_text(encoding="utf-8"))

    registry_hints: dict[str, object] = {
        key: fields.pop(key) for key in REGISTRY_KEYS if key in fields
    }
    for key in list(fields):
        if key not in FRONTMATTER_FIELDS:
            del fields[key]  # 丢弃：author / runAs / metadata / contributor …

    directory = f"cli-anything-{appcli}"
    fields["name"] = directory
    # 上游没有 display_name；本仓注册表的 display 是产品名，用它兜底。
    fields.setdefault("display_name", display or appcli)
    fields["description"] = collapse_description(str(fields.get("description", "")))
    if not fields["description"]:
        raise ConversionError(f"{appcli}：上游没有 description（无法生成触发描述）")
    fields.setdefault("version", "1.0.0")

    text = render(fields, insert_banner(strip_repl_traces(strip_sections(body))))
    validate(text)
    return ConvertedSkill(
        name=directory,
        directory=directory,
        text=text,
        source=source,
        registry_hints=registry_hints,
    )


def convert_all(
    upstream: Path,
    registry_path: Path | None = None,
    *,
    include_unverified: bool = False,
) -> tuple[list[ConvertedSkill], list[str]]:
    """按注册表挑 appcli 并逐个转换；返回 (产物, 跳过的 appcli 说明)。"""
    registry = AppCliRegistry.load(registry_path)
    converted: list[ConvertedSkill] = []
    skipped: list[str] = []
    for spec in registry.specs():
        if not spec.verified and not include_unverified:
            skipped.append(f"{spec.name}（riscv64={spec.riscv64}，未验收不落地）")
            continue
        converted.append(convert_skill(upstream, spec.name, display=spec.display))

    names = [item.name for item in converted]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        # Runtime 的 skills_list 用 setdefault 去重，重名会**静默丢技能**。
        raise ConversionError(f"技能名冲突（Runtime 会静默丢一个）：{', '.join(duplicates)}")
    return converted, skipped


# ── 落盘 ─────────────────────────────────────────────────────────────

def write_skills(skills: list[ConvertedSkill], out: Path) -> list[Path]:
    written: list[Path] = []
    for skill in skills:
        target = out / skill.directory / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill.text, encoding="utf-8")
        written.append(target)
    return written


def check_skills(skills: list[ConvertedSkill], out: Path) -> list[str]:
    """返回与磁盘不一致的地方（CI 失败用），空列表表示同步。

    产物直接落在 Runtime 的 `resources/skills/`（那里还有 20 多个手工技能），
    所以「多余」只对转换器自己管的 `cli-anything-*` 目录判定：别的技能不归它管，
    报出来会让 `--check` 永远为红。
    """
    problems: list[str] = []
    expected = {skill.directory for skill in skills}
    for skill in skills:
        target = out / skill.directory / "SKILL.md"
        if not target.is_file():
            problems.append(f"缺失：{target}")
        elif target.read_text(encoding="utf-8") != skill.text:
            problems.append(f"过期：{target}")
    for existing in sorted(path for path in out.glob("*/SKILL.md") if path.is_file()):
        if _SKILL_DIR.match(existing.parent.name) and existing.parent.name not in expected:
            problems.append(f"多余：{existing}（上游已不在同步清单里）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="转换上游 CLI-Anything 技能为 Runtime 技能")
    parser.add_argument("--upstream", required=True, type=Path, help="CLI-Anything 仓库根目录")
    parser.add_argument("--out", required=True, type=Path, help="产物目录（Runtime 的 skills 根）")
    parser.add_argument("--registry", type=Path, default=None, help="appcli 注册表路径")
    parser.add_argument(
        "--include-unverified", action="store_true",
        help="连未验收的 appcli 一起转换（开发机自检用，产物不进设备）",
    )
    parser.add_argument("--report", type=Path, default=None, help="写出转换报告 JSON")
    parser.add_argument("--check", action="store_true", help="只校验产物是否与源同步")
    args = parser.parse_args(argv)

    try:
        skills, skipped = convert_all(
            args.upstream, args.registry, include_unverified=args.include_unverified
        )
    except (ConversionError, AppCliRegistryError) as exc:
        print(f"转换失败：{exc}", file=sys.stderr)
        return 2

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "upstream": str(args.upstream),
                    "skills": [
                        {
                            "name": skill.name,
                            "source": str(skill.source),
                            "registry_hints": skill.registry_hints,
                        }
                        for skill in skills
                    ],
                    "skipped": skipped,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    for note in skipped:
        print(f"跳过：{note}")

    if args.check:
        problems = check_skills(skills, args.out)
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"同步 {len(skills)} 个，问题 {len(problems)} 个")
        return 1 if problems else 0

    written = write_skills(skills, args.out)
    for path in written:
        print(f"写入：{path}")
    print(f"完成：{len(written)} 个技能，跳过 {len(skipped)} 个")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
